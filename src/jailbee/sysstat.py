"""Host-wide memory, load average and filesystem readings.

The host-wide counterpart of ``procstat.py``: where that module reads one
container's processes, this one reads the whole host's memory from
``/proc/meminfo``, load from ``/proc/loadavg`` and filesystem free space via
``os.statvfs``. It runs no process and holds no state, and every reader returns None rather
than raising, so a dashboard gather never fails over a missing figure.

The module imports nothing from ``jailbee`` and takes its /proc root as a
parameter, so the tests drive it against ``tmp_path`` rather than the host.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class MemoryStatus:
    """Host memory, in bytes."""

    total: int
    available: int
    cache: int  # reclaimable page cache, capped at `available`
    swap_total: int = 0  # 0 when the host has no swap
    swap_free: int = 0

    @property
    def used(self) -> int:
        return self.total - self.available

    @property
    def free_ratio(self) -> float:
        return self.available / self.total if self.total else 0.0

    @property
    def swap_used(self) -> int:
        return self.swap_total - self.swap_free

    @property
    def swap_free_ratio(self) -> float:
        return self.swap_free / self.swap_total if self.swap_total else 0.0


@dataclass(frozen=True)
class LoadStatus:
    """One-minute host load and the host's CPU count."""

    load1: float
    cpus: int

    @property
    def per_cpu(self) -> float:
        return self.load1 / self.cpus


def read_load(*, proc_root: Path = PROC_ROOT, cpus: int | None = None) -> LoadStatus | None:
    """Read the one-minute load average, or None if unavailable or malformed."""
    try:
        fields = (proc_root / "loadavg").read_text(encoding="utf-8", errors="replace").split()
        load1 = float(fields[0])
    except (OSError, IndexError, ValueError):
        return None
    if not math.isfinite(load1) or load1 < 0:
        return None
    count = (os.cpu_count() or 1) if cpus is None else cpus
    return LoadStatus(load1=load1, cpus=max(1, count))


@dataclass(frozen=True)
class DiskStatus:
    """A filesystem's capacity, in bytes; `available` is what unprivileged users can use."""

    total: int
    available: int

    @property
    def used(self) -> int:
        return self.total - self.available

    @property
    def free_ratio(self) -> float:
        return self.available / self.total if self.total else 0.0


def _meminfo_fields(text: str) -> dict[str, int]:
    """``Name: value kB`` lines as a name → bytes mapping; malformed lines are skipped."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            fields[name.strip()] = int(parts[0]) * 1024
        except ValueError:
            continue
    return fields


def read_memory(*, proc_root: Path = PROC_ROOT) -> MemoryStatus | None:
    """Host memory from ``meminfo``, or None if unreadable or incomplete.

    The cache figure counts what the kernel can give back under pressure:
    buffers, page cache and reclaimable slab, less shared memory, which
    cannot be reclaimed. It never exceeds the available figure.
    """
    try:
        text = (proc_root / "meminfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields = _meminfo_fields(text)
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    if total is None or available is None:
        return None
    cache = max(
        0,
        fields.get("Buffers", 0)
        + fields.get("Cached", 0)
        + fields.get("SReclaimable", 0)
        - fields.get("Shmem", 0),
    )
    swap_total = fields.get("SwapTotal", 0)
    return MemoryStatus(
        total=total,
        available=available,
        cache=min(cache, available),
        swap_total=swap_total,
        swap_free=min(fields.get("SwapFree", 0), swap_total),
    )


def read_disk(path: Path) -> DiskStatus | None:
    """Capacity of the filesystem holding `path`, or None on OSError."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return DiskStatus(
        total=st.f_blocks * st.f_frsize,
        available=st.f_bavail * st.f_frsize,
    )


def same_filesystem(a: Path, b: Path) -> bool:
    """Whether `a` and `b` live on the same device. False if either is missing."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False
