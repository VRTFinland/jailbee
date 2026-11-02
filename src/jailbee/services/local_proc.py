"""Container-side subprocess exception for service tmux and shell operations."""

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence


class LocalProc:
    """Execute container-local service operations."""

    def tmux(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)

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
