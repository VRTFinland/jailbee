"""Expose only PR identity labels to the container, never host configuration."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.container_locks import locked
from jailbee.container_outbox.bindings import ERROR_MARKER, KEYS
from jailbee.paths import xdg_data_home

if TYPE_CHECKING:
    from jailbee.config import Config
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


def _legacy_repo_dir(full: str, cfg: Config | None) -> str:
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.incus import IncusError

    if cfg is not None:
        root = cfg.repo_root
    else:
        from sqlmodel import Session, select

        from jailbee.db import get_engine
        from jailbee.db.models import RegisteredRepo

        with Session(get_engine()) as session:
            repos = [
                row
                for row in session.exec(select(RegisteredRepo))
                if full.startswith(row.container_prefix + "-")
            ]
            if not repos:
                raise IncusError("legacy repository path unavailable; run jb start in its repo")
            root = Path(max(repos, key=lambda row: len(row.container_prefix)).repo_root)
    # Match lifecycle.container_repo_dir's pre-label fallback, never the host path.
    return f"/home/{CONTAINER_USERNAME}/{root.name}"


def _running(incus: Incus, full: str) -> bool:
    return any(
        row.get("name") == full and row.get("status") == "Running"
        for row in incus.list_containers()
    )


def _writer_uid(incus: Incus, full: str, cfg: Config | None) -> int:
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.incus import IncusError

    if cfg is not None:
        uid = cfg.container_user.uid
    else:
        # The publishing state objects do not carry Config. Ask the actual
        # container instead of guessing its writer from the host's identity.
        value = incus.exec(full, ["id", "-u", CONTAINER_USERNAME]).strip()
        if not value.isascii() or not value.isdecimal():
            raise IncusError("could not resolve container user UID; run jb start in its repo")
        uid = int(value)
    if uid <= 0:
        raise IncusError("container binding infrastructure requires a non-root user UID")
    return uid


def sync(incus: Incus, full: str, *, cfg: Config | None = None) -> None:
    """Serialize authoritative reads, publication and mount repair across host processes."""
    with locked(full):
        _sync_locked(incus, full, cfg=cfg)


def _sync_locked(incus: Incus, full: str, *, cfg: Config | None) -> None:
    from jailbee.incus import IncusError

    directory = metadata_root() / full
    directory.mkdir(parents=True, exist_ok=True)
    running = _running(incus, full)
    try:
        if running:
            uid = _writer_uid(incus, full, cfg)
            # Guard the fixed container path, not an arbitrary device source.
            incus.exec(
                full,
                ["sh", "-c", 'set -eu; mkdir -p "${1%/*}"; touch "$1"', "sh", str(ERROR_MARKER)],
                uid=uid,
            )
        config = {}
        for key in KEYS:
            value = incus.config_get_checked(full, key)
            if value is not None:
                if not isinstance(value, str):
                    raise IncusError("invalid binding label response")
                config[key] = value
        if not config.get(KEYS[0]):
            config[KEYS[0]] = _legacy_repo_dir(full, cfg)
        _write(directory, config)
        properties = {
            "source": str(directory),
            "path": "/opt/jailbee/metadata",
            "readonly": "true",
        }
        existing = incus.config_device_get(full, "jailbee-metadata", "type")
        if existing is None:
            incus.config_device_add(full, "jailbee-metadata", "disk", properties)
        else:
            if existing != "disk":
                raise IncusError("jailbee-metadata is not a disk device; repair it before retrying")
            if any(
                incus.config_device_get(full, "jailbee-metadata", key) != value
                for key, value in properties.items()
            ):
                incus.config_device_set(full, "jailbee-metadata", properties)
        if running:
            incus.exec(full, ["rm", "-f", "--", str(ERROR_MARKER)], uid=uid)
    except (IncusError, OSError):
        # Includes device operations: never hide a mount repair failure as success.
        _write(directory, {"error": "PR bindings could not be refreshed; restart with jb start"})
        raise


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
