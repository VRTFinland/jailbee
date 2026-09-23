"""Process-boundary tests; no children or real terminals are started."""

from __future__ import annotations

import asyncio
import errno
import io
import signal
import struct
import termios
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from asyncssh import SignalReceived, SSHReader, SSHServerProcess, SSHWriter, TerminalSizeChanged

from jailbee.remote_ssh import pty as runner
from jailbee.remote_ssh.pty import (
    ChildSpec,
    PTYError,
    decode_wait_status,
    run_child,
    validated_term,
)


class Reader:
    def __init__(self, data=b"", *, pending=False, error=None):
        self.data = data
        self.pending = pending
        self.error = error
        self.sizes = []
        self.cancelled = False

    async def read(self, size):
        self.sizes.append(size)
        assert 0 < size <= 65536
        if self.error:
            raise self.error
        if self.pending:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.drains = 0
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        self.drains += 1

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class SSHProcess:
    def __init__(self, term=None):
        self.term_type = term
        self.term_size = (80, 24, 0, 0)
        self.stdin = Reader()
        self.stdout = Writer()
        self.stderr = Writer()
        self.exit = Mock()
        self.exit_with_signal = Mock()
        self.signal_received = Mock()
        self.terminal_size_changed = Mock()
        self.disconnected = asyncio.Event()
        self.redirected = asyncio.Event()

        async def redirect(**kwargs):
            self.redirected.set()
            if kwargs["stdout"] != asyncio.subprocess.PIPE:
                kwargs["stdout"].close()

        self.redirect = AsyncMock(side_effect=redirect)

    @property
    def env(self):
        pytest.fail("SSH environment must never be consumed")

    async def wait_closed(self):
        await self.disconnected.wait()


@pytest.fixture
def spec(tmp_path):
    return ChildSpec(("/usr/bin/jailbee", "ls", "space ; $(literal)"), tmp_path, False)


@pytest.fixture
def boundary(monkeypatch):
    fork = Mock(return_value=(4321, 90))
    monkeypatch.setattr(runner.pty, "fork", fork)
    monkeypatch.setattr(runner.os, "waitpid", Mock(return_value=(4321, 7 << 8)))
    killpg = Mock()
    probe = Mock(side_effect=ProcessLookupError)

    def signal_group(pid, sig):
        if sig == 0:
            return probe(pid)
        return killpg(pid, sig)

    monkeypatch.setattr(runner.os, "killpg", signal_group)
    monkeypatch.setattr(runner.os, "close", Mock())
    monkeypatch.setattr(runner.os, "dup", Mock(side_effect=[91, 92]))
    monkeypatch.setattr(runner.os, "set_blocking", Mock())
    monkeypatch.setattr(runner.os, "write", Mock(side_effect=lambda fd, data: len(data)))
    attrs = [0, 0, 0, termios.ICANON, 0, 0, [b"\0"] * termios.NCCS]
    attrs[6][termios.VEOF] = b"\x04"
    monkeypatch.setattr(runner.termios, "tcgetattr", Mock(return_value=attrs))
    files = []

    def fdopen(fd, mode, buffering=0):
        file = io.BytesIO()
        file.mode = mode
        file.fileno = Mock(return_value=fd)
        files.append(file)
        return file

    monkeypatch.setattr(runner.os, "fdopen", Mock(side_effect=fdopen))
    monkeypatch.setattr(runner.fcntl, "ioctl", Mock())
    create = AsyncMock()
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", create)
    return SimpleNamespace(fork=fork, killpg=killpg, probe=probe, files=files, create=create)


def test_term_accepts_a_conservative_terminal_name():
    assert validated_term("xterm-256color") == "xterm-256color"


@pytest.mark.parametrize("term", [None, "", "xterm\nBAD=1", "x" * 65, "../xterm"])
def test_term_rejects_untrusted_values(term):
    with pytest.raises(PTYError):
        validated_term(term)


@pytest.mark.parametrize("status,want", [(7 << 8, (7, None)), (15, (None, signal.SIGTERM))])
def test_decode_wait_status(status, want):
    assert decode_wait_status(status) == want


