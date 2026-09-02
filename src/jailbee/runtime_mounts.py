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
from pathlib import Path

from jailbee.config import Config
from jailbee.gui import host_is_wayland, host_wayland_socket
from jailbee.incus import Incus, IncusError
from jailbee.tui import info, warn

WAYLAND_DEVICE = "wayland-socket"
"""Incus device name of the compositor socket mount.

The *device* name is fixed even though the socket it points at is not, so
``detach_runtime_devices`` can drop it without knowing which session
attached it.
"""

# Device names → relative path under /run/user/<uid>, for the sockets whose
# name is the same on every host. Source and target paths are identical
# (host /run/user/<host_uid>/X → container same path).
#
# WAYLAND_DEVICE is deliberately absent: its basename is the host's
# $WAYLAND_DISPLAY, resolved per session by `_socket_devices`.
_FIXED_SOCKET_BASENAMES: dict[str, str] = {
    "pulse-socket": "pulse",
    "dbus-socket": "bus",
    "gpg-socket": "gnupg",
}

# Every device this module attaches and detaches, session-independent.
SOCKET_DEVICES: frozenset[str] = frozenset({WAYLAND_DEVICE, *_FIXED_SOCKET_BASENAMES})

# Device names that belong to the gpg integration and must be skipped
# when ``gpg.enabled`` is false.
GPG_DEVICES: frozenset[str] = frozenset({"gpg-socket"})

# Device names whose host source is a *directory* inside the host's own
# /run/user/<uid>, mounted read-only.
#
# The container runs its own ``systemd --user`` (the golden image enables
# linger, which is what provisions /run/user/<uid> in the first place). Its
# gnupg and pulse socket units listen on paths *inside these mounts* and
# unlink whatever file is already there before binding. On a writable mount
# that unlink deletes the **host's** sockets: the host gpg-agent logs "socket
# file has been removed - shutting down" and the container's agent — which
# has no smartcard access — answers in its place, so the host silently loses
# its YubiKey keys mid-session because a container rebooted. Read-only turns
# that unlink into EROFS.
#
# Safe because connecting to a unix socket needs no writable filesystem, only
# permission on the socket inode, and because read-only binds the container's
# side alone: the host stays free to unlink and re-create its own sockets in
# the directory, and the container sees the new ones.
#
# ``wayland-socket`` and ``dbus-socket`` are single-file mounts, where
# unlinking the bind-mount point returns EBUSY — already protected.
READONLY_DEVICES: frozenset[str] = frozenset({"gpg-socket", "pulse-socket"})

WAYLAND_DISPLAY_ENV = "WAYLAND_DISPLAY"
WAYLAND_DISPLAY_KEY = f"environment.{WAYLAND_DISPLAY_ENV}"
"""Instance config key holding the socket name for this boot.

The `<prefix>-base` profile renders the same key, but only when `jailbee
apply` runs — an apply-time snapshot of whatever session happened to invoke
it. The socket name is session state, so a reboot that renumbers it (a
nested compositor, a different login order) leaves that snapshot naming a
socket the container no longer has, while the *mount* — recomputed on every
boot — is right. Setting it per container, next to the mount that decides
it, keeps the two in lockstep; the profile stays the fallback for a
container jailbee has not booted (one created by an older version, or
started with plain `incus start`).

Instance config outranks profile config in Incus, which is the precedence
we want — with one exception, see `_pin_wayland_display`.
"""


def _socket_devices() -> dict[str, str]:
    """Device name → basename under /run/user/<uid> for *this* session.

    Only the compositor socket varies: its name is whatever the host's
    ``$WAYLAND_DISPLAY`` says.
    """
    return {WAYLAND_DEVICE: host_wayland_socket(), **_FIXED_SOCKET_BASENAMES}


def _host_path_exists(path: str) -> bool:
    """Whether a host path is there. Separate function so tests can say."""
    return Path(path).exists()


