"""Native Mac windows: Cocoa-Way and waypipe on the Mac, JailBee's SSH service in the machine.

GUI windows only open through JailBee's own SSH service: it marks a `waypipe ssh`
session, and `gui.display_target` recognises that marker. The service listens on
the machine's loopback and is reached through the machine's sshd (ProxyJump), so
only the Mac's key gets in; for that reason its host restrictions are turned off
here, which lets the dashboard keep every host command. Flip
`remote.ssh.restrict_host` back to true in the machine's global.yaml to restrict it.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import yaml

from jailbee.macsetup.engine import SetupContext, SetupError, ask
from jailbee.macsetup.steps_jailbee import GLOBAL_YAML, read_vm_global, run_as_user, write_vm_global

TAP = "J-x-Z/tap"
_NEEDED = ("cocoa-way", "waypipe")
_INSTALL_COMMAND = (
    f"brew tap {TAP} && brew trust --tap {TAP} && brew install cocoa-way waypipe-darwin"
)


def merge_remote_yaml(existing: str) -> str:
    """Set `remote.ssh.restrict_host: false` unless the user already chose a value."""
    data: Any = yaml.safe_load(existing) if existing.strip() else {}
    if not isinstance(data, dict):
        raise SetupError(
            f"{GLOBAL_YAML} in the machine is not a mapping.",
            remedy="Fix or remove that file inside the machine, then re-run.",
        )
    data.setdefault("remote", {}).setdefault("ssh", {}).setdefault("restrict_host", False)
    return yaml.safe_dump(data, sort_keys=False)


class GuiStep:
    id = "gui"
    title = "Setting up native Mac windows"

    def __init__(
        self,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._which = which
        self._runner = runner

    def fingerprint(self, ctx: SetupContext) -> str:
        return "1" if ctx.state.options.gui else "off"

    def run(self, ctx: SetupContext) -> None:
        if not ctx.state.options.gui:
            return
        self._ensure_mac_tools(ctx)
        self._enable_service(ctx)

    def _ensure_mac_tools(self, ctx: SetupContext) -> None:
        if all(self._which(tool) for tool in _NEEDED):
            return
        if not self._which("brew"):
            raise SetupError(
                "Native windows need Cocoa-Way and waypipe, and Homebrew is not installed.",
                remedy="Install them yourself (docs/macos.md), or re-run with --no-gui.",
            )
        question = (
            f"Install cocoa-way and waypipe-darwin from the third-party Homebrew tap {TAP}? "
            "Homebrew will be allowed to load that tap's formulae."
        )
        if not ask(ctx, question, _INSTALL_COMMAND):
            raise SetupError(
                "Native windows need Cocoa-Way.", remedy="Re-run with --no-gui to skip them."
            )
        for argv in (
            ["brew", "tap", TAP],
            ["brew", "trust", "--tap", TAP],
            ["brew", "install", "cocoa-way", "waypipe-darwin"],
        ):
            result = self._runner(argv, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise SetupError(
                    f"`{' '.join(argv)}` failed: {result.stderr.strip()}",
                    remedy=f"Run it yourself, or re-run with --no-gui: {_INSTALL_COMMAND}",
                )

    def _enable_service(self, ctx: SetupContext) -> None:
        public = (ctx.paths.ssh_dir / "id_ed25519.pub").read_text().strip()
        run_as_user(ctx, "jailbee remote ssh key add -", input=public + "\n")
        write_vm_global(ctx, merge_remote_yaml(read_vm_global(ctx)))
        run_as_user(ctx, "jailbee remote ssh enable")
        run_as_user(ctx, "jailbee remote ssh restart")