def test_decode_rejects_stopped_child():
    with pytest.raises(PTYError):
        decode_wait_status((signal.SIGSTOP << 8) | 0x7F)


def test_required_pty_is_rejected_before_spawn(spec, boundary):
    with pytest.raises(PTYError, match="PTY"):
        asyncio.run(run_child(SSHProcess(), ChildSpec(spec.argv, spec.cwd, True)))
    boundary.fork.assert_not_called()
    boundary.create.assert_not_called()


@pytest.mark.parametrize("term", ["", "../bad", "x\nEVIL=1"])
def test_invalid_requested_terminal_never_spawns(term, spec, boundary):
    with pytest.raises(PTYError):
        asyncio.run(run_child(SSHProcess(term), spec))
    boundary.fork.assert_not_called()
    boundary.create.assert_not_called()


@pytest.mark.parametrize(
    "size",
    [
        (80, -1, 0, 0),
        (65536, 24, 0, 0),
        (80, 24, -1, 0),
        (80, 24, 0, 65536),
        (True, 24, 0, 0),
        (80.5, 24, 0, 0),
        (80, 24),
    ],
)
def test_invalid_dimensions_never_spawn(size, spec, boundary):
    process = SSHProcess("xterm")
    process.term_size = size
    with pytest.raises(PTYError):
        asyncio.run(run_child(process, spec))
    boundary.fork.assert_not_called()


def test_optional_command_uses_requested_pty_and_merges_output(spec, boundary):
    process = SSHProcess("xterm-256color")
    process.term_size = (132, 43, 800, 600)
    asyncio.run(run_child(process, spec))
    boundary.fork.assert_called_once_with()
    boundary.create.assert_not_called()
    runner.fcntl.ioctl.assert_called_once_with(
        90, termios.TIOCSWINSZ, struct.pack("HHHH", 43, 132, 800, 600)
    )
    redirected = process.redirect.call_args_list[0].kwargs
    assert redirected["stdout"].mode == "rb"
    assert redirected.get("stdin") is None
    assert len(boundary.files) == 2
    assert boundary.files[0].mode == "wb"
    assert redirected.get("stderr") is None
    assert 0 < redirected["bufsize"] <= 65536
    assert redirected["send_eof"] is False
    runner.os.waitpid.assert_called_once_with(4321, 0)
    process.exit.assert_called_once_with(7)
    assert all(file.closed for file in boundary.files)
    runner.os.close.assert_called_once_with(90)


def test_pty_signal_status_reaches_ssh(spec, boundary):
    runner.os.waitpid.return_value = (4321, signal.SIGTERM)
    process = SSHProcess("xterm")
    asyncio.run(run_child(process, spec))
    process.exit_with_signal.assert_called_once_with("TERM")
    process.exit.assert_not_called()


class ChildExited(BaseException):
    pass


def test_fork_child_executes_literal_argv_cwd_and_only_trusted_env(spec, boundary, monkeypatch):
    boundary.fork.return_value = (0, -1)
    monkeypatch.setattr(runner.os, "environ", {"PATH": "/bin", "TERM": "old"})
    chdir = Mock()
    execute = Mock(side_effect=OSError("exec failed"))
    leave = Mock(side_effect=ChildExited)
    monkeypatch.setattr(runner.os, "chdir", chdir)
    monkeypatch.setattr(runner.os, "execvpe", execute)
    monkeypatch.setattr(runner.os, "_exit", leave)
    with pytest.raises(ChildExited):
        asyncio.run(run_child(SSHProcess("xterm"), spec))
    chdir.assert_called_once_with(spec.cwd)
    execute.assert_called_once_with(spec.argv[0], spec.argv, {"PATH": "/bin", "TERM": "xterm"})
    leave.assert_called_once_with(127)
    assert runner.os.environ["TERM"] == "old"


