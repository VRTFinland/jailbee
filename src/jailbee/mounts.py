"""Optional bind mounts (e.g. ``~/.aws``) added/removed on demand."""

from __future__ import annotations

from jailbee.config import Config
from jailbee.incus import Incus

DEVICE_NAME_PREFIX = "optional-"


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
    incus.config_device_remove(container, f"{DEVICE_NAME_PREFIX}{kind}")
