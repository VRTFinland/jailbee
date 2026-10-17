"""Host memory, storage pool and root filesystem status for the dashboards.

`SystemReader` is held by the gatherer. Memory and the root filesystem are
cheap local reads, taken on every call. Pool usage costs Incus calls, so it
is re-read at most once per ``interval`` (or when the set of pools in use
changes) and the last good reading is carried forward in between and when
Incus fails. Nothing here raises out of `read`.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from jailbee.sysstat import (
    PROC_ROOT,
    DiskStatus,
    MemoryStatus,
    read_disk,
    read_memory,
    same_filesystem,
)

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup
    from jailbee.incus import Incus

log = logging.getLogger(__name__)

WARN_FREE = 0.15
CRIT_FREE = 0.05


def level(free_ratio: float) -> Literal["ok", "warn", "crit"]:
    """Severity of a free-space ratio; the boundaries themselves are not yet worse."""
    if free_ratio < CRIT_FREE:
        return "crit"
    if free_ratio < WARN_FREE:
        return "warn"
    return "ok"


@dataclass(frozen=True)
class PoolStatus:
    name: str
    disk: DiskStatus


@dataclass(frozen=True)
class SystemStatus:
    memory: MemoryStatus | None
    pools: tuple[PoolStatus, ...]
    # None when unreadable or when it shares a filesystem with a pool.
    root: DiskStatus | None


def pool_names(groups: Sequence[RepoGroup]) -> tuple[str, ...]:
    """Sorted distinct root pools of every container in `groups`."""
    return tuple(
        sorted({c.root_pool for g in groups for c in g.containers if c.root_pool is not None})
    )


class SystemReader:
    def __init__(
        self,
        *,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        proc_root: Path = Path(PROC_ROOT),
        root: Path = Path("/"),
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._proc_root = proc_root
        self._root = root
        self._pools: tuple[PoolStatus, ...] = ()
        self._sources: tuple[str, ...] = ()
        self._names: tuple[str, ...] | None = None
        self._last: float | None = None
        self._default_pool: str | None = None

    def read(self, incus: Incus, pool_names: Sequence[str]) -> SystemStatus:
        names = tuple(sorted(set(pool_names)))
        now = self._clock()
        due = self._last is None or now - self._last >= self._interval or names != self._names
        if due:
            self._last = now
            self._names = names
            try:
                self._refresh(incus, names)
            except Exception as exc:
                log.debug("storage pool read failed: %s", exc)
        return SystemStatus(
            memory=read_memory(proc_root=self._proc_root),
            pools=self._pools,
            root=self._root_status(),
        )

    def _refresh(self, incus: Incus, names: tuple[str, ...]) -> None:
        wanted = names or self._fallback_names(incus)
        listed = {str(p.get("name")): p for p in incus.list_storage_pools()}
        pools: list[PoolStatus] = []
        sources: list[str] = []
        for name in wanted:
            space = incus.storage_pool_resources(name).get("space") or {}
            total, used = space.get("total"), space.get("used")
            if not isinstance(total, int) or not isinstance(used, int):
                raise ValueError(f"pool {name!r} reported no usable space")
            pools.append(
                PoolStatus(name=name, disk=DiskStatus(total=total, available=total - used))
            )
            source = ((listed.get(name) or {}).get("config") or {}).get("source")
            if isinstance(source, str):
                sources.append(source)
        self._pools = tuple(pools)
        self._sources = tuple(sources)

    def _fallback_names(self, incus: Incus) -> tuple[str, ...]:
        if self._default_pool is None:
            profile: Any = yaml.safe_load(incus.profile_show("default")) or {}
            pool = ((profile.get("devices") or {}).get("root") or {}).get("pool")
            if not isinstance(pool, str) or not pool:
                return ()
            self._default_pool = pool
        return (self._default_pool,)

    def _root_status(self) -> DiskStatus | None:
        for source in self._sources:
            path = Path(source)
            if not path.is_absolute():
                continue  # a block device name or a zfs dataset is never the root's filesystem
            probe = _probe_path(path)
            if probe is not None and same_filesystem(probe, self._root):
                return None
        return read_disk(self._root)


def _probe_path(path: Path) -> Path | None:
    """`path`, or its nearest ancestor that can be stat'ed when `path` itself cannot.

    Incus keeps loop images in a root-only directory the unprivileged state
    service cannot enter; the directory above it decides the device just as
    well. None when `path` is missing or nothing above it can be stat'ed.
    """
    probe = path
    while True:
        try:
            os.stat(probe)
            return probe
        except PermissionError:
            if probe.parent == probe:
                return None
            probe = probe.parent
        except OSError:
            return None
