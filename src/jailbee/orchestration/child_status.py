"""Pure status model for child containers.

Everything here is a function of values the caller already holds: the
process samples from `procstat`, the clock, and timestamps the broker stored.
Nothing reads /proc, runs a process, or touches the network, so the broker's
gather path can call it freely and the tests need no fakes.

Text that originates inside a child (a commit subject, a process name) is
hostile by default; `clean_text` is applied where it is recorded, and again
wherever it is printed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jailbee import procstat
from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.incus import Incus
    from jailbee.orchestration.agent_watch import AgentStatus

STATUS_INTERVAL = 30
"""Seconds between host-side status probes of one child."""

GIT_INTERVAL = 60
"""Seconds between git-side status probes of one child."""

BUSY_CPU_PERCENT = 5.0
"""CPU share of one core above which a child counts as busy."""

SUBJECT_LIMIT = 120
"""Longest commit subject kept, in characters."""

COMMAND_LIMIT = 160
"""Longest process name kept, in characters."""

# C0 controls (incl. ESC and TAB), DEL, and the C1 range.
_SHA = re.compile(r"[0-9a-f]{40,64}")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")


def clean_text(value: str, limit: int) -> str:
    """Replace control characters with `?`, then cut to `limit` with `…`.

    A cut string ends in `…` and is exactly `limit` characters long.
    """
    text = _CONTROL.sub("?", value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


WAITER_COMMS = frozenset({"jailbee", "jailbee-inbox"})
"""Process names of the orchestration waiters, which are not work the child does."""


_WAITER_MODULES = frozenset({"jailbee", "jailbee.container_main", "jailbee.inbox"})


def _is_waiter(comm: str, cmdline: str | None) -> bool:
    """True for `inbox wait` and `inbox send --wait`, however they were started.

    In a container `jailbee` is a wrapper that execs
    `python -P -m jailbee.container_main ...`, so the real process is named
    `python` and only its command line says what it is. Matches an entry point
    (`jailbee`, `jailbee-inbox` or `-m <jailbee module>`) anywhere in the
    words, followed by an optional `inbox` and then `wait`, or `send` with
    `--wait`. A tool such as `python -m pytest` never matches.
    """
    if comm in WAITER_COMMS:
        return True
    if cmdline is None:
        return False
    words = cmdline.split()
    for index, word in enumerate(words):
        if word.rsplit("/", 1)[-1] in WAITER_COMMS:
            rest = words[index + 1 :]
        elif word == "-m" and index + 1 < len(words) and words[index + 1] in _WAITER_MODULES:
            rest = words[index + 2 :]
        else:
            continue
        if rest[:1] == ["inbox"]:
            rest = rest[1:]
        if rest[:1] == ["wait"] or (rest[:1] == ["send"] and "--wait" in rest):
            return True
    return False


def longest_command(
    samples: Mapping[int, procstat.ProcSample],
    *,
    agent_pid: int | None,
    uptime: float,
    clk_tck: int,
    cmdline: Callable[[int], str | None] | None = None,
) -> dict[str, object] | None:
    """The oldest non-shell process a shell started below the agent in `samples`.

    `agent_pid` is the agent's pid as `samples` keys it; the agent itself and
    anything outside its process tree is never reported, nor are the
    `jailbee inbox wait` waiters (found by name, or by `cmdline(pid)`) and what
    they started. With no known agent the answer is None: unknown, never the
    agent. `uptime` and `clk_tck` turn the start tick into seconds since boot;
    the result is how long that process has been running.
    """
    if agent_pid is None or agent_pid not in samples:
        return None
    children: dict[int, list[int]] = {}
    for pid, sample in samples.items():
        children.setdefault(sample.ppid, []).append(pid)
    below: list[int] = []
    pending = list(children.get(agent_pid, ()))
    seen = {agent_pid}
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if _is_waiter(samples[pid].comm, cmdline(pid) if cmdline is not None else None):
            continue
        below.append(pid)
        pending.extend(children.get(pid, ()))
    candidates = [
        samples[pid]
        for pid in below
        if samples[pid].comm not in procstat.SHELL_COMMS
        and (parent := samples.get(samples[pid].ppid)) is not None
        and parent.comm in procstat.SHELL_COMMS
    ]
    if not candidates:
        return None
    oldest = min(candidates, key=lambda sample: sample.starttime)
    return {
        "command": clean_text(oldest.comm, COMMAND_LIMIT),
        "seconds": int(uptime - oldest.starttime / clk_tck),
    }


def activity_at(
    *,
    started_at: float,
    agent_last_event_at: float | None,
    head_time: float | None,
) -> float:
    """The latest moment the child showed progress.

    Progress is an agent event or a commit. CPU use is not: a hung build or
    test JVM keeps burning a little CPU and would hide a stalled child (#76).
    Unknown (None) signals are ignored; `started_at` is always known.
    """
    times = [started_at]
    for value in (agent_last_event_at, head_time):
        if value is not None:
            times.append(value)
    return max(times)


@dataclass
class StatusCache:
    """Per-child timestamps that rate-limit the two kinds of status probe.

    `host_at` gates the cheap host-side probe (every `STATUS_INTERVAL`);
    `exec_at` gates the probe that runs git inside the container (every
    `GIT_INTERVAL`). `samplers` hold the CPU readings between gathers, one per orchestrator: a
    sampler keeps only the containers of its latest call, so two orchestrators
    sharing one would erase each other's previous readings.
    """

    host_at: dict[str, float] = field(default_factory=dict)
    exec_at: dict[str, float] = field(default_factory=dict)
    samplers: dict[str, procstat.ActivitySampler] = field(default_factory=dict)
    # The last agent probe result per child; the snapshot reads session files with it.
    agent: dict[str, AgentStatus] = field(default_factory=dict)

    def sampler_for(self, orchestrator: str) -> procstat.ActivitySampler:
        """The CPU sampler of `orchestrator`'s children, created on first use."""
        sampler = self.samplers.get(orchestrator)
        if sampler is None:
            sampler = self.samplers[orchestrator] = procstat.ActivitySampler()
        return sampler

    def prune(self, children: Collection[str], orchestrators: Collection[str]) -> None:
        """Forget what is kept for containers that no longer exist."""
        for per_child in (self.host_at, self.exec_at, self.agent):
            for full in [f for f in per_child if f not in children]:
                del per_child[full]
        for full in [f for f in self.samplers if f not in orchestrators]:
            del self.samplers[full]

    def host_due(self, full: str, now: float) -> bool:
        """True, and record `now`, when the host probe for `full` is due."""
        return _due(self.host_at, full, now, STATUS_INTERVAL)

    def exec_due(self, full: str, now: float) -> bool:
        """True, and record `now`, when the in-container probe for `full` is due."""
        return _due(self.exec_at, full, now, GIT_INTERVAL)


def _due(stamps: dict[str, float], full: str, now: float, interval: float) -> bool:
    last = stamps.get(full)
    if last is not None and now - last < interval:
        return False
    stamps[full] = now
    return True


# $1 is the repo directory, passed as an argument so it is never parsed as shell.
_HEAD_SCRIPT = r"""
export GIT_OPTIONAL_LOCKS=0
g="git --no-optional-locks -C"
log=$($g "$1" log -1 --format=%H%x1f%ct%x1f%s 2>/dev/null) && printf '%s\n' "$log"
if ! porcelain=$($g "$1" status --porcelain --ignore-submodules=all 2>/dev/null); then
    echo ERROR
elif [ -n "$porcelain" ]; then
    echo DIRTY
else
    echo CLEAN
fi
exit 0
"""


def probe_head(
    incus: Incus, full: str, repo_dir: str, *, uid: int
) -> tuple[dict[str, object] | None, bool | None]:
    """The child's HEAD commit and whether its worktree is dirty.

    `(None, dirty)` for a repository without commits; `(None, None)` when the
    probe fails or prints something unexpected.
    """
    try:
        raw = incus.exec(full, ["sh", "-c", _HEAD_SCRIPT, "sh", repo_dir], uid=uid)
    except IncusError:
        return None, None
    lines = [line for line in raw.splitlines() if line]
    if not lines or lines[-1] not in ("DIRTY", "CLEAN"):
        return None, None
    dirty = lines[-1] == "DIRTY"
    if len(lines) == 1:
        return None, dirty
    if len(lines) != 2:
        return None, None
    parts = lines[0].split("\x1f", 2)
    try:
        if len(parts) != 3 or not _SHA.fullmatch(parts[0]):
            return None, None
        when = float(int(parts[1]))
    except ValueError:
        return None, None
    return {"sha": parts[0], "time": when, "subject": clean_text(parts[2], SUBJECT_LIMIT)}, dirty
