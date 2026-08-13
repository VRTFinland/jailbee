"""Maintenance utilities — disk-usage and prune."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jailbee.config import Config
from jailbee.global_config import GlobalConfig
from jailbee.incus import Incus
from jailbee.lifecycle import list_containers


@dataclass
class DiskRow:
    component: str
    # None means "not measurable" — e.g. the storage-pool container dirs are
    # root-only, so `du` as the normal user jailbee runs as can't read them. That
    # renders as "n/a" rather than a misleading 0.
    size_bytes: int | None
    path: str


def gather_disk_usage(cfg: Config, gcfg: GlobalConfig, incus: Incus) -> list[DiskRow]:
    """Gather disk-usage rows for a Rich table.

    Golden-image sizes come from the Incus API (``incus image list``), which
    works as a non-root user and is driver-independent — the on-disk
    ``/var/lib/incus/images`` dir is root-only, so ``du`` there returns 0 for
    the normal user. Container/snapshot data lives under each storage pool's
    ``source`` path; the ``/var/lib/incus/containers`` entries are symlinks
    into the pool that ``du`` won't follow. Measuring those dirs still needs
    root, so a normal user sees ``n/a`` (run ``sudo jailbee disk-usage`` for the
    figures).
    """
    assert cfg.shared_dir is not None  # set by load_config
    images_bytes = sum(int(img.get("size", 0) or 0) for img in incus.list_images())
    sources = _pool_sources(incus)
    return [
        DiskRow("Golden images", images_bytes, "incus image list"),
        DiskRow(
            "Containers",
            _du_pool_subdirs(sources, "containers"),
            _pool_subdir_display(sources, "containers"),
        ),
        DiskRow(
            "Container snapshots",
            _du_pool_subdirs(sources, "containers-snapshots"),
            _pool_subdir_display(sources, "containers-snapshots"),
        ),
        DiskRow(
            "Shared caches", _du_bytes(cfg.shared_dir / "caches"), str(cfg.shared_dir / "caches")
        ),
        DiskRow(
            "Docker registry mirror",
            _du_bytes(gcfg.docker_registry_mirror.data_dir),
            str(gcfg.docker_registry_mirror.data_dir),
        ),
    ]


def _pool_sources(incus: Incus) -> list[Path]:
    """Return the on-disk ``source`` path of every storage pool that has one."""
    sources: list[Path] = []
    for pool in incus.list_storage_pools():
        src = (pool.get("config") or {}).get("source")
        if src:
            sources.append(Path(src))
    return sources


def _pool_subdir_display(sources: list[Path], sub: str) -> str:
    """Human-readable path column: the pool subdir(s) being measured."""
    return ", ".join(str(s / sub) for s in sources) or f"(no storage pool)/{sub}"


def _du_pool_subdirs(sources: list[Path], sub: str) -> int | None:
    """Sum ``du`` over ``<source>/<sub>`` for every pool.

    Returns None if any pool's subdir is present-but-unreadable (root-only),
    since a partial sum would understate reality. Returns 0 when there are no
    pools or the dirs are simply empty/absent.
    """
    total = 0
    for src in sources:
        b = _du_bytes(src / sub)
        if b is None:
            return None
        total += b
    return total


def _du_bytes(path: Path) -> int | None:
    """Total bytes at path.

    Returns 0 if the path is absent, and None if it is present but ``du``
    can't read it (e.g. a root-only dir for the normal user jailbee runs as) —
    None surfaces as "n/a" instead of a misleading 0.
    """
    if not path.exists():
        return 0
    if shutil.which("du"):
        try:
            result = subprocess.run(
                ["du", "-sb", str(path)],
                capture_output=True,
                text=True,
                check=True,
            )
            return int(result.stdout.split()[0])
        except (subprocess.CalledProcessError, ValueError, OSError):
            return None
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def humanize(n: int | None) -> str:
    if n is None:
        return "n/a"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(n)
    for u in units:
        if val < 1024 or u == units[-1]:
            return f"{val:.1f} {u}"
        val /= 1024
    return f"{n} B"


def find_stale_stopped(cfg: Config, incus: Incus, days: int = 30) -> list[str]:
    """Return names of containers stopped for more than ``days`` days.

    Best-effort approximation: Incus does not directly expose 'last-stopped time'
    so we use the container's mtime under ``/var/lib/incus/containers/<name>``.
    """
    stale: list[str] = []
    now = datetime.now(UTC)
    for c in list_containers(cfg, incus, all_repos=True):
        if c.state != "Stopped":
            continue
        path = Path(f"/var/lib/incus/containers/{c.name}")
        if not path.exists():
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if now - mtime > timedelta(days=days):
            stale.append(c.name)
    return stale
