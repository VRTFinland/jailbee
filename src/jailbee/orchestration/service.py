"""Orchestration broker service: the systemd user unit that runs `jailbee _broker`."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from importlib.resources import files

from jailbee.systemd import systemd_user_dir, write_if_changed
from jailbee.tui import warn

BROKER_SERVICE = "jailbee-broker.service"


def install(jailbee_bin: str) -> bool:
    """Write the broker unit; True when its content changed."""
    units_dir = systemd_user_dir()
    units_dir.mkdir(parents=True, exist_ok=True)
    template = (files("jailbee.templates.systemd") / BROKER_SERVICE).read_text()
    rendered = template.replace("{jailbee_bin}", shlex.quote(jailbee_bin))
    return write_if_changed(units_dir / BROKER_SERVICE, rendered)


def enable_now(jailbee_bin: str) -> None:
    """Install the unit, daemon-reload on change, `enable --now`. May raise."""
    if install(jailbee_bin):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", BROKER_SERVICE], check=True)


def ensure_running() -> None:
    """Make sure the orchestration broker is running. Never raises."""
    jailbee_bin = shutil.which("jailbee")
    if jailbee_bin is None:
        warn("jailbee not on PATH — broker not started; run `jb setup` after installing.")
        return
    try:
        enable_now(jailbee_bin)
    except (OSError, subprocess.CalledProcessError) as exc:
        warn(f"Could not start the orchestration broker ({exc}); run `jb setup`.")


def status() -> str:
    """The unit's `systemctl --user is-active` answer, or "unknown"."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", BROKER_SERVICE],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return proc.stdout.strip() or "unknown"