def test_child_env_is_built_before_fork_not_in_the_child(spec, boundary, monkeypatch):
    """Regression for final-review finding M1.

    `pty.fork()` runs in a multi-threaded process (other sessions' waitpid
    threads, the default executor). If the child then runs Python code
    (`os.environ.copy()`, dict assignment) before `execve`, it can deadlock
    on a lock another thread held at fork time. The child must do nothing
    but chdir + execve + _exit; env must already exist by the time fork() is
    called.
    """
    prepared = False

    class RecordingEnviron(dict):
        def copy(self):
            nonlocal prepared
            prepared = True
            return dict(self)

    monkeypatch.setattr(runner.os, "environ", RecordingEnviron({"PATH": "/bin"}))

    def fork():
        assert prepared, "env must be built before pty.fork(), not in the forked child"
        return (4321, 90)

    boundary.fork.side_effect = fork

    asyncio.run(run_child(SSHProcess("xterm"), spec))

    boundary.fork.assert_called_once_with()
    runner.os.waitpid.assert_called_once_with(4321, 0)


def test_redirect_failure_hangs_up_reaps_and_closes_every_fd(spec, boundary):
    process = SSHProcess("xterm")
    process.redirect.side_effect = RuntimeError("redirect failed")
    with pytest.raises(RuntimeError, match="redirect failed"):
        asyncio.run(run_child(process, spec))
    boundary.killpg.assert_called_once_with(4321, signal.SIGHUP)
    runner.os.waitpid.assert_called_once_with(4321, 0)
    assert all(file.closed for file in boundary.files)
    runner.os.close.assert_called_once_with(90)


def test_second_dup_failure_closes_first_file_and_master(spec, boundary):
    runner.os.dup.side_effect = [91, OSError("no descriptors")]
    with pytest.raises(OSError, match="no descriptors"):
        asyncio.run(run_child(SSHProcess("xterm"), spec))
    assert len(boundary.files) == 1 and boundary.files[0].closed
    runner.os.close.assert_called_once_with(90)
    boundary.killpg.assert_called_once_with(4321, signal.SIGHUP)


def test_fdopen_failure_closes_the_unwrapped_duplicate(spec, boundary):
    runner.os.fdopen.side_effect = OSError("cannot wrap")
    with pytest.raises(OSError, match="cannot wrap"):
        asyncio.run(run_child(SSHProcess("xterm"), spec))
    assert call(91) in runner.os.close.call_args_list
    assert call(90) in runner.os.close.call_args_list


@pytest.mark.parametrize("disconnect", [False, True])
def test_pty_cancellation_or_disconnect_hangs_up_and_reaps(spec, boundary, monkeypatch, disconnect):
    async def scenario():
        reaped = asyncio.Event()

        async def wait_in_thread(function, *args):
            await reaped.wait()
            return function(*args)

        monkeypatch.setattr(runner.asyncio, "to_thread", wait_in_thread)
        boundary.killpg.side_effect = lambda *_: reaped.set()
        process = SSHProcess("xterm")
        task = asyncio.create_task(run_child(process, spec))
        await process.redirected.wait()
        if disconnect:
            process.disconnected.set()
        else:
            task.cancel()
        with pytest.raises((asyncio.CancelledError, ConnectionError)):
            await task
        assert reaped.is_set()
        process.exit.assert_not_called()

    asyncio.run(scenario())
    boundary.killpg.assert_called_once_with(4321, signal.SIGHUP)
    runner.os.waitpid.assert_called_once_with(4321, 0)
    assert all(file.closed for file in boundary.files)


def pipe_child(*, status=0, pending=False):
    child = SimpleNamespace(
        pid=4321,
        stdin=Writer(),
        stdout=Reader(b"output"),
        stderr=Reader(b"error"),
        returncode=None,
    )
    child.finished = asyncio.Event()

    async def wait():
        if pending:
            await child.finished.wait()
        child.returncode = status
        return status

    child.wait = AsyncMock(side_effect=wait)
    return child


