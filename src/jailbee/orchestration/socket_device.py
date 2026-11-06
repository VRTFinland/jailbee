"""The per-container broker socket directory, as an Incus disk device."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from jailbee.incus import ORCHESTRATION_DEVICE

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jailbee.incus import Incus

CONTAINER_PATH = "/run/jailbee/orchestration"


def sock_dir(root: Path, uuid: str) -> Path:
    return root / "sock" / uuid


def device_config(root: Path, uuid: str) -> dict[str, str]:
    return {
        "type": "disk",
        "source": str(sock_dir(root, uuid)),
        "path": CONTAINER_PATH,
        "readonly": "true",
        "required": "false",
    }


def attach(incus: Incus, full: str, root: Path, uuid: str, *, reassert: bool) -> None:
    """Make ``full`` carry the device for ``uuid``; no-op when it already does."""
    config = device_config(root, uuid)
    if not reassert and incus.config_device_get(full, ORCHESTRATION_DEVICE, "source") == str(
        sock_dir(root, uuid)
    ):
        return
    incus.config_device_remove(full, ORCHESTRATION_DEVICE, missing_ok=True)
    incus.config_device_add(
        full, ORCHESTRATION_DEVICE, "disk", {k: v for k, v in config.items() if k != "type"}
    )


def detach(incus: Incus, full: str) -> None:
    incus.config_device_remove(full, ORCHESTRATION_DEVICE, missing_ok=True)


def has_device(entry: Mapping[str, object]) -> bool:
    devices = entry.get("devices")
    return isinstance(devices, dict) and ORCHESTRATION_DEVICE in devices


def mismatched(entry: Mapping[str, object]) -> bool:
    """Whether the entry carries a device that points at another instance's socket."""
    devices = entry.get("devices")
    if not isinstance(devices, dict):
        return False
    device = devices.get(ORCHESTRATION_DEVICE)
    if not isinstance(device, dict):
        return False
    config = entry.get("config")
    uuid = config.get("volatile.uuid") if isinstance(config, dict) else None
    source = device.get("source")
    return PurePosixPath(str(source or "")).name != uuid
