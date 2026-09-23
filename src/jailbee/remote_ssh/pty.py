"""Bounded SSH byte relays and ownership of local child process groups."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import io
import os
import pty
import re
import signal
import struct
import termios
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from asyncssh import SSHServerProcess

_TERM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,63}$")
_BUFFER_SIZE = 65536


class PTYError(ValueError):
    """The requested terminal cannot be safely provided."""


class _RawEOFError(Exception):
    """Raw terminals cannot represent an input half-close."""


@dataclass(frozen=True)
class ChildSpec:
    argv: tuple[str, ...]
    cwd: Path
    requires_pty: bool


class _Reader(Protocol):
    async def read(self, n: int) -> bytes: ...


class _Writer(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


def validated_term(value: str | None) -> str:
    if value is None or not _TERM_RE.fullmatch(value):
        raise PTYError("invalid or missing terminal type")
    return value


def decode_wait_status(status: int) -> tuple[int | None, signal.Signals | None]:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status), None
    if os.WIFSIGNALED(status):
        return None, signal.Signals(os.WTERMSIG(status))
    raise PTYError(f"unexpected child wait status: {status}")


def _window_size(
    size: tuple[int, int, int, int], previous: bytes = struct.pack("HHHH", 24, 80, 0, 0)
) -> bytes:
    if len(size) != 4 or any(type(value) is not int or not 0 <= value <= 65535 for value in size):
        raise PTYError("invalid terminal dimensions")
    columns, rows, x_pixels, y_pixels = size
    old_rows, old_columns, old_x, old_y = struct.unpack("HHHH", previous)
    return struct.pack(
        "HHHH", rows or old_rows, columns or old_columns, x_pixels or old_x, y_pixels or old_y
    )


@contextmanager
def _controls(
    process: SSHServerProcess[bytes], pid: int, master: int | None = None
) -> Iterator[asyncio.Future[None]]:
    """Keep channel control callbacks independent of stdin EOF and flow control."""
    original_signal = process.signal_received
    original_resize = process.terminal_size_changed
    signal_names = signal.Signals.__members__
    size = _window_size(process.term_size) if master is not None else b""
    failed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def receive_signal(signal: str) -> None:
        sig = signal_names.get("SIG" + signal)
        if sig is None:
            original_signal(signal)
            return
        try:
            _signal_group(pid, sig)
        except OSError as exc:
            if not failed.done():
                failed.set_exception(exc)

    def resize(width: int, height: int, pixwidth: int, pixheight: int) -> None:
        nonlocal size
        if master is None:
            original_resize(width, height, pixwidth, pixheight)
            return
        try:
            size = _window_size((width, height, pixwidth, pixheight), size)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
        except (OSError, PTYError) as exc:
            if not failed.done():
                failed.set_exception(exc)

    # AsyncSSH invokes these public session callbacks directly. Consuming valid
    # controls here prevents them queuing behind blocked stdin bytes. Restore
    # the original handlers only after child/group cleanup has completed.
    # These per-session overrides intentionally replace AsyncSSH callback methods.
    process.signal_received = receive_signal  # type: ignore[method-assign]
    process.terminal_size_changed = resize  # type: ignore[method-assign]
    try:
        yield failed
    finally:
        # Restore the callback methods replaced above.
        process.signal_received = original_signal  # type: ignore[method-assign]
        process.terminal_size_changed = original_resize  # type: ignore[method-assign]
        if failed.done() and not failed.cancelled():
            failed.exception()
        failed.cancel()


def _signal_group(pid: int, sig: signal.Signals) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, sig)


async def _finish[T](task: asyncio.Task[T]) -> T:
    """Keep resource cleanup alive even if the caller is cancelled again."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _terminate(pid: int, waiter: asyncio.Task[int]) -> None:
    _signal_group(pid, signal.SIGHUP)

    async def wait_for_group() -> None:
        await asyncio.shield(waiter)
        while True:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            # Probe failures other than ESRCH are not evidence of termination.
            # Let them propagate to the caller instead of claiming cleanup.
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(wait_for_group(), 2)
    except TimeoutError:
        _signal_group(pid, signal.SIGKILL)
        await waiter


