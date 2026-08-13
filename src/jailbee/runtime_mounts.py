"""Per-container runtime device mounts for /run/user/<uid>/* sockets.

Wayland, PulseAudio, D-Bus and GnuPG sockets must be bind-mounted into
the container's ``/run/user/<uid>``. Doing this via the ``<prefix>-base``
profile races with systemd-logind: LXC mounts the sockets before PID 1
starts, which forces Incus to auto-create the parent dir as root-owned
tmpfs. Then logind (triggered by the linger marker in the golden image)
creates *another* tmpfs at the same path for the dev user — shadowing
the original mounts. Bind mounts are still in the mount table, but
invisible at directory listing level.

The fix is to attach the socket devices *after* logind has
provisioned ``/run/user/<uid>`` for the dev user — so the bind mounts
land on logind's live tmpfs, not on a parent that gets shadowed.
"""

from __future__ import annotations

import time

from jailbee.config import Config
from jailbee.gui import host_is_wayland
from jailbee.incus import Incus, IncusError
from jailbee.tui import info, warn

# Device names → relative path under /run/user/<uid>. Source and target
# paths are identical (host /run/user/<host_uid>/X → container same path).
# ``wayland-socket`` is conditional: it only exists on Wayland hosts. On
# X11 hosts the source path /run/user/<uid>/wayland-0 doesn't exist and
# Incus rejects the device add, so it is filtered out at attach time.
SOCKET_DEVICES: dict[str, str] = {
    "wayland-socket": "wayland-0",
    "pulse-socket": "pulse",
    "dbus-socket": "bus",
    "gpg-socket": "gnupg",
}

# Device names whose host source path is only present on Wayland sessions.
WAYLAND_ONLY_DEVICES: frozenset[str] = frozenset({"wayland-socket"})

# Device names that belong to the gpg integration and must be skipped
# when ``gpg.enabled`` is false.
GPG_DEVICES: frozenset[str] = frozenset({"gpg-socket"})


def _devices_for_host(cfg: Config) -> dict[str, str]:
    """Subset of ``SOCKET_DEVICES`` that applies to this host and config.

    Two devices are conditional:

    - ``wayland-socket`` only exists on Wayland hosts; on X11 the source
      path /run/user/<uid>/wayland-0 is absent and Incus rejects the add.
    - ``gpg-socket`` belongs to the gpg integration. With
      ``gpg.enabled: false`` the host may run no gpg-agent at all, so
      /run/user/<uid>/gnupg is likewise absent — and mounting the host's
      agent socket is exactly what that switch turns off.
    """
    skip: set[str] = set()
    if not host_is_wayland():
        skip |= WAYLAND_ONLY_DEVICES
    if not cfg.gpg.enabled:
        skip |= GPG_DEVICES
    return {k: v for k, v in SOCKET_DEVICES.items() if k not in skip}


# How long to wait for logind to provision /run/user/<uid> before giving
# up. On a typical host this completes within ~1s of `incus start`
# returning, but cold-cache fresh container starts can take longer.
DEFAULT_LOGIND_TIMEOUT_S = 15.0
DEFAULT_LOGIND_POLL_INTERVAL_S = 0.25


def attach_runtime_devices(
    cfg: Config,
    incus: Incus,
    name: str,
    *,
    timeout_s: float = DEFAULT_LOGIND_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_LOGIND_POLL_INTERVAL_S,
    sleep_fn: object = time.sleep,
) -> bool:
    """Attach the applicable GUI/IPC socket devices once logind has
    provisioned ``/run/user/<uid>`` for the dev user.

    Returns True if all devices were attached, False if logind didn't
    materialise the directory in time. Failure is non-fatal: the user can
    still ``jailbee shell`` and run non-GUI commands.

    The ``sleep_fn`` argument is for test injection only.
    """
    uid = cfg.container_user.uid
    runtime_dir = f"/run/user/{uid}"

    if not _wait_for_logind_runtime_dir(
        incus,
        name,
        uid,
        runtime_dir,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        sleep_fn=sleep_fn,
    ):
        warn(
            f"Timed out waiting for logind to provision {runtime_dir} "
            f"in {name}. GUI sockets not attached — `jailbee ide`/`jailbee chrome` "
            f"will fail. Try `jailbee restart {name}`."
        )
        return False

    devices = _devices_for_host(cfg)
    for device_name, basename in devices.items():
        path = f"{runtime_dir}/{basename}"
        try:
            incus.config_device_add(
                name,
                device_name,
                "disk",
                {"source": path, "path": path},
            )
        except IncusError as e:
            # Most likely cause: device already exists from a previous
            # attach (e.g. user ran `jailbee start` twice without intervening
            # stop). Tolerate it — the existing mount is the right one.
            if "already exists" in str(e).lower():
                continue
            raise

    skipped = set(SOCKET_DEVICES) - set(devices)
    display_skipped = sorted(skipped & WAYLAND_ONLY_DEVICES)
    config_skipped = sorted(skipped - WAYLAND_ONLY_DEVICES)
    if display_skipped:
        # The missing socket is the display itself, so this is a warning
        # rather than a note: audio, D-Bus and GnuPG still reach the
        # container, but there is nothing for a window to appear on.
        warn(
            f"Attached GUI sockets to {name}, minus {', '.join(display_skipped)} — "
            "this is not a Wayland session, so GUI launches will not display."
        )
    else:
        info(f"Attached GUI sockets to {name}")
    if config_skipped:
        # Config-driven omissions are what the user asked for, so they
        # stay a note even when the display warning fires alongside.
        info(f"Skipped {', '.join(config_skipped)} for {name} — disabled in config.")
    return True


def detach_runtime_devices(
    cfg: Config,
    incus: Incus,
    name: str,
) -> None:
    """Remove the four socket devices. Tolerates devices that don't exist
    (defensive — call before `start` to ensure no leftover devices race
    with logind on the next boot).
    """
    _ = cfg  # kept for symmetry / future config-driven device list
    for device_name in SOCKET_DEVICES:
        try:
            incus.config_device_remove(name, device_name)
        except IncusError as e:
            # Device wasn't attached — fine, that's the steady-state for
            # fresh containers and after a previous detach.
            if "doesn't exist" in str(e).lower() or "not found" in str(e).lower():
                continue
            raise


def _wait_for_logind_runtime_dir(
    incus: Incus,
    name: str,
    uid: int,
    runtime_dir: str,
    *,
    timeout_s: float,
    poll_interval_s: float,
    sleep_fn: object,
) -> bool:
    """Poll ``stat -c '%u' <runtime_dir>`` until it returns the dev UID."""
    deadline = time.monotonic() + timeout_s
    expected_uid = str(uid)
    while True:
        try:
            owner_uid = incus.exec(
                name,
                ["stat", "-c", "%u", runtime_dir],
            ).strip()
        except IncusError:
            owner_uid = ""
        if owner_uid == expected_uid:
            return True
        if time.monotonic() >= deadline:
            return False
        # mypy: sleep_fn is typed object for test injection — call it.
        sleep_fn(poll_interval_s)  # type: ignore[operator]
