"""Wire the Mac to the machine: an SSH key and alias, and the bridge configuration."""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

from jailbee.macsetup.engine import SetupContext, SetupError

SSH_ALIAS = "jailbee-vm"
_INCLUDE = "~/.config/jailbee/mac/ssh/config"
_KEY = "id_ed25519"


def ensure_include_line(config: Path, target: str) -> None:
    """Make `Include target` the first line of an ssh config, exactly once."""
    line = f"Include {target}\n"
    config.parent.mkdir(parents=True, exist_ok=True)
    if config.exists():
        text = config.read_text()
        if line in text.splitlines(keepends=True):
            return
        config.write_text(line + "\n" + text if text else line)
    else:
        config.write_text(line)
        config.chmod(0o600)


class WiringStep:
    id = "wiring"
    title = "Connecting the Mac to the machine"

    def __init__(
        self,
        user: str | None = None,
        user_ssh_config: Path | None = None,
        macos_yaml: Path | None = None,
        container_binary: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._user = user or getpass.getuser()
        self._user_ssh_config = user_ssh_config or Path.home() / ".ssh" / "config"
        self._macos_yaml = macos_yaml
        self._container = container_binary or shutil.which("container") or "container"
        self._runner = runner

    def fingerprint(self, ctx: SetupContext) -> str:
        opts = ctx.state.options
        return f"{opts.mode}:{opts.gui}:{opts.machine}:1"

    def _macos_yaml_path(self) -> Path:
        if self._macos_yaml is not None:
            return self._macos_yaml
        from jailbee.macos import _config_path

        return _config_path()

    def run(self, ctx: SetupContext) -> None:
        ssh_dir = ctx.paths.ssh_dir
        ssh_dir.mkdir(parents=True, exist_ok=True)
        ssh_dir.chmod(0o700)
        public = self._ensure_key(ssh_dir)
        self._authorize(ctx, public)
        self._write_ssh_config(ctx)
        ensure_include_line(self._user_ssh_config, _INCLUDE)
        self._write_macos_yaml(ctx)

    def _ensure_key(self, ssh_dir: Path) -> str:
        key = ssh_dir / _KEY
        if not key.exists():
            result = self._runner(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jailbee-mac-setup", "-f", str(key)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise SetupError(f"ssh-keygen failed: {result.stderr.strip()}")
        return Path(f"{key}.pub").read_text().strip()

    def _authorize(self, ctx: SetupContext, public: str) -> None:
        script = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && read -r key && "
            'grep -qxF "$key" ~/.ssh/authorized_keys 2>/dev/null || '
            'printf "%s\\n" "$key" >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys'
        )
        result = ctx.ac.machine_run(ctx.state.options.machine, ["bash", "-c", script], input=public + "\n")
        if result.returncode != 0:
            raise SetupError(f"Could not authorise the SSH key in the machine: {result.stderr.strip()}")

    def _write_ssh_config(self, ctx: SetupContext) -> None:
        ssh_dir = ctx.paths.ssh_dir
        proxy = f"{shlex.quote(self._container)} machine run -n {ctx.state.options.machine} -i -- nc 127.0.0.1 22"
        (ssh_dir / "config").write_text(
            f"Host {SSH_ALIAS}\n"
            f"    HostName {SSH_ALIAS}\n"
            f"    User {self._user}\n"
            f"    IdentityFile {ssh_dir / _KEY}\n"
            "    IdentitiesOnly yes\n"
            "    StrictHostKeyChecking accept-new\n"
            f"    UserKnownHostsFile {ssh_dir / 'known_hosts'}\n"
            f"    ProxyCommand {proxy}\n"
        )

    def _write_macos_yaml(self, ctx: SetupContext) -> None:
        path = self._macos_yaml_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + ".colima.bak")
        if path.exists() and not backup.exists():
            existing = yaml.safe_load(path.read_text()) or {}
            if not (isinstance(existing, dict) and existing.get("backend") == "apple"):
                backup.write_text(path.read_text())
        opts = ctx.state.options
        data = {"backend": "apple", "machine": opts.machine, "mode": opts.mode, "gui": opts.gui}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False))
        os.replace(tmp, path)
