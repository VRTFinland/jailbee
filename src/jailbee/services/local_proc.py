"""Container-side subprocess exception for service tmux and shell operations."""

import codecs
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence


class LocalProc:
    """Execute container-local service operations."""

    def tmux(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)

    def follow_pane(self, target: str, lines: int) -> Iterator[str]:
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
        try:
            for line in process.stdout:
                line = line.rstrip(b"\n")
                if capture_id is not None and not streaming:
                    if line == b"%end " + capture_id:
                        streaming = True
                        yield b"".join(snapshot).decode("utf-8", errors="replace")
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
                        process.stdin.write(f"capture-pane -p -t {pane} -S -{lines}\n".encode())
                        process.stdin.flush()
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
            process.stdin.close()
            if process.poll() is None:
                process.terminate()
            process.wait()
            process.stdout.close()

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
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return 124

    def killpg(self, pid: int, sig: int) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError:
            pass

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
