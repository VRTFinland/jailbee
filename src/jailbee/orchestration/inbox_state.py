"""Publish child lifecycle observations for non-consuming inbox wakeups."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import lifecycle, procstat
from jailbee.orchestration import agent_watch, child_status, labels
from jailbee.orchestration.outbox import _BASE, _atomic_write
from jailbee.orchestration.state import atomic_write_text

if TYPE_CHECKING:
    from jailbee.incus import Incus
    from jailbee.orchestration.actions import ActionContext, Node

log = logging.getLogger(__name__)

STATUS_EXEC_TIMEOUT_S = 10
"""Bound on every `incus exec` a status probe makes; a hung child must not stall a round."""

_STATUS_KEYS = (
    "observed_at",
    "agent",
    "cpu_percent",
    "memory_bytes",
    "longest_command",
    "head",
    "dirty",
    "last_busy_at",
)


def _uptime() -> float:
    return float(Path("/proc/uptime").read_text().split()[0])


def _load(path: Path) -> dict[str, object]:
    try:
        saved = json.loads(path.read_text())
        if isinstance(saved, dict) and isinstance(saved.get("children"), list):
            return saved
    except (OSError, ValueError, TypeError):
        pass
    return {"owner": None, "children": []}


def note_delivery(root: Path, orchestrator_full: str, child_full: str, at: float) -> None:
    """Record that the orchestrator just sent `child_full` something the broker accepted."""
    path = root / "inbox-state" / f"{orchestrator_full}.json"
    try:
        saved = _load(path)
        children = saved["children"]
        assert isinstance(children, list)
        for entry in children:
            if isinstance(entry, dict) and entry.get("full") == child_full:
                entry["last_to_child_at"] = at
                break
        else:
            children.append({"full": child_full, "last_to_child_at": at})
        atomic_write_text(path, json.dumps(saved))
    except OSError:
        log.debug("could not record delivery to %s", child_full, exc_info=True)


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _memory_bytes(node: Node) -> int | None:
    usage = node.memory_usage
    if usage is None or node.init_pid is None:
        return usage
    cache = procstat.read_container_cache(node.init_pid, node.full)
    return usage if cache is None else max(0, usage - cache)


def _cmdline(pid: int) -> str | None:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _agent_host_pid(
    samples: dict[int, procstat.ProcSample], agent: agent_watch.AgentStatus | None
) -> int | None:
    """The host pid of the monitored agent, matched on namespace pid and start tick."""
    if agent is None or agent.pid is None or agent.proc_start is None:
        return None
    for pid, sample in samples.items():
        if sample.starttime == agent.proc_start and procstat.read_nspid(pid) == agent.pid:
            return pid
    return None


def _longest_command(
    node: Node, agent: agent_watch.AgentStatus | None
) -> dict[str, object] | None:
    if node.init_pid is None:
        raise ValueError("no init pid")
    samples = {}
    for pid in procstat.read_container_pids(node.init_pid, node.full):
        sample = procstat.read_process(pid)
        if sample is not None:
            samples[pid] = sample
    return child_status.longest_command(
        samples,
        agent_pid=_agent_host_pid(samples, agent),
        uptime=_uptime(),
        clk_tck=os.sysconf("SC_CLK_TCK"),
        cmdline=_cmdline,
    )


def _refresh_agent(
    ctx: ActionContext, cache: child_status.StatusCache, node: Node, work: dict[str, object]
) -> None:
    agent = cache.agent.get(node.full)
    if agent is None:
        return
    try:
        snap = agent_watch.agent_snapshot(ctx.cfg, node.full, agent, now=ctx.now)
    except Exception:
        log.debug("agent snapshot of %s failed", node.full, exc_info=True)
        return
    # A session that could not be read this time is not a child that stopped
    # writing: keep the last event time we knew.
    earlier = work.get("agent")
    if snap.get("last_event_at") is None and isinstance(earlier, dict):
        known = _number(earlier.get("last_event_at"))
        if known is not None:
            snap = {**snap, "last_event_at": known}
    work["agent"] = snap


def _collect_exec(
    ctx: ActionContext, cache: child_status.StatusCache, node: Node, work: dict[str, object]
) -> None:
    uid = ctx.cfg.container_user.uid
    bounded: Incus | None = None
    try:
        bounded = ctx.incus.with_exec_timeout(STATUS_EXEC_TIMEOUT_S)
        agent = agent_watch.probe(bounded, node.full, uid=uid)
        if agent is not None:
            cache.agent[node.full] = agent
    except Exception:
        log.debug("agent probe of %s failed", node.full, exc_info=True)
    try:
        if bounded is None:
            return
        repo = node.config.get("user.jailbee.repo_dir") or lifecycle.container_repo_dir(
            ctx.cfg, bounded, node.full
        )
        head, dirty = child_status.probe_head(bounded, node.full, repo, uid=uid)
        # (None, None) is a failed probe; a repo with no commits is (None, bool).
        if head is not None or dirty is not None:
            work["head"] = head
            work["dirty"] = dirty
    except Exception:
        log.debug("head probe of %s failed", node.full, exc_info=True)


def _collect_host(
    ctx: ActionContext,
    cache: child_status.StatusCache,
    nodes: list[Node],
    working: dict[str, dict[str, object]],
) -> None:
    now = ctx.now.timestamp()
    rates: dict[str, procstat.ContainerActivity] | None = None
    try:
        # Every running child is sampled together: the sampler keeps one
        # reading set, and leaving a child out would drop its previous reading.
        rates = cache.sampler_for(ctx.sender.full).sample(
            [procstat.SampleInput(n.full, n.init_pid, n.cpu_usage_ns) for n in nodes]
        )
    except Exception:
        log.debug("cpu sample failed", exc_info=True)
    for node in nodes:
        work = working[node.full]
        work["observed_at"] = now
        if rates is not None:
            activity = rates.get(node.full)
            cpu = activity.cpu_percent if activity is not None else None
            work["cpu_percent"] = cpu
            # Unknown CPU never counts as busy, and never erases earlier busyness.
            if cpu is not None and cpu >= child_status.BUSY_CPU_PERCENT:
                work["last_busy_at"] = now
        try:
            work["memory_bytes"] = _memory_bytes(node)
        except Exception:
            log.debug("memory of %s failed", node.full, exc_info=True)
        try:
            work["longest_command"] = _longest_command(node, cache.agent.get(node.full))
        except Exception:
            log.debug("process scan of %s failed", node.full, exc_info=True)
        _refresh_agent(ctx, cache, node, work)


FUTURE_SLACK_S = 5
"""How far ahead of the broker's clock a child-supplied time may be and still count."""