def test_pipe_argv_env_and_separate_bounded_streams(spec, boundary, monkeypatch):
    monkeypatch.setattr(runner.os, "environ", {"PATH": "/bin"})
    process = SSHProcess()
    process.stdin = Reader(b"input" * 20000)
    child = pipe_child(status=9)
    boundary.create.return_value = child
    asyncio.run(run_child(process, spec))
    boundary.create.assert_awaited_once_with(
        *spec.argv,
        cwd=spec.cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/bin"},
        start_new_session=True,
    )
    assert process.stdout.data == b"output"
    assert process.stderr.data == b"error"
    assert child.stdin.data == b"input" * 20000
    assert child.stdin.closed
    assert child.stdin.drains >= 2
    assert process.stdout.drains and process.stderr.drains
    process.exit.assert_called_once_with(9)
    boundary.fork.assert_not_called()


def test_child_exit_does_not_wait_for_client_stdin_eof(spec, boundary):
    process = SSHProcess()
    process.stdin = Reader(pending=True)
    child = pipe_child(status=-signal.SIGINT)
    boundary.create.return_value = child
    asyncio.run(asyncio.wait_for(run_child(process, spec), 1))
    assert process.stdin.cancelled
    assert child.stdin.closed
    process.exit_with_signal.assert_called_once_with("INT")


def test_client_stdin_eof_closes_pipe_without_killing_child(spec, boundary):
    child = pipe_child(pending=True)
    boundary.create.return_value = child
    process = SSHProcess()

    async def scenario():
        task = asyncio.create_task(run_child(process, spec))
        while not child.stdin.closed:
            await asyncio.sleep(0)
        assert not task.done()
        child.finished.set()
        await task

    asyncio.run(scenario())
    boundary.killpg.assert_not_called()


def test_pipe_stream_failure_terminates_and_reaps(spec, boundary):
    child = pipe_child(pending=True)
    boundary.create.return_value = child
    process = SSHProcess()
    process.stdin = Reader(error=ConnectionResetError("gone"))
    boundary.killpg.side_effect = lambda *_: child.finished.set()
    with pytest.raises(ConnectionResetError):
        asyncio.run(run_child(process, spec))
    boundary.killpg.assert_called_once_with(4321, signal.SIGHUP)
    assert child.returncode == 0
    assert child.stdin.closed


@pytest.mark.parametrize("pty_requested", [False, True])
def test_cleanup_timeout_kills_group_then_reaps(spec, boundary, monkeypatch, pty_requested):
    async def scenario():
        reaped = asyncio.Event()
        process = SSHProcess("xterm" if pty_requested else None)
        child = pipe_child(pending=True)
        boundary.create.return_value = child

        async def wait_in_thread(function, *args):
            await reaped.wait()
            return function(*args)

        monkeypatch.setattr(runner.asyncio, "to_thread", wait_in_thread)
        real_wait_for = asyncio.wait_for

        async def short_wait(awaitable, timeout):
            assert timeout == 2
            return await real_wait_for(awaitable, 0.01)

        monkeypatch.setattr(runner.asyncio, "wait_for", short_wait)

        def kill(_pid, sig):
            if sig == signal.SIGKILL:
                reaped.set()
                child.finished.set()

        boundary.killpg.side_effect = kill
        task = asyncio.create_task(run_child(process, spec))
        while not (process.redirected.is_set() if pty_requested else child.wait.called):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reaped.is_set()

    asyncio.run(scenario())
    assert boundary.killpg.call_args_list == [call(4321, signal.SIGHUP), call(4321, signal.SIGKILL)]


class EventReader(Reader):
    def __init__(self, events):
        super().__init__()
        self.events = iter(events)

    async def read(self, size):
        assert 0 < size <= 65536
        event = next(self.events, b"")
        if isinstance(event, Exception):
            raise event
        return event


@pytest.mark.parametrize("pty_requested", [False, True])
def test_real_ssh_signal_events_reach_child_group(spec, boundary, pty_requested):
    process = SSHProcess("xterm" if pty_requested else None)
    process.stdin = EventReader([SignalReceived("INT"), SignalReceived("not-a-signal")])
    boundary.create.return_value = pipe_child()
    asyncio.run(run_child(process, spec))
    boundary.killpg.assert_called_once_with(4321, signal.SIGINT)


