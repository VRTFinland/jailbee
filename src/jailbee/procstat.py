"""Per-container CPU activity, read from the host's own /proc and cgroups.

The dashboards answer two questions about a container — how hard is it
working, and what is doing the work — and this module answers both without
running a single process. That is deliberate: the alternative,
``incus exec <name> -- top``, costs a process (and, for an instantaneous
rather than lifetime-average figure, a sampling delay) per container per
refresh tick.

It is sound only because the process running the dashboard always shares a
kernel with the containers: ``incus.py`` drives the local ``incus`` client
with no remote flags, and on macOS ``macos.maybe_delegate`` hands the whole
command to the Linux VM, where it runs locally.

The module imports nothing from ``jailbee`` and takes its filesystem roots
as parameters, so the tests drive it against ``tmp_path`` rather than the
host's real /proc.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")

# Seconds between the two readings a delta needs. Short enough to be free
# next to an `incus list`, long enough that the tick counter has moved.
PRIME_INTERVAL_SECONDS = 0.2

# Below this share of one core a process is idling, not working — and
# without the cut the column fills with systemd, sshd and tmux.
ACTIVE_PROCESS_MIN_PERCENT = 5.0

try:
    CLOCK_TICKS = int(os.sysconf("SC_CLK_TCK"))
except (ValueError, OSError):  # pragma: no cover - every supported host has it
    CLOCK_TICKS = 100


@dataclass(frozen=True)
class ProcSample:
    """One process's cumulative CPU time at one instant."""

    comm: str
    ticks: int  # utime + stime, in clock ticks
    # proc(5) field 22. Two readings of the same pid with different start
    # times are two different processes: the pid was recycled, and the
    # counter went backwards rather than forwards.
    starttime: int


def read_process(pid: int, *, proc_root: Path = PROC_ROOT) -> ProcSample | None:
    """Read one process, or None if it is gone or unreadable.

    ``comm`` (field 2) is the one free-form field in ``stat``: it is wrapped
    in parentheses but may itself contain spaces and ``)``. Parsing
    therefore cuts at the *last* ``)``, never the first and never on
    whitespace — a process named ``) evil (`` would otherwise shift every
    later field.
    """
    try:
        raw = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return None
    open_at = raw.find("(")
    close_at = raw.rfind(")")
    if open_at < 0 or close_at < open_at:
        return None
    comm = raw[open_at + 1 : close_at]
    # `rest` starts at field 3, so field N sits at index N - 3:
    # utime (14) -> 11, stime (15) -> 12, starttime (22) -> 19.
    rest = raw[close_at + 2 :].split()
    try:
        return ProcSample(
            comm=comm,
            ticks=int(rest[11]) + int(rest[12]),
            starttime=int(rest[19]),
        )
    except (IndexError, ValueError):
        return None
