"""The shared RDP display: one container, one weston, one screen for every app.

``jailbee-display`` runs weston with the RDP backend on its own loopback; an
Incus proxy device publishes that port on the host's loopback. A host
directory holding weston's Wayland socket is mounted into this container and
into every client container (`runtime_mounts`), so an app in any container
draws on the one screen. Modelled on `registry.py`; everything goes through the
`Incus` wrapper and no subprocess is called here.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from typing import TYPE_CHECKING, Any

import yaml

from jailbee.config import CONTAINER_USERNAME
from jailbee.gui import display_state_dir
from jailbee.incus import Incus, IncusError
from jailbee.remote_ssh.display_forward import DISPLAY_FORWARD_HOST, DISPLAY_FORWARD_PORT
from jailbee.runtime_mounts import DISPLAY_DEVICE, display_device_config
from jailbee.stopping import stop_container

if TYPE_CHECKING:
    from collections.abc import Callable

DISPLAY_CONTAINER = "jailbee-display"
DISPLAY_PROFILE = "jailbee-display-profile"
DISPLAY_SERVICE = "jailbee-display.service"
DISPLAY_BRIDGE = "jailbee-loose"
# Where the shared directory is mounted INSIDE the display container. Not under
# /run: this device is part of the container's config, so Incus mounts it at
# start, before systemd puts a fresh tmpfs over /run and hides it (weston then
# fails with "XDG_RUNTIME_DIR ... is not a directory"). Client containers get
# `SHARED_DISPLAY_DIR` attached after boot, so for them /run is fine. Keep in
# step with the paths in provision/display/jailbee-display.service.
DISPLAY_CONTAINER_DIR = "/srv/jailbee-display"
# The `waypipe ssh` sessions' forward listeners (`remote_ssh.waypipe.links_dir`),
# mounted into this container only: a client container must never reach the
# laptop's waypipe client except through a server running here.
WAYPIPE_LINKS_CONTAINER_DIR = "/srv/jailbee-waypipe-links"
LINKS_DEVICE = "waypipe-links"
RDP_PORT = 3389
HOST_RDP_PORT = DISPLAY_FORWARD_PORT
RDP_PORT_ADDRESS = f"localhost:{RDP_PORT}"
CLIENT_WAIT_SECONDS = 120.0
CLIENT_POLL_SECONDS = 2.0
# An established TCP connection precedes the TLS/RDP handshake and weston's
# seat, so a connection must survive this long before it counts as a client.
SEAT_SETTLE_SECONDS = 3.0

_IMAGE = "images:ubuntu/26.04/cloud"
_SERVICE_WAIT_SECONDS = 60
_PROVISION_PKG = "jailbee.provision"
_PROVISION_SUBDIR = "display"
_UNIT_PATH = "/etc/systemd/system/jailbee-display.service"


class DisplayError(RuntimeError):
    """The shared display could not be prepared for a launch."""


class DisplayStatus(StrEnum):
    """Reported state of the jailbee-display container + its weston service."""

    RUNNING = "running"
    STOPPED = "stopped"
    DEGRADED = "degraded"
    MISSING = "missing"


def _no_steps(_message: str) -> None:
    """Default `on_step`: report nowhere."""


def _present(incus: Incus) -> dict[str, Any] | None:
    for c in incus.list_containers():
        if c.get("name") == DISPLAY_CONTAINER:
            return c
    return None


def _service_state(incus: Incus) -> str:
    try:
        return incus.exec(
            DISPLAY_CONTAINER, ["systemctl", "is-active", DISPLAY_SERVICE], timeout=10
        ).strip()
    except IncusError as e:
        return str(e)


def display_status(incus: Incus) -> DisplayStatus:
    entry = _present(incus)
    if entry is None:
        return DisplayStatus.MISSING
    if entry.get("status") != "Running":
        return DisplayStatus.STOPPED
    return DisplayStatus.RUNNING if _service_state(incus) == "active" else DisplayStatus.DEGRADED


def _display_profile_yaml(host_uid: int, host_gid: int) -> str:
    """Same idmap as the client containers, so the shared socket's owner matches.

    Unlike the registry mirror (`uid <uid> 0`), the compositor runs as the dev
    user, not as container root.

    ``security.nesting`` is required for the same reason as on the registry
    mirror: on hosts with ``kernel.apparmor_restrict_unprivileged_userns=1``
    systemd 256+ in the container hangs at ``(sd-mkuserns)``, so networkd and
    resolved never come up - no DHCPv4 lease, no resolv.conf, and the first
    ``apt-get`` fails with "Temporary failure resolving".
    """
    profile = {
        "name": DISPLAY_PROFILE,
        "description": "idmap + network for the jailbee-display container",
        "config": {
            "raw.idmap": f"uid {host_uid} {host_uid}\ngid {host_gid} {host_gid}",
            "security.nesting": "true",
        },
        "devices": {"eth0": {"type": "nic", "name": "eth0", "network": DISPLAY_BRIDGE}},
    }
    return yaml.safe_dump(profile, sort_keys=False)


def _ensure_profile(incus: Incus) -> None:
    if not incus.profile_exists(DISPLAY_PROFILE):
        incus.profile_create(DISPLAY_PROFILE)
    incus.profile_set_yaml(DISPLAY_PROFILE, _display_profile_yaml(os.getuid(), os.getgid()))


def _read_provision_text(filename: str) -> str:
    return (
        resources.files(_PROVISION_PKG).joinpath(_PROVISION_SUBDIR).joinpath(filename).read_text()
    )


def _provision(incus: Incus) -> None:
    unit = _read_provision_text("jailbee-display.service")
    install = _read_provision_text("install.sh")
    script = (
        "set -euo pipefail\n"
        f"cat > /root/jailbee-display.service <<'JAILBEE_UNIT_EOF'\n{unit.rstrip()}\n"
        "JAILBEE_UNIT_EOF\n"
        f"cat > /root/install.sh <<'JAILBEE_INSTALL_EOF'\n{install.rstrip()}\n"
        "JAILBEE_INSTALL_EOF\n"
        "chmod +x /root/install.sh\n"
        f"JAILBEE_UID={os.getuid()} JAILBEE_GID={os.getgid()} "
        f"JAILBEE_USER={CONTAINER_USERNAME} /root/install.sh\n"
    )
    incus.exec(DISPLAY_CONTAINER, ["bash", "-c", script], timeout=600)


def _links_device_config() -> dict[str, str]:
    from jailbee.remote_ssh.waypipe import links_dir

    return {"source": str(links_dir()), "path": WAYPIPE_LINKS_CONTAINER_DIR}


def _create(incus: Incus, shared_dir: str) -> None:
    incus.init(_IMAGE, DISPLAY_CONTAINER)
    incus.profile_assign(DISPLAY_CONTAINER, ["default", DISPLAY_PROFILE])
    incus.config_set(DISPLAY_CONTAINER, "boot.autostart", "true")
    incus.config_device_add(
        DISPLAY_CONTAINER, "shared", "disk", {"source": shared_dir, "path": DISPLAY_CONTAINER_DIR}
    )
    incus.config_device_add(DISPLAY_CONTAINER, LINKS_DEVICE, "disk", _links_device_config())
    # weston listens on the container's own loopback only (the bridge is never
    # an access path); this device publishes it on the host's loopback.
    incus.config_device_add(
        DISPLAY_CONTAINER,
        "rdp",
        "proxy",
        {"listen": f"tcp:127.0.0.1:{HOST_RDP_PORT}", "connect": f"tcp:127.0.0.1:{RDP_PORT}"},
    )
    incus.start(DISPLAY_CONTAINER)


def _provisioning_incomplete(incus: Incus) -> bool:
    """An RDP-era display (no waypipe) is re-provisioned in place by the idempotent install.sh."""
    try:
        out = incus.exec(
            DISPLAY_CONTAINER,
            ["bash", "-c", (
                    f"test -f {_UNIT_PATH} && command -v waypipe >/dev/null "
                    "&& echo present || echo absent"
                )],
            timeout=10,
        )
    except IncusError:
        return False
    return out.strip() == "absent"


def _wait_for_service(
    incus: Incus, on_step: Callable[[str], None], sleep_fn: Callable[[float], None]
) -> str | None:
    state = ""
    for remaining in range(_SERVICE_WAIT_SECONDS, 0, -2):
        state = _service_state(incus)
        if state == "active":
            return None
        on_step(f"waiting for {DISPLAY_SERVICE} - {state}, {remaining}s left")
        sleep_fn(2)
    return f"{DISPLAY_SERVICE} is {state!r} after {_SERVICE_WAIT_SECONDS}s"


def display_up(
    incus: Incus,
    *,
    recreate: bool = False,
    on_step: Callable[[str], None] = _no_steps,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Create, start and provision the display container; idempotent."""
    on_step("preparing the shared display directory and profile")
    directory = display_state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    from jailbee.remote_ssh.waypipe import links_dir

    links = links_dir()
    links.mkdir(parents=True, exist_ok=True, mode=0o700)
    links.chmod(0o700)
    if not incus.network_exists(DISPLAY_BRIDGE):
        incus.network_create(DISPLAY_BRIDGE)
    _ensure_profile(incus)
    if recreate and _present(incus) is not None:
        incus.delete(DISPLAY_CONTAINER, force=True)
    entry = _present(incus)
    if entry is None:
        on_step("creating the display container")
        _create(incus, str(directory))
        on_step("installing weston (this can take a minute)")
        _provision(incus)
    else:
        if entry.get("status") != "Running":
            incus.start(DISPLAY_CONTAINER)
        ensure_links_device(incus)
        if _provisioning_incomplete(incus):
            on_step("finishing provisioning")
            _provision(incus)
    failure = _wait_for_service(incus, on_step, sleep_fn)
    if failure is not None:
        raise DisplayError(f"{failure}. Run `jailbee display up --recreate`.")


