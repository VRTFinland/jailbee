"""Expose only PR identity labels to the container, never host configuration."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.container_outbox.bindings import KEYS
from jailbee.paths import xdg_data_home

if TYPE_CHECKING:
    from jailbee.incus import Incus


def metadata_root() -> Path:
    return xdg_data_home() / "jailbee" / "container-metadata"


def _write(directory: Path, value: dict[str, str]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".bindings-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, directory / "bindings.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync(incus: Incus, full: str) -> None:
    """Refresh a directory mount atomically; works for running and stopped instances."""
    from jailbee.incus import IncusError

    if not full or Path(full).name != full or full in (".", ".."):
        raise ValueError("invalid container name")
    directory = metadata_root() / full
    directory.mkdir(parents=True, exist_ok=True)
    try:
        config = {}
        for key in KEYS:
            value = incus.config_get_checked(full, key)
            if value is not None:
                if not isinstance(value, str):
                    raise IncusError("invalid binding label response")
                config[key] = value
        _write(directory, config)
    except (IncusError, OSError):
        # Never let a failed refresh keep selecting an old identity.
        _write(directory, {"error": "PR bindings could not be refreshed; restart with jb start"})
        raise
    if incus.config_device_get(full, "jailbee-metadata", "source") != str(directory):
        incus.config_device_add(
            full,
            "jailbee-metadata",
            "disk",
            {
                "source": str(directory),
                "path": "/opt/jailbee/metadata",
                "readonly": "true",
            },
        )


def refresh(incus: Incus, full: str) -> bool:
    """PR publication may already have succeeded; report snapshot failures as warnings."""
    from jailbee.incus import IncusError
    from jailbee.tui import warn

    try:
        sync(incus, full)
    except (IncusError, OSError) as exc:
        warn(f"Could not refresh container PR bindings: {exc}; restart with jb start")
        return False
    return True
