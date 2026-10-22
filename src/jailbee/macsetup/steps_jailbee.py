"""Install JailBee inside the machine and build the shared base image."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

import yaml

from jailbee.macsetup.engine import SetupContext, SetupError

_GIT_SPEC = "git+https://github.com/VRTFinland/jailbee"
_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
_USER_BUS = (
    'export XDG_RUNTIME_DIR="/run/user/$(id -u)"; '
    'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"'
)
GLOBAL_YAML = "~/.config/jailbee/global.yaml"
SCRATCH_DIR = "$HOME/jailbee-scratch"
POOL = "jailbee-btrfs"


def _mac_version() -> str:
    from jailbee import __version__

    return __version__


def install_command(version: str, *, warn: Callable[[str], None] | None = None) -> str:
    """`uv tool install` command for the Mac's own version (PyPI), or git for dev builds."""
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


def _user(ctx: SetupContext, script: str, *, input: str | None = None) -> str:
    """Run `script` in a bash as the machine's development user."""
    full = f"{_PATH}; {_USER_BUS}; {script}"
    result = ctx.ac.machine_run(ctx.state.options.machine, ["bash", "-lc", full], input=input)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SetupError(
            f"A step inside the machine failed: {detail}" if detail else
            "A step inside the machine failed.",
            remedy="Check the message above, then re-run.",
        )
    return result.stdout


class JailbeeStep:
    id = "jailbee"
    title = "Installing JailBee in the machine"

    def __init__(self, mac_version: str | None = None) -> None:
        self._version = mac_version or _mac_version()

    def fingerprint(self, ctx: SetupContext) -> str:
        return self._version

    def run(self, ctx: SetupContext) -> None:
        _user(ctx, "command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh")
        _user(ctx, install_command(self._version, warn=ctx.log))
        current = ctx.ac.machine_run(
            ctx.state.options.machine, ["bash", "-lc", f"cat {GLOBAL_YAML} 2>/dev/null || true"]
        ).stdout
        merged = merge_global_yaml(current)
        _user(ctx, f"mkdir -p ~/.config/jailbee && cat > {GLOBAL_YAML}", input=merged)
        _user(ctx, "jailbee setup --yes --only timer")


class BaseImageStep:
    id = "base"
    title = "Building the base image (several minutes)"

    def __init__(self, mac_version: str | None = None) -> None:
        self._version = mac_version or _mac_version()

    def fingerprint(self, ctx: SetupContext) -> str:
        data = f"{self._version}\0{ctx.state.facts['kernel_path']}"
        return hashlib.sha256(data.encode()).hexdigest()[:12]

    def run(self, ctx: SetupContext) -> None:
        scratch = SCRATCH_DIR
        _user(ctx, f"mkdir -p {scratch} && cd {scratch} && jailbee base build")
        # Doctor is advisory here: it also reports host details setup does not manage.
        report = ctx.ac.machine_run(
            ctx.state.options.machine, ["bash", "-lc", f"{_PATH}; {_USER_BUS}; cd {scratch} && jailbee doctor"]
        )
        for line in (report.stdout or report.stderr).splitlines():
            ctx.log(f"  {line}")
