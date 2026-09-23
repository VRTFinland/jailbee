"""Install and inspect the SSH server's systemd user service."""

from __future__ import annotations

import importlib.util
import shlex
import shutil
import stat
import subprocess
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files
from pathlib import Path

from jailbee.config import ConfigError
from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.remote_ssh.keys import (
    SSHDependencyError,
    SSHKeyError,
    ensure_key_files,
    read_authorized_keys,
    ssh_paths,
)
from jailbee.systemd import systemd_user_dir, write_if_changed

SSH_SERVICE = "jailbee-ssh.service"


class ProblemSeverity(Enum):
    """How urgently `status()` wants a problem acted on.

    `FATAL` covers configuration errors and unsafe key-file permissions —
    states an operator should treat as broken. `WARNING` covers everything
    else `status()` reports (service lifecycle state, an empty key file),
    which can be entirely normal (a freshly-enabled service, or a
    deliberately empty `authorized_keys`; see docs/security.md).
    """

    WARNING = "warning"
    FATAL = "fatal"


@dataclass(frozen=True)
class Problem:
    severity: ProblemSeverity
    message: str


@dataclass(frozen=True)
class ServiceStatus:
    unit_path: Path
    installed: bool
    enabled: bool
    active: bool
    listen: str
    port: int
    entrypoints: tuple[str, ...]
    authorized_keys: int
    problems: tuple[Problem, ...]


def _ssh_dependency_available() -> bool:
    try:
        return importlib.util.find_spec("asyncssh") is not None
    except (ImportError, ValueError):
        return False


def enable() -> None:
    """Install, enable, and start the SSH user service."""
    if not _ssh_dependency_available():
        raise SSHDependencyError(
            "The SSH server requires the optional 'ssh' extra. "
            "Install it with: uv tool install 'jailbee[ssh]'"
        )
    jailbee_bin = shutil.which("jailbee")
    if jailbee_bin is None:
        raise RuntimeError("jailbee is not on PATH")

    ensure_key_files()
    units_dir = systemd_user_dir()
    units_dir.mkdir(parents=True, exist_ok=True)
    template = (files("jailbee.templates.systemd") / SSH_SERVICE).read_text()
    rendered = template.replace("{jailbee_bin}", shlex.quote(jailbee_bin))
    if write_if_changed(units_dir / SSH_SERVICE, rendered):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", SSH_SERVICE],
        check=True,
    )


def disable() -> None:
    """Disable and stop the SSH user service without removing its keys."""
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", SSH_SERVICE],
        check=True,
    )


def restart() -> None:
    """Restart an installed SSH user service."""
    if not (systemd_user_dir() / SSH_SERVICE).is_file():
        raise RuntimeError(f"{SSH_SERVICE} is not installed")
    subprocess.run(
        ["systemctl", "--user", "restart", SSH_SERVICE],
        check=True,
    )


def _systemctl_probe(action: str) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", action, SSH_SERVICE],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return False, f"Could not query whether the SSH service is {action}: {exc}"
    return result.returncode == 0, None


def _entrypoints(config: RemoteSSHConfig) -> tuple[str, ...]:
    return tuple(
        name
        for name, available in (
            ("dashboard", config.dashboard),
            ("shell", config.shell),
            ("exec", config.exec),
        )
        if available
    )


def status() -> ServiceStatus:
    """Inspect the service, config, and key files without starting anything."""
    problems: list[Problem] = []

    def warn(message: str) -> None:
        problems.append(Problem(ProblemSeverity.WARNING, message))

    def fatal(message: str) -> None:
        problems.append(Problem(ProblemSeverity.FATAL, message))

    unit_path = systemd_user_dir() / SSH_SERVICE
    installed = unit_path.is_file()
    if not _ssh_dependency_available():
        warn("The optional SSH dependency is not installed.")
    if not installed:
        warn("The SSH service unit is not installed.")

    enabled, enabled_problem = _systemctl_probe("is-enabled")
    active, active_problem = _systemctl_probe("is-active")
    if enabled_problem is not None:
        warn(enabled_problem)
    if active_problem is not None:
        warn(active_problem)
    if not enabled:
        warn("The SSH service is not enabled.")
    if not active:
        warn("The SSH service is not active.")

    try:
        global_config, _warnings = load_global_config(default_global_config_path())
        ssh_config = global_config.remote.ssh
    except (ConfigError, OSError) as exc:
        fatal(f"Global config is invalid: {exc}")
        ssh_config = RemoteSSHConfig()

    paths = ssh_paths()
    # A missing authorized-keys file behaves exactly like an empty one (both
    # read as zero keys), and the security design treats an empty file as a
    # valid, if useless, configuration — so only its *unsafe* states are
    # fatal. A missing host key has no such safe reading: the listener
    # cannot start without one. Either file's permissions or unreadability
    # being wrong is equally unsafe key-file handling, so those are fatal for
    # both (previously the authorized-keys file's own unsafe mode was not,
    # while the host key's was — final review finding M4).
    for label, path, missing in (
        ("Authorized keys file", paths.authorized_keys, warn),
        ("Host key", paths.host_key, fatal),
    ):
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            missing(f"{label} is missing.")
            continue
        except OSError as exc:
            fatal(f"Could not inspect {label.lower()}: {exc}")
            continue
        if mode != 0o600:
            fatal(f"{label} mode is {mode:04o}; expected 0600.")

    try:
        authorized_keys = len(read_authorized_keys(paths=paths))
    except (OSError, SSHKeyError) as exc:
        warn(f"Could not read authorized keys: {exc}")
        authorized_keys = 0
    if authorized_keys == 0:
        warn("No authorized client keys are configured.")

    return ServiceStatus(
        unit_path=unit_path,
        installed=installed,
        enabled=enabled,
        active=active,
        listen=ssh_config.listen,
        port=ssh_config.port,
        entrypoints=_entrypoints(ssh_config),
        authorized_keys=authorized_keys,
        problems=tuple(problems),
    )
