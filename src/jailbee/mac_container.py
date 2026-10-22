"""Wrapper around Apple's `container` CLI, used by `jailbee mac setup`.

This is the only module that runs `container`. Every call is an argv list, never a
shell string — except `machine_run`, whose command must reach the machine as ONE
pre-quoted string: `container machine run` joins its arguments with spaces and runs
them through `bash -c`, so separate argv items lose their quoting (measured on a
Mac: `sh -c "exit 7"` arrived as `sh -c exit 7`).

Self-contained like `macos.py`: stdlib only.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_KERNELS_DIR = Path.home() / "Library" / "Application Support" / "com.apple.container" / "kernels"
_DEFAULT_KERNEL_LINK = "default.kernel-arm64"


class AppleContainerError(Exception):
    """A `container` command failed; the message is its stderr or a plain sentence."""


@dataclass(frozen=True)
class MachineInfo:
    name: str
    running: bool
    kernel: str | None
    home_mount: str
    ip: str | None = None


class AppleContainer:
    def __init__(
        self,
        binary: str = "container",
        runner: Runner = subprocess.run,
        kernels_dir: Path = _KERNELS_DIR,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._bin = binary
        self._runner = runner
        self._kernels_dir = kernels_dir

    # -- plumbing ---------------------------------------------------------

    def _capture(self, *args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        return self._runner(
            [self._bin, *args], capture_output=True, text=True, check=False, input=input
        )

    def _checked(self, *args: str) -> str:
        result = self._capture(*args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise AppleContainerError(detail or f"container {' '.join(args[:2])} failed")
        return result.stdout

    # -- system -----------------------------------------------------------

    def version(self) -> str | None:
        try:
            result = self._capture("--version")
        except FileNotFoundError:
            return None
        match = re.search(r"version (\d+\.\d+\.\d+)", result.stdout)
        return match.group(1) if result.returncode == 0 and match else None

    def system_running(self) -> bool:
        result = self._capture("system", "status")
        return result.returncode == 0 and bool(
            re.search(r"^status\s+running\b", result.stdout, re.MULTILINE)
        )

    def default_kernel_release(self) -> str | None:
        """Release of Apple's default kernel, read from its symlink's target name."""
        link = self._kernels_dir / _DEFAULT_KERNEL_LINK
        if not link.is_symlink():
            return None
        return link.resolve().name.removeprefix("vmlinux-") or None

    def set_default_kernel_recommended(self) -> None:
        """Replace Apple's global default kernel with the recommended one.

        Global state other `container` users rely on: callers ask first.
        """
        self._checked("system", "kernel", "set", "--recommended", "--force")

    # -- images and one-shot containers -----------------------------------

    def build_image(self, context: Path, tag: str, *, cpus: int, memory: str) -> None:
        self._checked(
            "build", "--progress", "plain", "--cpus", str(cpus), "--memory", memory,
            "-t", tag, str(context),
        )  # fmt: skip

    def image_exists(self, tag: str) -> bool:
        return self._capture("image", "inspect", tag).returncode == 0

    def image_rm(self, tag: str) -> None:
        self._checked("image", "rm", tag)

    def run_ephemeral(
        self,
        image: str,
        argv: list[str],
        *,
        volumes: dict[Path, str],
        cpus: int,
        memory: str,
    ) -> int:
        """Run `argv` in a throwaway container with stdio inherited; return its exit code."""
        cmd = [self._bin, "run", "--rm", "--cpus", str(cpus), "--memory", memory]
        for source, target in volumes.items():
            cmd += ["-v", f"{source}:{target}"]
        return self._runner([*cmd, image, *argv], check=False).returncode

    # -- machines ---------------------------------------------------------

    def machine(self, name: str) -> MachineInfo | None:
        result = self._capture("machine", "inspect", name)
        if result.returncode != 0:
            return None
        try:
            entry: dict[str, Any] = json.loads(result.stdout)[0]
        except (ValueError, IndexError, TypeError):
            return None
        return MachineInfo(
            name=str(entry.get("id", name)),
            running=entry.get("status") == "running",
            kernel=entry.get("kernel") or None,
            home_mount=str(entry.get("homeMount", "rw")),
            ip=entry.get("ipAddress") or None,
        )

    def machine_create(
        self,
        name: str,
        image: str,
        *,
        cpus: int,
        memory: str,
        home_mount: str,
        kernel: Path,
    ) -> None:
        self._checked(
            "machine", "create", "--name", name, "--cpus", str(cpus), "--memory", memory,
            "--home-mount", home_mount, "--kernel", str(kernel), image,
        )  # fmt: skip

    def machine_set(self, name: str, **settings: str) -> None:
        self._checked("machine", "set", "-n", name, *(f"{k}={v}" for k, v in settings.items()))

    def machine_stop(self, name: str) -> None:
        self._checked("machine", "stop", name)

    def machine_rm(self, name: str) -> None:
        self._checked("machine", "rm", name)

    def machine_run(
        self,
        name: str,
        argv: list[str],
        *,
        root: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` in the machine, capturing output. Never raises on a non-zero exit."""
        flags = ["--root"] if root else []
        if input is not None:
            flags.append("-i")
        return self._capture(
            "machine", "run", "-n", name, *flags, "--", shlex.join(argv), input=input
        )

    def wait_ready(self, name: str, *, attempts: int = 60) -> None:
        """Wait until the machine's systemd has finished booting.

        Just after boot the system bus is missing and `is-system-running` prints
        only an error, so an empty answer is retried; `degraded` counts as up.
        """
        for _ in range(attempts):
            result = self.machine_run(name, ["systemctl", "is-system-running", "--wait"], root=True)
            state = result.stdout.strip()
            if state in {"running", "degraded"}:
                return
            if state == "offline":
                raise AppleContainerError(f"machine {name} is offline; stop and start it again")
            self._sleep(2)
        raise AppleContainerError(f"machine {name} did not finish booting; try again in a minute")