def _wayland_skip_reason(runtime_dir: str) -> str | None:
    """Why the compositor socket cannot be mounted, or None if it can.

    Returned text completes "minus wayland-socket — <reason>, so GUI
    launches will not display", so phrase it as a clause.

    Two ways to have no socket to mount. An X11 (or headless) session
    never had one. And ``$WAYLAND_DISPLAY`` can name a socket that isn't
    there: a stale value inherited by a long-lived tmux or ssh environment
    after the compositor restarted, or an absolute path — which the
    Wayland spec allows — that is not under ``$XDG_RUNTIME_DIR`` at all.
    Either way Incus rejects a disk device whose source is missing, and
    that used to abort the whole create.
    """
    if not host_is_wayland():
        return "this is not a Wayland session"
    source = f"{runtime_dir}/{host_wayland_socket()}"
    if not _host_path_exists(source):
        return f"$WAYLAND_DISPLAY names {source}, which does not exist"
    return None


def _devices_for_host(cfg: Config, *, skip_wayland: bool) -> dict[str, str]:
    """Subset of ``_socket_devices()`` that applies to this host and config.

    Two devices are conditional:

    - the compositor socket, per ``skip_wayland`` (see
      ``_wayland_skip_reason``, which is also what phrases it for the user).
    - ``gpg-socket`` belongs to the gpg integration. With
      ``gpg.enabled: false`` the host may run no gpg-agent at all, so
      /run/user/<uid>/gnupg is likewise absent — and mounting the host's
      agent socket is exactly what that switch turns off.
    """
    skip: set[str] = set()
    if skip_wayland:
        skip.add(WAYLAND_DEVICE)
    if not cfg.gpg.enabled:
        skip |= GPG_DEVICES
    return {k: v for k, v in _socket_devices().items() if k not in skip}


def _pin_wayland_display(
    cfg: Config,
    incus: Incus,
    name: str,
    *,
    socket: str | None,
) -> None:
    """Point the container's ``WAYLAND_DISPLAY`` at the socket just mounted.

    ``socket=None`` means none was mounted; the key is then cleared rather
    than left naming a socket from an earlier boot that is no longer there.

    A ``WAYLAND_DISPLAY`` in ``container.env`` is documented to win over the
    value jailbee picks (`ContainerConfig`), and it is rendered into the
    profile — which an instance-level key would outrank. So that case clears
    the key too, leaving the user's profile value in force.

    Takes effect without a restart: Incus reads ``environment.*`` from the
    instance config on every ``incus exec``, and this runs before the
    autostart steps.
    """
    if socket is None or WAYLAND_DISPLAY_ENV in cfg.container.env:
        incus.config_unset(name, WAYLAND_DISPLAY_KEY)
        return
    incus.config_set(name, WAYLAND_DISPLAY_KEY, socket)


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

    wayland_skip_reason = _wayland_skip_reason(runtime_dir)
    devices = _devices_for_host(cfg, skip_wayland=wayland_skip_reason is not None)
    for device_name, basename in devices.items():
        path = f"{runtime_dir}/{basename}"
        device_config = {"source": path, "path": path}
        if device_name in READONLY_DEVICES:
            device_config["readonly"] = "true"
        try:
            incus.config_device_add(
                name,
                device_name,
                "disk",
                device_config,
            )
        except IncusError as e:
            # Most likely cause: device already exists from a previous
            # attach (e.g. user ran `jailbee start` twice without intervening
            # stop). Tolerate it — the existing mount is the right one.
            if "already exists" in str(e).lower():
                continue
            raise

    _pin_wayland_display(
        cfg,
        incus,
        name,
        socket=None if wayland_skip_reason is not None else host_wayland_socket(),
    )

    config_skipped = sorted(set(SOCKET_DEVICES) - set(devices) - {WAYLAND_DEVICE})
    if wayland_skip_reason is not None:
        # The missing socket is the display itself, so this is a warning
        # rather than a note: audio, D-Bus and GnuPG still reach the
        # container, but there is nothing for a window to appear on.
        warn(
            f"Attached GUI sockets to {name}, minus {WAYLAND_DEVICE} — "
            f"{wayland_skip_reason}, so GUI launches will not display."
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
    """Remove the four socket devices and the boot's ``WAYLAND_DISPLAY``.
    Tolerates devices that don't exist (defensive — call before `start` to
    ensure no leftover devices race with logind on the next boot).

    Clearing the env key matters when the next boot's attach never gets to
    run (logind timeout): the container then falls back to the profile's
    value instead of keeping this session's.
    """
    _ = cfg  # kept for symmetry / future config-driven device list
    incus.config_unset(name, WAYLAND_DISPLAY_KEY)
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
