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


def _unified_cgroup_path(raw: str) -> str:
    """The cgroup path from a /proc/<pid>/cgroup file.

    Prefers the v2 unified line (``0::<path>``) and falls back to the first
    v1 line, so a hybrid host still yields a usable path.
    """
    fallback = ""
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        if parts[0] == "0" and parts[1] == "":
            return parts[2]
        if not fallback:
            fallback = parts[2]
    return fallback


def _container_cgroup(raw: str, container: str) -> str | None:
    """Cut init's cgroup path back to the container's own cgroup.

    systemd inside the container puts pid 1 in ``init.scope``, so init's
    path names a child of what we want. The cut is made after the first
    component carrying the container's name — which assumes only that Incus
    names the cgroup after the instance, not where it puts it — and falls
    back to the topmost component for a layout that does not.
    """
    parts = [p for p in _unified_cgroup_path(raw).split("/") if p]
    if not parts:
        return None
    for i, part in enumerate(parts):
        if container in part:
            return "/".join(parts[: i + 1])
    return parts[0]


def read_container_pids(
    init_pid: int,
    container: str,
    *,
    proc_root: Path = PROC_ROOT,
    cgroup_root: Path = CGROUP_ROOT,
) -> list[int]:
    """Every pid inside the container whose init process is ``init_pid``.

    Under cgroup v2 a ``cgroup.procs`` file lists only the processes sitting
    directly in that cgroup, and a systemd container spreads its work across
    ``system.slice/<unit>.service``, ``user.slice/…`` and more — so the
    whole subtree is walked and the pids unioned.

    Returns an empty list for anything unreadable: the caller renders a
    dash, and a gather must never fail over this.
    """
    try:
        raw = (proc_root / str(init_pid) / "cgroup").read_text()
    except OSError:
        return []
    rel = _container_cgroup(raw, container)
    if rel is None:
        return []
    base = cgroup_root / rel
    pids: list[int] = []
    try:
        procs_files = sorted(base.rglob("cgroup.procs"))
    except OSError:
        return []
    for procs in procs_files:
        try:
            text = procs.read_text()
        except OSError:
            continue  # a cgroup can be torn down mid-walk
        for token in text.split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
    return pids