def test_real_resize_event_applies_rows_columns_pixels(spec, boundary):
    process = SSHProcess("xterm")
    process.stdin = EventReader([TerminalSizeChanged(100, 40, 900, 700), b"typed"])
    asyncio.run(run_child(process, spec))
    assert call(90, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 900, 700)) in (
        runner.fcntl.ioctl.call_args_list
    )
    assert runner.os.write.call_args_list == [call(91, b"typed"), call(91, b"\x04\x04")]


def test_pty_eof_uses_configured_canonical_eof_character(spec, boundary):
    attrs = [0, 0, 0, termios.ICANON, 0, 0, [b"\0"] * termios.NCCS]
    attrs[6][termios.VEOF] = b"\x04"
    runner.termios.tcgetattr.return_value = attrs
    asyncio.run(run_child(SSHProcess("xterm"), spec))
    runner.os.write.assert_called_once_with(91, b"\x04\x04")


def test_pty_waits_for_redirect_output_before_reporting_exit(spec, boundary):
    process = SSHProcess("xterm")

    async def scenario():
        async def redirect(**kwargs):
            process.redirected.set()

        process.redirect.side_effect = redirect
        task = asyncio.create_task(run_child(process, spec))
        await process.redirected.wait()
        for _ in range(10):
            await asyncio.sleep(0)
        process.exit.assert_not_called()
        process.redirect.call_args.kwargs["stdout"].close()
        await task
        process.exit.assert_called_once_with(7)

    asyncio.run(scenario())


def test_disconnect_during_redirect_setup_terminates_child(spec, boundary):
    process = SSHProcess("xterm")

    async def scenario():
        async def redirect(**kwargs):
            if kwargs["stdout"] == asyncio.subprocess.PIPE:
                return
            process.redirected.set()
            await asyncio.Future()

        process.redirect.side_effect = redirect
        task = asyncio.create_task(run_child(process, spec))
        await process.redirected.wait()
        process.disconnected.set()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, 0.1)

    asyncio.run(scenario())
    boundary.killpg.assert_called_once_with(4321, signal.SIGHUP)
    assert all(file.closed for file in boundary.files)


def test_cancellation_during_pipe_spawn_still_cleans_child_group(spec, boundary):
    async def scenario():
        spawned = asyncio.Event()
        release = asyncio.Event()
        child = pipe_child(pending=True)

        async def create(*args, **kwargs):
            spawned.set()
            await release.wait()
            return child

        boundary.create.side_effect = create
        boundary.killpg.side_effect = lambda *_: child.finished.set()
        task = asyncio.create_task(run_child(SSHProcess(), spec))
        await spawned.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.stdin.closed
        assert child.returncode == 0

    asyncio.run(scenario())
    boundary.killpg.assert_called_once_with(4321, signal.SIGHUP)


def test_child_closing_stdin_early_still_delivers_output_and_status(spec, boundary):
    child = pipe_child(status=3)
    child.stdin.drain = AsyncMock(side_effect=BrokenPipeError)
    boundary.create.return_value = child
    process = SSHProcess()
    process.stdin = Reader(b"input")
    asyncio.run(run_child(process, spec))
    assert process.stdout.data == b"output"
    process.exit.assert_called_once_with(3)
    boundary.killpg.assert_not_called()


def test_pty_eio_after_child_exit_does_not_replace_exit_status(spec, boundary):
    runner.termios.tcgetattr.side_effect = OSError(errno.EIO, "terminal closed")
    process = SSHProcess("xterm")
    asyncio.run(run_child(process, spec))
    process.exit.assert_called_once_with(7)
    boundary.killpg.assert_not_called()


