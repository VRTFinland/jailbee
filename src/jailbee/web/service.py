"""Install and inspect the web dashboard's systemd user service.

Never imports FastAPI: `enable` and `status` work without the `web` extra
(the unit's `serve` then reports the missing extra itself).
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jailbee.config import ConfigError
from jailbee.config.models_remote import RemoteWebConfig
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.systemd import systemd_user_dir, write_if_changed

WEB_SERVICE = "jailbee-web.service"


@dataclass(frozen=True)
class Problem:
    message: str


@dataclass(frozen=True)
class WebServiceStatus:
    unit_path: Path
    installed: bool
    enabled: bool
    active: bool
    listen: str
    port: int
    problems: tuple[Problem, ...]


def enable() -> None:
    """Install, enable, and start the web user service."""
    jailbee_bin = shutil.which("jailbee")
    if jailbee_bin is None:
        raise RuntimeError("jailbee is not on PATH")
    units_dir = systemd_user_dir()
    units_dir.mkdir(parents=True, exist_ok=True)
    template = (files("jailbee.templates.systemd") / WEB_SERVICE).read_text()
    rendered = template.replace("{jailbee_bin}", shlex.quote(jailbee_bin))
    if write_if_changed(units_dir / WEB_SERVICE, rendered):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", WEB_SERVICE], check=True)


def disable() -> None:
    """Disable and stop the web user service."""
    subprocess.run(["systemctl", "--user", "disable", "--now", WEB_SERVICE], check=True)


def _probe(action: str) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", action, WEB_SERVICE],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return False, f"Could not query whether the web service is {action}: {exc}"
    return result.returncode == 0, None


def status() -> WebServiceStatus:
    """Inspect the unit and the configured address without starting anything."""
    problems: list[Problem] = []
    unit_path = systemd_user_dir() / WEB_SERVICE
    installed = unit_path.is_file()
    if not installed:
        problems.append(Problem("The web service unit is not installed."))
    enabled, enabled_problem = _probe("is-enabled")
    active, active_problem = _probe("is-active")
    problems.extend(Problem(m) for m in (enabled_problem, active_problem) if m is not None)
    if not enabled:
        problems.append(Problem("The web service is not enabled."))
    if not active:
        problems.append(Problem("The web service is not active."))
    try:
        web = load_global_config(default_global_config_path())[0].remote.web
    except (ConfigError, OSError) as exc:
        problems.append(Problem(f"Global config is invalid: {exc}"))
        web = RemoteWebConfig()
    return WebServiceStatus(
        unit_path=unit_path,
        installed=installed,
        enabled=enabled,
        active=active,
        listen=web.listen,
        port=web.port,
        problems=tuple(problems),
    )
