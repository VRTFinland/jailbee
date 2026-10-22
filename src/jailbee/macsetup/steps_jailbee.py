"""Install JailBee inside the machine and build the shared base image."""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import shlex
from collections.abc import Callable
from typing import Any

import yaml

from jailbee.macsetup.engine import SetupContext, SetupError
from jailbee.macsetup.steps_machine import machine_generation

_GIT_SPEC = "git+https://github.com/VRTFinland/jailbee"
_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
_USER_BUS = (
    'export XDG_RUNTIME_DIR="/run/user/$(id -u)"; '
    'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"'
)
GLOBAL_YAML = "~/.config/jailbee/global.yaml"
SCRATCH_DIR = "$HOME/jailbee-scratch"
POOL = "jailbee-btrfs"
RECIPE = 2
"""Bump when this step does more than before, so machines already set up redo it."""
_KERNEL_ROWS = ("kernel bridge conntrack", "kernel btrfs ACLs", "apparmor")


def _mac_version() -> str:
    from jailbee import __version__

    return __version__


def install_command(version: str, *, warn: Callable[[str], None] | None = None) -> str:
    """`uv tool install` command for the Mac's own version (PyPI), or git for dev builds.

    `JAILBEE_MAC_INSTALL_SPEC` overrides the spec, to try a branch or a wheel build.
    """
    override = os.environ.get("JAILBEE_MAC_INSTALL_SPEC")
    if override:
        return f"uv tool install --force {shlex.quote(override)}"
    if re.search(r"dev|\+|unknown", version):
        if warn is not None:
            warn(f"warning: this Mac runs {version}, which is not on PyPI; installing from git")
        return f"uv tool install --force {_GIT_SPEC}"
    return f"uv tool install --force 'jailbee[ssh]=={version}'"


def merge_global_yaml(existing: str) -> str:
    """Add the pool and Firefox defaults to a global.yaml without touching other keys."""
    data: Any = yaml.safe_load(existing) if existing.strip() else {}
    if not isinstance(data, dict):
        raise SetupError(
            f"{GLOBAL_YAML} in the machine is not a mapping.",
            remedy="Fix or remove that file inside the machine, then re-run.",
        )
    data.setdefault("defaults", {}).setdefault("storage_pool", POOL)
    data.setdefault("browsers", {}).setdefault("firefox", {}).setdefault("enabled", True)
    return yaml.safe_dump(data, sort_keys=False)


def run_as_user(
    ctx: SetupContext, script: str, *, input: str | None = None, check: bool = True
) -> str:
    """Run `script` in a bash as the machine's development user; return its stdout."""
    ctx.ac.wait_ready(ctx.state.options.machine)  # after a Mac reboot the machine is cold
    full = f"{_PATH}; {_USER_BUS}; {script}"
    # Root enters the machine and `runuser -u` drops to the login user *with* its
    # groups: a plain `machine run` session carries none, so `incus` is refused.
    argv = ["runuser", "-u", getpass.getuser(), "--", "bash", "-lc", full]
    result = ctx.ac.machine_run(ctx.state.options.machine, argv, root=True, input=input)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SetupError(
            f"A step inside the machine failed: {detail}"
            if detail
            else "A step inside the machine failed.",
            remedy="Check the message above, then re-run.",
        )
    return result.stdout


def read_vm_global(ctx: SetupContext) -> str:
    """The development user's global.yaml inside the machine ('' when absent)."""
    result = ctx.ac.machine_run(
        ctx.state.options.machine, ["bash", "-lc", f"cat {GLOBAL_YAML} 2>/dev/null || true"]
    )
    return result.stdout


def write_vm_global(ctx: SetupContext, text: str) -> None:
    run_as_user(ctx, f"mkdir -p ~/.config/jailbee && cat > {GLOBAL_YAML}", input=text)


class JailbeeStep:
    id = "jailbee"
    title = "Installing JailBee in the machine"

    def __init__(self, mac_version: str | None = None) -> None:
        self._version = mac_version or _mac_version()

    def fingerprint(self, ctx: SetupContext) -> str:
        return f"{self._version}:r{RECIPE}:{machine_generation(ctx)}"

    def run(self, ctx: SetupContext) -> None:
        run_as_user(
            ctx, "command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
        run_as_user(ctx, install_command(self._version, warn=ctx.log))
        write_vm_global(ctx, merge_global_yaml(read_vm_global(ctx)))
        run_as_user(ctx, "jailbee setup --yes --only timer")
        if ctx.state.options.mode == "mac-repos":
            # Owners on the Mac share flicker between the login user and root in the
            # machine, which makes git refuse the repositories as 'dubious ownership'.
            run_as_user(
                ctx,
                "git config --global --get-all safe.directory | grep -qxF '*' "
                "|| git config --global --add safe.directory '*'",
            )


class BaseImageStep:
    id = "base"
    title = "Building the base image (several minutes)"

    def __init__(self, mac_version: str | None = None) -> None:
        self._version = mac_version or _mac_version()

    def fingerprint(self, ctx: SetupContext) -> str:
        data = f"{self._version}\0{ctx.state.facts['kernel_path']}\0{machine_generation(ctx)}"
        return hashlib.sha256(data.encode()).hexdigest()[:12]

    def run(self, ctx: SetupContext) -> None:
        scratch = SCRATCH_DIR
        run_as_user(ctx, f"mkdir -p {scratch} && cd {scratch} && jailbee base build")
        # Doctor also reports host details setup does not manage (git identity, a
        # graphical session), so only the rows about the kernel this setup built count.
        report = run_as_user(ctx, f"cd {scratch} && jailbee doctor", check=False)
        failing = [
            line.strip(" |│")
            for line in report.splitlines()
            if any(row in line for row in _KERNEL_ROWS) and "FAIL" in line
        ]
        if failing:
            raise SetupError(
                "The kernel is missing something JailBee needs: " + "; ".join(failing),
                remedy="Run: jailbee mac setup --help, or see docs/apple-container-machine.md",
            )