def test_pipe_disconnect_drains_blocked_output_before_reaping(spec, boundary):
    async def scenario():
        process = SSHProcess()
        process.stdin = Reader(pending=True)
        child = pipe_child(pending=True)
        drained = asyncio.Event()
        writing = asyncio.Event()
        killed = asyncio.Event()

        class Output(Reader):
            async def read(self, size):
                if self.data:
                    return await super().read(size)
                await killed.wait()
                drained.set()
                return b""

        async def drain():
            writing.set()
            await asyncio.Future()

        async def wait():
            await killed.wait()
            await drained.wait()
            child.returncode = -signal.SIGHUP
            return child.returncode

        child.stdout = Output(b"pending output")
        child.wait.side_effect = wait
        process.stdout.drain = drain
        boundary.create.return_value = child
        boundary.killpg.side_effect = lambda *_: killed.set()
        task = asyncio.create_task(run_child(process, spec))
        await writing.wait()
        process.disconnected.set()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, 0.1)
        assert drained.is_set()

    asyncio.run(scenario())


def test_cleanup_detaches_pty_reader_before_closing_descriptors(spec, boundary):
    process = SSHProcess("xterm")

    async def redirect(**kwargs):
        if kwargs["stdout"] != asyncio.subprocess.PIPE:
            process.redirected.set()
            raise RuntimeError("failed after attaching reader")
        assert not any(file.closed for file in boundary.files)

    process.redirect.side_effect = redirect
    with pytest.raises(RuntimeError, match="attaching reader"):
        asyncio.run(run_child(process, spec))
    assert process.redirect.call_args == call(stdout=asyncio.subprocess.PIPE)


def actual_process(term=None):
    """Use real AsyncSSH callback-to-stream dispatch with only the channel mocked."""
    channel = Mock()
    channel.get_encoding.return_value = (None, "strict")
    channel.get_loop.return_value = asyncio.get_running_loop()
    channel.get_recv_window.return_value = 2097152
    channel.get_read_datatypes.return_value = [None]
    channel.get_write_datatypes.return_value = [None, 1]
    channel.get_terminal_type.return_value = term
    channel.get_terminal_size.return_value = (80, 24, 0, 0)
    process = SSHServerProcess(lambda _: None, None, 3, False)
    process.connection_made(channel)
    process._start_process(
        SSHReader(process, channel), SSHWriter(process, channel), SSHWriter(process, channel, 1)
    )
    closed = asyncio.Event()
    channel.wait_closed = closed.wait
    return process, channel


@pytest.mark.parametrize("state", ["eof", "closed", "blocked"])
@pytest.mark.parametrize("terminal", [None, "xterm"])
def test_live_control_callbacks_outlast_stdin(spec, boundary, monkeypatch, state, terminal):
    async def scenario():
        process, _ = actual_process(terminal)
        original_signal = process.signal_received
        original_resize = process.terminal_size_changed
        child = pipe_child(pending=True)
        boundary.create.return_value = child
        ready = asyncio.Event()
        done = asyncio.Event()

        async def wait_in_thread(function, *args):
            await done.wait()
            return function(*args)

        monkeypatch.setattr(runner.asyncio, "to_thread", wait_in_thread)

        async def redirect(**kwargs):
            if kwargs["stdout"] != asyncio.subprocess.PIPE:
                kwargs["stdout"].close()

        process.redirect = AsyncMock(side_effect=redirect)

        async def drain():
            ready.set()
            if state == "closed":
                raise BrokenPipeError
            await asyncio.Future()

        child.stdin.drain = drain
        if terminal:

            def write(fd, data):
                ready.set()
                if state == "closed":
                    raise BrokenPipeError
                raise BlockingIOError

            monkeypatch.setattr(runner.os, "write", write)
            monkeypatch.setattr(asyncio.get_running_loop(), "add_writer", Mock())
            monkeypatch.setattr(asyncio.get_running_loop(), "remove_writer", Mock())
        if state == "eof":
            process.eof_received()
        else:
            process.data_received(b"bytes", None)
        task = asyncio.create_task(run_child(process, spec))
        try:
            if state != "eof":
                await ready.wait()
            else:
                while not (child.wait.called if not terminal else process.redirect.called):
                    await asyncio.sleep(0)
            for _ in range(5):
                await asyncio.sleep(0)
            process.signal_received("INT")
            process.terminal_size_changed(111, 41, 0, 0)
            assert call(4321, signal.SIGINT) in boundary.killpg.call_args_list
            if terminal:
                assert call(90, termios.TIOCSWINSZ, struct.pack("HHHH", 41, 111, 0, 0)) in (
                    runner.fcntl.ioctl.call_args_list
                )
        finally:
            child.finished.set()
            done.set()
            await task
        assert process.signal_received == original_signal
        assert process.terminal_size_changed == original_resize

    asyncio.run(scenario())


