"""Container-side subprocess exception for service tmux and shell operations."""

import codecs
import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, cast


@dataclass(frozen=True)
class ProbeResult:
    code: int | None
    stdout: str
    stderr: str
    timed_out: bool


_PROBE_OUTPUT_LIMIT = 16 * 1024


def _decode_probe_output(output: bytearray) -> str:
    text = output.decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")[-_PROBE_OUTPUT_LIMIT:]
    return encoded.decode("utf-8", errors="ignore")


# The anchor publishes its identity before the service command starts. It stays
# in the pane's process group after the pane shell exits.
_ANCHOR = r"""import json, os, signal, sys, time
from pathlib import Path
for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, signal.SIG_IGN)
pid = os.getpid()
stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
data = {"boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid": pid, "start": stat[19], "pgid": int(stat[2]), "session": int(stat[3])}
path = Path(sys.argv[1])
tmp = path.with_name(path.name + "." + str(pid))
tmp.write_text(json.dumps(data))
os.replace(tmp, path)
while True:
    time.sleep(3600)
"""


def _control_lines(stream: IO[bytes], poll_interval: float | None) -> Generator[bytes | None]:
    if poll_interval is None:
        yield from stream
        return
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
        buffer = b""
        while True:
            if not selector.select(poll_interval):
                yield None
                continue
            try:
                chunk = os.read(stream.fileno(), 8192)
            except BlockingIOError:
                continue
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line
    finally:
        selector.close()