def display_down(incus: Incus) -> None:
    """Stop the display container."""
    entry = _present(incus)
    if entry is None or entry.get("status") != "Running":
        return
    stop_container(incus, DISPLAY_CONTAINER, force_fallback=True, label="the shared display")
    remove_waypipe_sockets()


def client_connected(incus: Incus) -> bool:
    """Whether an RDP client is attached (weston has no input seat before one is)."""
    try:
        out = incus.exec(
            DISPLAY_CONTAINER,
            ["bash", "-c", f"ss -Htn state established '( sport = :{RDP_PORT} )' | wc -l"],
            timeout=10,
        )
    except IncusError:
        return False
    return out.strip() not in ("", "0")


def client_ready(
    incus: Incus,
    sleep_fn: Callable[[float], None] = time.sleep,
    settle_s: float = SEAT_SETTLE_SECONDS,
) -> bool:
    """A client that is connected now and still connected after the settle."""
    if not client_connected(incus):
        return False
    sleep_fn(settle_s)
    return client_connected(incus)


def wait_for_client(
    incus: Incus,
    *,
    timeout_s: float = CLIENT_WAIT_SECONDS,
    poll_s: float = CLIENT_POLL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = clock() + timeout_s
    while True:
        if client_ready(incus, sleep_fn):
            return True
        if clock() >= deadline:
            return False
        sleep_fn(poll_s)


@dataclass(frozen=True)
class ConnectionInfo:
    ssh_command: str
    rdp_address: str
    hints: tuple[str, ...]


def connection_info(ssh_port: int) -> ConnectionInfo:
    return ConnectionInfo(
        ssh_command=(
            f"ssh -N -L {RDP_PORT}:{DISPLAY_FORWARD_HOST}:{HOST_RDP_PORT} "
            f"-p {ssh_port} jailbee@<host>"
        ),
        rdp_address=RDP_PORT_ADDRESS,
        hints=(
            "Login: any user name and password; weston does not check them",
            "Windows: mstsc  |  macOS: Windows App (was Microsoft Remote Desktop)  |  "
            "Linux: xfreerdp / Remmina",
            "Already connected over SSH? Add the forward live with ~C, then -L ...",
        ),
    )


def format_connection_info(info: ConnectionInfo) -> list[str]:
    """The recipe as plain lines: the CLI and the dashboard both show exactly this."""
    return [
        "1. Open the tunnel on your computer:",
        f"     {info.ssh_command}",
        f"2. Connect an RDP client to {info.rdp_address}",
        *(f"   {hint}" for hint in info.hints),
    ]


def ensure_display_mount(incus: Incus, container: str) -> None:
    """Add the shared directory (read-only) to a running container that predates it."""
    try:
        incus.config_device_add(container, DISPLAY_DEVICE, "disk", display_device_config())
    except IncusError as e:
        if "already exists" in str(e).lower():
            return
        raise


def ensure_links_device(incus: Incus) -> None:
    """Add the waypipe links directory to a display container that predates it."""
    try:
        incus.config_device_add(DISPLAY_CONTAINER, LINKS_DEVICE, "disk", _links_device_config())
    except IncusError as e:
        if "already exists" in str(e).lower():
            return
        raise


def ensure_waypipe_display(
    incus: Incus,
    *,
    on_step: Callable[[str], None] = _no_steps,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Make jailbee-display ready to host `waypipe ssh` servers.

    Unlike `prepare_shared_display`, no RDP client is involved: weston may
    stay unconnected for the whole session.
    """
    if display_status(incus) is not DisplayStatus.RUNNING:
        display_up(incus, on_step=on_step, sleep_fn=sleep_fn)
    ensure_links_device(incus)


def remove_waypipe_sockets(session_id: str | None = None) -> None:
    """Remove the host-side sockets of one waypipe session, or of all of them."""
    from jailbee.remote_ssh.waypipe import links_dir

    servers = f"wp-{session_id}-*" if session_id else "wp-*"
    links = f"{session_id}.sock" if session_id else "*.sock"
    for directory, pattern in ((display_state_dir(), servers), (links_dir(), links)):
        if not directory.is_dir():
            continue
        for path in directory.glob(pattern):
            path.unlink(missing_ok=True)


def prepare_shared_display(
    incus: Incus,
    container: str,
    *,
    ssh_port: int,
    say: Callable[[str], None],
    sleep_fn: Callable[[float], None] = time.sleep,
    wait_seconds: float = CLIENT_WAIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Make the shared display ready for one launch from an SSH session.

    Brings the display up if needed, mounts it into ``container``, and (if no
    RDP client is attached yet, which an app would fail without) prints the
    recipe and waits for one.
    """
    if display_status(incus) is not DisplayStatus.RUNNING:
        say("Starting the shared display...")
        display_up(incus, on_step=say, sleep_fn=sleep_fn)
    ensure_display_mount(incus, container)
    if client_ready(incus, sleep_fn):
        return
    for line in format_connection_info(connection_info(ssh_port)):
        say(line)
    say("Waiting for an RDP client...")
    if not wait_for_client(incus, timeout_s=wait_seconds, sleep_fn=sleep_fn, clock=clock):
        raise DisplayError(
            f"No RDP client connected within {int(wait_seconds)}s. Open the tunnel, "
            f"connect to {RDP_PORT_ADDRESS}, then launch again."
        )