def test_raw_pty_eof_terminates_and_reaps(spec, boundary, monkeypatch):
    async def scenario():
        reaped = asyncio.Event()

        async def wait_in_thread(function, *args):
            await reaped.wait()
            return function(*args)

        monkeypatch.setattr(runner.asyncio, "to_thread", wait_in_thread)
        boundary.killpg.side_effect = lambda *_: reaped.set()
        process = SSHProcess("xterm")
        runner.termios.tcgetattr.return_value[3] = 0
        task = asyncio.create_task(run_child(process, spec))
        await process.redirected.wait()
        try:
            await asyncio.wait_for(reaped.wait(), 0.1)
            assert call(4321, signal.SIGHUP) in boundary.killpg.call_args_list
        finally:
            reaped.set()
            await task
        runner.os.waitpid.assert_called_once_with(4321, 0)

    asyncio.run(scenario())


def test_cleanup_kills_descendants_after_leader_is_reaped(spec, boundary, monkeypatch):
    process = SSHProcess("xterm")
    process.redirect.side_effect = RuntimeError("setup failed")
    alive = True

    def kill(pid, sig):
        nonlocal alive
        if sig == signal.SIGKILL:
            alive = False

    boundary.killpg.side_effect = kill
    boundary.probe.side_effect = None
    real_wait_for = asyncio.wait_for

    async def short_wait(awaitable, timeout):
        assert timeout == 2
        return await real_wait_for(awaitable, 0.01)

    monkeypatch.setattr(runner.asyncio, "wait_for", short_wait)
    with pytest.raises(RuntimeError, match="setup failed"):
        asyncio.run(run_child(process, spec))
    boundary.probe.assert_called_with(4321)
    assert call(4321, signal.SIGKILL) in boundary.killpg.call_args_list
    assert not alive
    runner.os.waitpid.assert_called_once_with(4321, 0)


@pytest.mark.parametrize("initial", [(0, 0, 0, 0), (0, 32, 0, 0)])
def test_unspecified_initial_size_uses_terminal_defaults(spec, boundary, initial):
    process = SSHProcess("xterm")
    process.term_size = initial
    asyncio.run(run_child(process, spec))
    assert call(90, termios.TIOCSWINSZ, struct.pack("HHHH", initial[1] or 24, 80, 0, 0)) in (
        runner.fcntl.ioctl.call_args_list
    )


def test_unspecified_resize_dimensions_preserve_current_size(spec, boundary):
    process = SSHProcess("xterm")
    process.stdin = EventReader(
        [
            TerminalSizeChanged(132, 43, 800, 600),
            TerminalSizeChanged(0, 0, 0, 0),
            TerminalSizeChanged(0, 50, 0, 0),
        ]
    )
    asyncio.run(run_child(process, spec))
    assert runner.fcntl.ioctl.call_args_list[-1] == call(
        90, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 132, 800, 600)
    )