async def _cleanup_pipes(child: asyncio.subprocess.Process, waiter: asyncio.Task[int]) -> None:
    async def discard(reader: asyncio.StreamReader) -> None:
        with suppress(OSError):
            while await reader.read(_BUFFER_SIZE):
                pass

    # Process.wait() can wait for pipe closure. Continue draining after the SSH
    # relays stop, otherwise a full StreamReader buffer can prevent reaping.
    drains = [
        asyncio.create_task(discard(reader))
        for reader in (child.stdout, child.stderr)
        if reader is not None
    ]
    if child.stdin is not None:
        child.stdin.close()
    try:
        await _terminate(child.pid, waiter)
    finally:
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
        if child.stdin is not None:
            with suppress(BrokenPipeError, ConnectionResetError):
                await child.stdin.wait_closed()


async def _copy(reader: _Reader, writer: _Writer) -> None:
    while data := await reader.read(_BUFFER_SIZE):
        writer.write(data)
        await writer.drain()


async def _connected[T](
    process: SSHServerProcess[bytes], work: Awaitable[T], failed: asyncio.Future[None] | None = None
) -> T:
    task = asyncio.ensure_future(work)
    disconnected = asyncio.create_task(process.wait_closed())
    try:
        watches: set[asyncio.Future[T] | asyncio.Future[None]] = {task, disconnected}
        if failed is not None:
            watches.add(failed)
        done, _ = await asyncio.wait(watches, return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            raise ConnectionError("SSH channel disconnected")
        if failed in done:
            assert failed is not None
            failed.result()
        return task.result()
    finally:
        task.cancel()
        disconnected.cancel()
        await asyncio.gather(task, disconnected, return_exceptions=True)


async def _input(
    process: SSHServerProcess[bytes],
    send: Callable[[bytes], Awaitable[None]],
    master: int | None = None,
) -> None:
    # Import only when an SSH session runs, keeping the SSH extra optional.
    from asyncssh import SignalReceived, TerminalSizeChanged

    while True:
        try:
            data = await process.stdin.read(_BUFFER_SIZE)
        except SignalReceived as exc:
            # Events queued before callback installation still arrive here.
            if "SIG" + exc.signal in signal.Signals.__members__:
                process.signal_received(exc.signal)
            continue
        except TerminalSizeChanged as exc:
            if master is not None:
                process.terminal_size_changed(*exc.term_size)
            continue
        if not data:
            if master is not None:
                attrs = termios.tcgetattr(master)
                if attrs[3] & termios.ICANON:
                    # Flush a partial canonical line, then signal EOF on an empty line.
                    await send(attrs[6][termios.VEOF] * 2)
                else:
                    # No EOF byte exists in raw mode. End this terminal session
                    # with the same HUP/grace/KILL policy used on disconnect.
                    raise _RawEOFError
            return
        try:
            await send(data)
        except (BrokenPipeError, ConnectionResetError):
            # A child may stop reading while it still has output to deliver.
            return


async def _supervise(
    process: SSHServerProcess[bytes],
    waiter: asyncio.Task[int],
    stdin: asyncio.Task[None],
    outputs: list[asyncio.Task[None]],
    failed: asyncio.Future[None],
) -> int:
    disconnected = asyncio.create_task(process.wait_closed())
    relays = [stdin, *outputs, disconnected]
    pending: set[asyncio.Future[int] | asyncio.Future[None]] = {waiter, *relays, failed}
    try:
        while True:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise ConnectionError("SSH channel disconnected")
            for task in done:
                task.result()
            if waiter.done() and all(task.done() for task in outputs):
                return waiter.result()
    finally:
        for task in relays:
            task.cancel()
        await asyncio.gather(*relays, return_exceptions=True)


async def _write_pty(fd: int, data: bytes) -> None:
    loop = asyncio.get_running_loop()
    while data:
        try:
            count = os.write(fd, data)
        except InterruptedError:
            continue
        except BlockingIOError:
            ready: asyncio.Future[None] = loop.create_future()

            def writable(ready: asyncio.Future[None] = ready) -> None:
                if not ready.done():
                    ready.set_result(None)

            loop.add_writer(fd, writable)
            try:
                await ready
            finally:
                loop.remove_writer(fd)
        else:
            data = data[count:]


class _PTYOutput(io.BufferedReader):
    """Observe AsyncSSH closing its read transport after all PTY output drains."""

    def __init__(self, file: io.FileIO):
        self.finished = asyncio.Event()
        super().__init__(file)

    def close(self) -> None:
        try:
            super().close()
        finally:
            self.finished.set()

    async def wait_closed(self) -> None:
        await self.finished.wait()


def _duplicate(master: int, mode: str) -> io.FileIO:
    fd = os.dup(master)
    try:
        # Binary, unbuffered fdopen returns a FileIO in both modes used here.
        return cast(io.FileIO, os.fdopen(fd, mode, buffering=0))
    except BaseException:
        os.close(fd)
        raise


def _waitpid(pid: int) -> int:
    while True:
        try:
            return os.waitpid(pid, 0)[1]
        except InterruptedError:
            continue


async def _run_pty(process: SSHServerProcess[bytes], spec: ChildSpec) -> int:
    term = validated_term(process.term_type)
    size = _window_size(process.term_size)
    # Build the child's env before forking: pty.fork() runs in a process that
    # other sessions keep multi-threaded (asyncio.to_thread waiters, the
    # default executor), and any Python code the child ran between fork and
    # execve — even os.environ.copy() — could deadlock on a lock another
    # thread held at fork time. The child must do nothing but
    # chdir + execve + _exit.
    env = os.environ.copy()
    env["TERM"] = term
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(spec.cwd)
            os.execvpe(spec.argv[0], spec.argv, env)
        finally:
            # Never unwind into the inherited server loop in a forked child.
            os._exit(127)

    waiter = asyncio.create_task(asyncio.to_thread(_waitpid, pid))
    with ExitStack() as resources:
        resources.callback(os.close, master)
        failed = resources.enter_context(_controls(process, pid, master))
        redirect_started = False
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
            os.set_blocking(master, False)
            writer = resources.enter_context(_duplicate(master, "wb"))
            raw_reader = resources.enter_context(_duplicate(master, "rb"))
            reader = resources.enter_context(_PTYOutput(raw_reader))
            redirect_started = True
            await _connected(
                process,
                process.redirect(stdout=reader, bufsize=_BUFFER_SIZE, send_eof=False),
                failed,
            )

            async def send(data: bytes) -> None:
                await _write_pty(writer.fileno(), data)

            async def input_pty() -> None:
                try:
                    await _input(process, send, master)
                except OSError as exc:
                    # Linux reports EIO once the slave side has closed.
                    if exc.errno != errno.EIO:
                        raise

            stdin = asyncio.create_task(input_pty())
            output = asyncio.create_task(reader.wait_closed())
            return await _supervise(process, waiter, stdin, [output], failed)
        except _RawEOFError:
            await _finish(asyncio.create_task(_terminate(pid, waiter)))
            await _connected(process, reader.wait_closed(), failed)
            return waiter.result()
        except BaseException:
            await _finish(asyncio.create_task(_terminate(pid, waiter)))
            raise
        finally:
            if redirect_started:

                async def detach() -> None:
                    with suppress(Exception):
                        await process.redirect(stdout=asyncio.subprocess.PIPE)

                await _finish(asyncio.create_task(detach()))


async def _run_pipes(process: SSHServerProcess[bytes], spec: ChildSpec) -> int:
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=spec.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            start_new_session=True,
        )
    )
    try:
        child = await asyncio.shield(spawn)
    except asyncio.CancelledError:

        async def abandon_spawn() -> None:
            child = await spawn
            with _controls(process, child.pid):
                await _cleanup_pipes(child, asyncio.create_task(child.wait()))

        await _finish(asyncio.create_task(abandon_spawn()))
        raise
    waiter = asyncio.create_task(child.wait())
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    child_stdin = child.stdin

    async def send(data: bytes) -> None:
        child_stdin.write(data)
        await child_stdin.drain()

    async def input_pipe() -> None:
        try:
            await _input(process, send)
        finally:
            child_stdin.close()

    with _controls(process, child.pid) as failed:
        try:
            stdin = asyncio.create_task(input_pipe())
            outputs = [
                asyncio.create_task(_copy(child.stdout, process.stdout)),
                asyncio.create_task(_copy(child.stderr, process.stderr)),
            ]
            return await _supervise(process, waiter, stdin, outputs, failed)
        except BaseException:
            await _finish(asyncio.create_task(_cleanup_pipes(child, waiter)))
            raise
        finally:
            child_stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await child_stdin.wait_closed()


async def run_child(process: SSHServerProcess[bytes], spec: ChildSpec) -> None:
    """Run an argv directly, owning all child resources until exit or disconnect."""
    if process.term_type is not None:
        status, sig = decode_wait_status(await _run_pty(process, spec))
    else:
        if spec.requires_pty:
            raise PTYError("This entry point requires a PTY; retry with ssh -t.")
        result = await _run_pipes(process, spec)
        status, sig = (result, None) if result >= 0 else (None, signal.Signals(-result))
    if sig is not None:
        process.exit_with_signal(sig.name.removeprefix("SIG"))
    else:
        assert status is not None
        process.exit(status)
