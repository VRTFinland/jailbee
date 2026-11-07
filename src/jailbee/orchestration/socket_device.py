"""The per-container broker socket directory, as an Incus disk device."""

from __future__ import annotations

import secrets
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING

from jailbee.incus import ORCHESTRATION_DEVICE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jailbee.incus import Incus

# Not under /run: systemd's boot-time tmpfs would hide Incus's pre-init mount.
CONTAINER_PATH = "/opt/jailbee/orchestration"
_GENERATION_SEP = "."


def fresh_sock_dir(root: Path, uuid: str) -> Path:
    """A socket directory for ``uuid`` no device has ever named.

    Every new listener gets one (``<uuid>.<random>``, a sibling, never a child,
    of earlier ones), so a stale mount of an earlier directory, held by a
    copy whose detach failed or was lazy, can never reach the socket again.
    """
    return root / "sock" / f"{uuid}{_GENERATION_SEP}{secrets.token_hex(8)}"


def dir_uuid(path: PurePath | str) -> str:
    """The uuid a socket directory belongs to (legacy or generation name)."""
    return PurePosixPath(str(path)).name.split(_GENERATION_SEP, 1)[0]


def reusable_dir(root: Path, uuid: str, source: str | None) -> Path | None:
    """The device's recorded directory when it is ``uuid``'s and still exists.

    A broker restart keeps serving there (no device churn); a closed or
    rotated directory is removed, so it is never reused. A legacy
    ``sock/<uuid>`` directory is never reused either: the broker before
    generations reset a copy's socket in place, so a copy may hold it.
    """
    if not source:
        return None
    path = Path(source)
    if path.parent != root / "sock" or dir_uuid(path) != uuid:
        return None
    if path.name == uuid:
        return None
    if path.is_symlink() or not path.is_dir():
        return None
    return path


def remove_dir(root: Path, source: str) -> bool:
    """Delete an unserved socket directory a foreign device names; True when removed.

    Only a directory directly under ``<root>/sock`` is touched, never through
    a symlink: a copy holding it then holds a deleted directory that no later
    socket can appear in.
    """
    path = Path(source)
    if path.parent != root / "sock" or path.is_symlink() or not path.is_dir():
        return False
    for child in path.iterdir():
        if child.is_symlink() or not child.is_dir():
            child.unlink()
    path.rmdir()
    return True


def device_config(directory: Path) -> dict[str, str]:
    return {
        "type": "disk",
        "source": str(directory),
        "path": CONTAINER_PATH,
        "readonly": "true",
        "required": "false",
    }


def attach(incus: Incus, full: str, directory: Path, *, reassert: bool) -> None:
    """Make ``full`` carry the device for ``directory``; no-op when it already does."""
    config = device_config(directory)
    if (
        not reassert
        and incus.config_device_get(full, ORCHESTRATION_DEVICE, "source") == str(directory)
        and incus.config_device_get(full, ORCHESTRATION_DEVICE, "path") == CONTAINER_PATH
    ):
        return
    incus.config_device_remove(full, ORCHESTRATION_DEVICE, missing_ok=True)
    incus.config_device_add(
        full, ORCHESTRATION_DEVICE, "disk", {k: v for k, v in config.items() if k != "type"}
    )


def detach(incus: Incus, full: str, *, project: str | None = None) -> None:
    if project is None:
        incus.config_device_remove(full, ORCHESTRATION_DEVICE, missing_ok=True)
    else:
        incus.config_device_remove(full, ORCHESTRATION_DEVICE, missing_ok=True, project=project)


def has_device(entry: Mapping[str, object]) -> bool:
    devices = entry.get("devices")
    return isinstance(devices, dict) and ORCHESTRATION_DEVICE in devices


def source(entry: Mapping[str, object]) -> str | None:
    """The directory the entry's device mounts, if it has one."""
    devices = entry.get("devices")
    if not isinstance(devices, dict):
        return None
    device = devices.get(ORCHESTRATION_DEVICE)
    if not isinstance(device, dict):
        return None
    return str(device.get("source") or "")


def mount_path(entry: Mapping[str, object]) -> str | None:
    """The device's container-side mount point, if it has one."""
    devices = entry.get("devices")
    if not isinstance(devices, dict):
        return None
    device = devices.get(ORCHESTRATION_DEVICE)
    if not isinstance(device, dict):
        return None
    return str(device.get("path") or "")


def source_uuid(entry: Mapping[str, object]) -> str | None:
    """The uuid whose socket directory the entry's device mounts, if it has one."""
    path = source(entry)
    return None if path is None else dir_uuid(path)


def mismatched(entry: Mapping[str, object]) -> bool:
    """Whether the entry carries a device that points at another instance's socket."""
    source = source_uuid(entry)
    if source is None:
        return False
    config = entry.get("config")
    uuid = config.get("volatile.uuid") if isinstance(config, dict) else None
    return source != uuid