class LocalProc:
    """Execute container-local service operations."""

    def tmux(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)

    def follow_pane(
        self, target: str, lines: int, *, initial: bool = True, poll_interval: float | None = None
    ) -> Iterator[str]:
        pane_result = self.tmux(["display-message", "-p", "-t", target, "#{pane_id}"])
        pane = pane_result.stdout.strip()
        if pane_result.returncode or not re.fullmatch(r"%\d+", pane):
            raise RuntimeError(pane_result.stderr.strip() or "service window not found")
        process = subprocess.Popen(
            ["tmux", "-C", "attach-session", "-t", "autostart", "-f", "read-only,ignore-size"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdin is not None and process.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        snapshot: list[bytes] = []
        attached = False
        capture_id: bytes | None = None
        streaming = False
        protocol = _control_lines(process.stdout, poll_interval)
        try:
            for line in protocol:
                if line is None:
                    yield ""
                    continue
                line = line.rstrip(b"\n")
                if capture_id is not None and not streaming:
                    if line == b"%end " + capture_id:
                        streaming = True
                        yield b"".join(snapshot[-lines:]).decode("utf-8", errors="replace")
                    elif line == b"%error " + capture_id:
                        raise RuntimeError("failed to capture service window")
                    else:
                        snapshot.append(line + b"\n")
                    continue
                if line.startswith(b"%exit"):
                    break
                if not attached:
                    if line.startswith(b"%error "):
                        raise RuntimeError("failed to attach tmux control client")
                    if line.startswith(b"%end "):
                        attached = True
                        if initial:
                            process.stdin.write(
                                f"capture-pane -p -J -t {pane} -S -{lines}\n".encode()
                            )
                            process.stdin.flush()
                        else:
                            streaming = True
                    continue
                if not streaming:
                    if re.fullmatch(rb"%begin \d+ \d+ \d+", line):
                        capture_id = line[len(b"%begin ") :]
                    continue
                prefix = f"%output {pane} ".encode()
                if line.startswith(prefix):
                    raw = line[len(prefix) :]
                    raw = re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), raw)
                    text = decoder.decode(raw)
                    if text:
                        yield text
        finally:
            protocol.close()
            process.stdin.close()
            if process.poll() is not None:
                process.wait()
            else:
                process.terminate()
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass
            process.stdout.close()

    def run_probe(
        self,
        cmd: str,
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout: int,
    ) -> ProbeResult:
        process = subprocess.Popen(
            ["bash", "-lc", cmd],
            cwd=cwd,
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        outputs = {process.stdout: bytearray(), process.stderr: bytearray()}
        selector = selectors.DefaultSelector()
        for stream in outputs:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = process.poll() is None or bool(selector.get_map())
                    break
                for key, _ in selector.select(remaining):
                    stream = cast(IO[bytes], key.fileobj)
                    chunk = os.read(stream.fileno(), 8192)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    output = outputs[stream]
                    output.extend(chunk)
                    if len(output) > _PROBE_OUTPUT_LIMIT:
                        del output[: len(output) - _PROBE_OUTPUT_LIMIT]
            if timed_out:
                self.signal_group(process.pid, signal.SIGKILL)
                process.kill()
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                self.signal_group(process.pid, signal.SIGKILL)
                process.kill()
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            return ProbeResult(
                None if timed_out else process.returncode,
                _decode_probe_output(outputs[process.stdout]),
                _decode_probe_output(outputs[process.stderr]),
                timed_out,
            )
        except BaseException:
            self.signal_group(process.pid, signal.SIGKILL)
            process.kill()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

    def run_shell(
        self,
        cmd: str,
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout: float | None,
        capture: bool,
    ) -> int:
        process = subprocess.Popen(
            ["bash", "-lc", cmd],
            cwd=cwd,
            env={**os.environ, **env},
            stdout=subprocess.DEVNULL if capture else None,
            stderr=subprocess.DEVNULL if capture else None,
            start_new_session=True,
        )
        pgid = process.pid  # start_new_session makes the child its group leader.
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.signal_group(pgid, signal.SIGKILL)
            process.wait()
            return 124
        except BaseException:
            self.signal_group(pgid, signal.SIGKILL)
            process.wait()
            raise

    def killpg(self, pid: int, sig: int) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError:
            pass

    def service_command(self, command: str, identity: Path) -> str:
        python = shlex.quote(sys.executable)
        anchor = shlex.quote(_ANCHOR)
        path = shlex.quote(str(identity))
        run = shlex.quote(command)
        # Wait for publication, and fail instead of running untracked if it fails.
        return (
            f"{python} -c {anchor} {path} & anchor=$!; "
            f"while [ ! -f {path} ]; do "
            'kill -0 "$anchor" 2>/dev/null || exit 125; sleep 0.01; done; '
            f'bash -lc {run}; status=$?; exit "$status"'
        )

    def signal_identity(self, identity: Path, sig: int) -> bool:
        """Signal only the still-identical anchor's isolated process group."""
        try:
            data = json.loads(identity.read_text())
            pid = int(data["pid"])
            pgid = int(data["pgid"])
            session = int(data["session"])
            if pid <= 1 or pgid <= 1 or session <= 1 or pgid == os.getpgrp():
                return False
            if data["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                return False
            server = self.tmux(["display-message", "-p", "#{pid}"])
            if server.returncode or not re.fullmatch(r"[0-9]+", server.stdout.strip()):
                return False
            server_pid = int(server.stdout.strip())
            if server_pid <= 1:
                return False
            server_pgid = os.getpgid(server_pid)
            server_session = os.getsid(server_pid)
            if pid == server_pid or pgid == server_pgid or session == server_session:
                return False
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if (
                stat[0] == "Z"
                or stat[19] != data["start"]
                or int(stat[2]) != pgid
                or int(stat[3]) != session
                or pgid != session
            ):
                return False
            os.killpg(pgid, sig)
            return True
        except (OSError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError):
            return False

    def service_pgid(self, pid: int) -> int | None:
        """Resolve only a pane-led group, never a shared caller/server group."""
        try:
            pgid = os.getpgid(pid)
            if pgid != pid or pgid == os.getpgrp():
                return None
            return pgid
        except ProcessLookupError:
            return None

    def signal_group(self, pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

    def pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def now(self) -> float:
        return time.monotonic()