def _activity(started: float, work: dict[str, object], now: float, previous: float | None) -> float:
    """`activity_at`, from the child's signals and never lower than `previous`.

    A child-supplied time more than `FUTURE_SLACK_S` ahead of `now` (a commit
    with a skewed or forged date, a forged session event) is unknown, not
    clamped: clamping would make the child look active every round. Unknown
    signals never lower an earlier reading either.
    """
    agent = work.get("agent")
    head = work.get("head")

    def seen(value: object) -> float | None:
        number = _number(value)
        return None if number is None or number > now + FUTURE_SLACK_S else number

    computed = child_status.activity_at(
        started_at=started,
        agent_last_event_at=seen(agent.get("last_event_at")) if isinstance(agent, dict) else None,
        head_time=seen(head.get("time")) if isinstance(head, dict) else None,
        last_busy_at=seen(work.get("last_busy_at")),
    )
    return computed if previous is None else max(previous, computed)


def sync_children(ctx: ActionContext, cache: child_status.StatusCache | None = None) -> None:
    """Refresh the heartbeat and retain fallback boot times across broker restarts.

    With a `cache`, also collect each child's status (#64) at the cache's
    cadence. Without one nothing is collected and `status` stays absent from
    the published items; durable status is still carried over untouched.
    """
    path = ctx.root / "inbox-state" / f"{ctx.sender.full}.json"
    previous: dict[str, dict[str, object]] = {}
    try:
        saved = json.loads(path.read_text())
        if saved["owner"] in (ctx.sender.key, None):
            previous = {c["full"]: c for c in saved["children"]}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    now = ctx.now.timestamp()
    kids = sorted(ctx.children(), key=lambda c: c.short)
    olds: dict[str, dict[str, object]] = {}
    working: dict[str, dict[str, object]] = {}
    running = [k for k in kids if k.running]
    for child in kids:
        old = previous.get(child.full, {})
        # An entry of another instance of this name carries nothing over.
        if old.get("key") not in (None, child.key):
            old = {}
        olds[child.full] = old
        prior = old.get("status")
        work: dict[str, object] = dict.fromkeys(_STATUS_KEYS)
        if isinstance(prior, dict):
            work.update({k: v for k, v in prior.items() if k in _STATUS_KEYS})
        working[child.full] = work
    if cache is not None:
        host_due = [cache.host_due(k.full, now) for k in running]
        exec_due = [cache.exec_due(k.full, now) for k in running]
        try:
            bounded_nodes = [k for k, due in zip(running, exec_due, strict=True) if due]
            for node in bounded_nodes:
                _collect_exec(ctx, cache, node, working[node.full])
            if any(host_due):
                _collect_host(ctx, cache, running, working)
        except Exception:
            log.debug("status collection failed", exc_info=True)
    children = []
    durable = []
    for child in kids:
        old = olds[child.full]
        started = child.started_at
        if started is None:
            started = now
            if old.get("key") == child.key and (not child.running or old.get("running")):
                value = old.get("started_at")
                if isinstance(value, (int, float)):
                    started = value
        # `running` is the container state (#63). A finished child may keep
        # running until import/destroy; `done` says so, and the inbox's quiet
        # watchdog skips done children.
        sessions = labels.sessions_of(child.config)
        item: dict[str, object] = {
            "name": child.short,
            "running": child.running,
            "done": labels.is_finished(child.config),
            "started_at": started,
            "session": sessions[-1] if sessions else None,
        }
        entry: dict[str, object] = {**item, "full": child.full, "key": child.key}
        delivered_at = _number(old.get("last_to_child_at"))
        if delivered_at is not None:
            entry["last_to_child_at"] = delivered_at
        prior = old.get("status")
        if cache is None:
            if isinstance(prior, dict):
                entry["status"] = prior
        else:
            work = working[child.full]
            if child.running:
                work["last_to_child_at"] = delivered_at
                last = old.get("status")
                work["activity_at"] = _activity(
                    started,
                    work,
                    now,
                    _number(last.get("activity_at")) if isinstance(last, dict) else None,
                )
                item["status"] = entry["status"] = work
            else:
                # Stopped: only what outlives the container. The durable record
                # keeps the whole last status for when it runs again.
                head = work.get("head")
                if isinstance(prior, dict) or delivered_at is not None:
                    item["status"] = {"head": head, "last_to_child_at": delivered_at}
                else:
                    item["status"] = None
                if isinstance(prior, dict):
                    entry["status"] = prior
        children.append(item)
        durable.append(entry)
    atomic_write_text(path, json.dumps({"owner": ctx.sender.key, "children": durable}))
    # Stamped now, not with `ctx.now`: that is the round's start, and a round
    # can run long (child imports, slow `incus exec`), which would make the
    # heartbeat look stale the moment it lands.
    text = json.dumps({"version": 1, "updated_at": time.time(), "children": children})
    _atomic_write(
        ctx.incus,
        ctx.sender.full,
        _BASE,
        "children.json",
        text + "\n",
        "0444",
        uid=ctx.cfg.container_user.uid,
    )
