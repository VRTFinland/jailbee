"""Optional bind mounts (e.g. ``~/.aws``) added/removed on demand."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

from jailbee.config import Config
from jailbee.incus import Incus, IncusError
from jailbee.db import state_dir

DEVICE_NAME_PREFIX = "optional-"
MOUNT_UNTIL_PREFIX = "user.jailbee.mount_until."


@contextmanager
def mount_lock() -> Iterator[None]:
    """Serialize JailBee optional-mount transactions across host processes."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "optional-mounts.lock").open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def until_key(kind: str) -> str:
    """The instance label holding ``kind``'s auto-unmount deadline."""
    return f"{MOUNT_UNTIL_PREFIX}{kind}"


def mount_until(config: Mapping[str, object]) -> dict[str, datetime]:
    """Deadlines by kind from an instance's config; malformed labels are skipped."""
    out: dict[str, datetime] = {}
    for key, raw in config.items():
        if not key.startswith(MOUNT_UNTIL_PREFIX) or not isinstance(raw, str) or not raw:
            continue
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            continue
        out[key.removeprefix(MOUNT_UNTIL_PREFIX)] = deadline
    return out


def is_attached(incus: Incus, container: str, kind: str) -> bool:
    """True when the container already carries ``kind``'s device."""
    return incus.config_device_get(container, f"{DEVICE_NAME_PREFIX}{kind}", "source") is not None


def add_optional_mount(cfg: Config, incus: Incus, container: str, kind: str) -> None:
    """Add an optional bind mount to the container."""
    if kind not in cfg.optional_mounts:
        raise ValueError(f"Unknown optional mount '{kind}'. Available: {list(cfg.optional_mounts)}")
    mount = cfg.optional_mounts[kind]
    props: dict[str, str] = {
        "source": str(mount.host),
        "path": mount.container,
    }
    if mount.readonly:
        props["readonly"] = "true"
    incus.config_device_add(container, f"{DEVICE_NAME_PREFIX}{kind}", "disk", props)


def remove_optional_mount(cfg: Config, incus: Incus, container: str, kind: str) -> None:
    """Remove an optional bind mount from the container."""
    if kind not in cfg.optional_mounts:
        raise ValueError(f"Unknown optional mount '{kind}'")
    with mount_lock():
        incus.config_device_remove(container, f"{DEVICE_NAME_PREFIX}{kind}")
        incus.config_unset_checked(container, until_key(kind))


def attach(cfg: Config, incus: Incus, container: str, kind: str, until: datetime | None) -> bool:
    """Attach ``kind`` if needed and set or clear its deadline label.

    Returns True when the device was added, or False when it was already
    attached and only its deadline changed.
    """
    if kind not in cfg.optional_mounts:
        raise ValueError(f"Unknown optional mount '{kind}'. Available: {list(cfg.optional_mounts)}")
    with mount_lock():
        added = not is_attached(incus, container, kind)
        if added:
            add_optional_mount(cfg, incus, container, kind)
        try:
            if until is None:
                incus.config_unset_checked(container, until_key(kind))
            else:
                incus.config_set(container, until_key(kind), until.isoformat())
        except Exception as error:
            if added:
                try:
                    incus.config_device_remove(container, f"{DEVICE_NAME_PREFIX}{kind}")
                except Exception as rollback_error:
                    raise IncusError(
                        f"Mount deadline failed: {error}; rollback failed for "
                        f"{container}/{kind}: {rollback_error}"
                    ) from error
            raise
        return added


def attached_kinds(devices: Mapping[str, object]) -> tuple[str, ...]:
    """The optional-mount kinds among a container's devices."""
    return tuple(
        sorted(
            device.removeprefix(DEVICE_NAME_PREFIX)
            for device in devices
            if device.startswith(DEVICE_NAME_PREFIX)
        )
    )