@pytest.mark.parametrize("outcome", ["cancel", "redirect_error", "invalid_resize"])
def test_pty_callbacks_survive_cleanup_and_restore_after_failure(
    spec, boundary, monkeypatch, outcome
):
    async def scenario():
        process, _ = actual_process("xterm")
        original_signal = Mock(wraps=process.signal_received)
        process.signal_received = original_signal
        original_resize = process.terminal_size_changed
        entered = asyncio.Event()
        reaped = asyncio.Event()

        async def wait_in_thread(function, *args):
            await reaped.wait()
            return function(*args)

        monkeypatch.setattr(runner.asyncio, "to_thread", wait_in_thread)

        def kill(pid, sig):
            if sig == signal.SIGHUP:
                assert process.signal_received != original_signal
                assert process.terminal_size_changed != original_resize
                process.signal_received("USR1")
                reaped.set()

        boundary.killpg.side_effect = kill

        async def redirect(**kwargs):
            if kwargs["stdout"] == asyncio.subprocess.PIPE:
                return
            assert process.signal_received != original_signal
            assert process.terminal_size_changed != original_resize
            process.signal_received("unknown")
            if outcome == "redirect_error":
                raise OSError("redirect failed")
            if outcome == "invalid_resize":
                process.terminal_size_changed(-1, 24, 0, 0)
            entered.set()
            kwargs["stdout"].close()

        process.redirect = AsyncMock(side_effect=redirect)
        task = asyncio.create_task(run_child(process, spec))
        if outcome == "cancel":
            await entered.wait()
            task.cancel()
        expected = {
            "cancel": asyncio.CancelledError,
            "redirect_error": OSError,
            "invalid_resize": PTYError,
        }[outcome]
        with pytest.raises(expected):
            await task
        original_signal.assert_called_once_with("unknown")
        assert call(4321, signal.SIGUSR1) in boundary.killpg.call_args_list
        assert process.signal_received == original_signal
        assert process.terminal_size_changed == original_resize
        assert all(file.closed for file in boundary.files)

    asyncio.run(scenario())


def test_pipe_callbacks_installed_before_write_and_restored_after_cancel(spec, boundary):
    async def scenario():
        process, _ = actual_process()
        original_signal = process.signal_received
        original_resize = process.terminal_size_changed
        child = pipe_child(pending=True)
        boundary.create.return_value = child
        writing = asyncio.Event()

        async def drain():
            assert process.signal_received != original_signal
            writing.set()
            await asyncio.Future()

        def kill(pid, sig):
            if sig == signal.SIGHUP:
                process.signal_received("USR1")
                child.finished.set()

        child.stdin.drain = drain
        boundary.killpg.side_effect = kill
        process.data_received(b"bytes", None)
        task = asyncio.create_task(run_child(process, spec))
        await writing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert call(4321, signal.SIGUSR1) in boundary.killpg.call_args_list
        assert process.signal_received == original_signal
        assert process.terminal_size_changed == original_resize
        assert child.stdin.closed

    asyncio.run(scenario())


def test_group_probe_errors_are_not_treated_as_successful_cleanup(spec, boundary):
    process = SSHProcess("xterm")
    process.redirect.side_effect = RuntimeError("redirect failed")
    boundary.probe.side_effect = PermissionError("group probe denied")
    with pytest.raises(PermissionError, match="group probe denied"):
        asyncio.run(run_child(process, spec))
    runner.os.waitpid.assert_called_once_with(4321, 0)
    assert all(file.closed for file in boundary.files)


def test_raw_pty_eof_escalates_when_child_ignores_hup(spec, boundary, monkeypatch):
    async def scenario():
        reaped = asyncio.Event()

        async def wait_in_thread(function, *args):
            await reaped.wait()
            return function(*args)

        monkeypatch.setattr(runner.asyncio, "to_thread", wait_in_thread)
        real_wait_for = asyncio.wait_for

        async def short_wait(awaitable, timeout):
            assert timeout == 2
            return await real_wait_for(awaitable, 0.01)

        monkeypatch.setattr(runner.asyncio, "wait_for", short_wait)

        def kill(pid, sig):
            if sig == signal.SIGKILL:
                reaped.set()

        boundary.killpg.side_effect = kill
        runner.termios.tcgetattr.return_value[3] = 0
        runner.os.waitpid.return_value = (4321, signal.SIGKILL)
        process = SSHProcess("xterm")
        await run_child(process, spec)
        process.exit_with_signal.assert_called_once_with("KILL")

    asyncio.run(scenario())
    assert boundary.killpg.call_args_list == [call(4321, signal.SIGHUP), call(4321, signal.SIGKILL)]
    runner.os.waitpid.assert_called_once_with(4321, 0)
