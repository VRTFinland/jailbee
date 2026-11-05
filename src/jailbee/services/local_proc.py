"""Container-side subprocess exception for service tmux and shell operations."""

import codecs
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProbeResult:
    code: int | None
    stdout: str
    stderr: str
    timed_out: bool


_PROBE_OUTPUT_LIMIT = 16 * 1024

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
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return ProbeResult(
                process.returncode,
                stdout[:_PROBE_OUTPUT_LIMIT].decode("utf-8", errors="replace"),
                stderr[:_PROBE_OUTPUT_LIMIT].decode("utf-8", errors="replace"),
                False,
            )
        except subprocess.TimeoutExpired:
            self.signal_group(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            return ProbeResult(
                None,
                stdout[:_PROBE_OUTPUT_LIMIT].decode("utf-8", errors="replace"),
                stderr[:_PROBE_OUTPUT_LIMIT].decode("utf-8", errors="replace"),
                True,
            )
        except BaseException:
            self.signal_group(process.pid, signal.SIGKILL)
            process.wait()
            raise

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
