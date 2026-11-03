import json
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from jailbee.incus import IncusError
from jailbee.orchestration import inbox_state, labels
from jailbee.orchestration.actions import ActionContext, Node


def context(tmp_path):
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    parent = Node("p-orch", "orch", True, {})
    child = Node("p-kid", "kid", True, {labels.SPAWNED_BY: parent.full})
    return ActionContext(
        cfg=MagicMock(),
        gcfg=MagicMock(),
        incus=MagicMock(),
        root=tmp_path,
        sender=parent,
        nodes={parent.full: parent, child.full: child},
        now=now,
    )


def delivered(ctx):
    return json.loads(ctx.incus.exec_with_input.call_args.args[2])


def test_snapshot_persists_start_across_broker_restart(tmp_path):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx)
    first = delivered(ctx)
    assert first["children"] == [
        {
            "name": "kid",
            "running": True,
            "done": False,
            "started_at": ctx.now.timestamp(),
            "session": None,
        }
    ]
    ctx.now += timedelta(minutes=10)
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"] == first["children"]
    assert delivered(ctx)["children"][0]["started_at"] == first["children"][0]["started_at"]


def test_heartbeat_stamped_at_write_not_round_start(tmp_path, monkeypatch):
    # A broker round can run long (imports, slow `incus exec`); `ctx.now` is
    # the round's start, so stamping it made the heartbeat stale on arrival.
    ctx = context(tmp_path)
    written = ctx.now.timestamp() + 58
    monkeypatch.setattr(inbox_state.time, "time", lambda: written)
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["updated_at"] == written


def test_snapshot_stop_restart_and_removal(tmp_path):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx)
    start = ctx.now.timestamp()
    ctx.now += timedelta(minutes=1)
    ctx.nodes = {"p-kid": Node("p-kid", "kid", False, {labels.SPAWNED_BY: "p-orch"})}
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["running"] is False
    assert delivered(ctx)["children"][0]["started_at"] == start
    ctx.now += timedelta(minutes=1)
    ctx.nodes = context(tmp_path).nodes
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == ctx.now.timestamp()
    ctx.nodes = {}
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"] == []


def test_snapshot_uses_incus_boot_timestamp_and_new_instance(tmp_path):
    from jailbee.orchestration.broker import _nodes
    from tests.conftest import make_config

    ctx = context(tmp_path)
    cfg = make_config(tmp_path).model_copy(update={"container_prefix": "p"})
    ctx.nodes = _nodes(
        [
            {
                "name": "p-kid",
                "status": "Running",
                "last_used_at": "2026-10-30T11:00:00Z",
                "config": {labels.SPAWNED_BY: "p-orch", "volatile.uuid": "one"},
            }
        ],
        {"p": cfg},
    )
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == ctx.now.timestamp() - 3600
    ctx.nodes = {
        "p-kid": Node("p-kid", "kid", True, {labels.SPAWNED_BY: "p-orch", "volatile.uuid": "two"})
    }
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == ctx.now.timestamp()


def test_snapshot_failed_write_can_retry(tmp_path):
    ctx = context(tmp_path)
    ctx.incus.exec_with_input.side_effect = IncusError("stopped")
    with pytest.raises(IncusError):
        inbox_state.sync_children(ctx)
    ctx.now += timedelta(seconds=5)
    ctx.incus.exec_with_input.side_effect = None
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == ctx.now.timestamp() - 5


def _kid_with(ctx, config):
    ctx.nodes = {
        "p-orch": ctx.sender,
        "p-kid": Node("p-kid", "kid", True, {labels.SPAWNED_BY: "p-orch", **config}),
    }


@pytest.mark.parametrize(
    ("config", "done"),
    [
        ({labels.STATE: labels.STATE_DONE}, True),
        (
            {
                labels.STATE: labels.STATE_IMPORTED,
                labels.DONE_SHA: "a" * 40,
                labels.LAST_IMPORT: "a" * 40,
            },
            True,
        ),
        (
            {
                labels.STATE: labels.STATE_IMPORTED,
                labels.DONE_SHA: "a" * 40,
                labels.LAST_IMPORT: "b" * 40,
            },
            False,
        ),
        ({labels.STATE: "working"}, False),
    ],
)
def test_snapshot_running_is_container_state_and_done_is_separate(tmp_path, config, done):
    # #63: a finished child whose container is up must read running=true;
    # `done` carries the finished state the quiet watchdog needs.
    ctx = context(tmp_path)
    _kid_with(ctx, config)
    inbox_state.sync_children(ctx)
    child = delivered(ctx)["children"][0]
    assert child["running"] is True
    assert child["done"] is done


def test_snapshot_stopped_finished_child(tmp_path):
    ctx = context(tmp_path)
    ctx.nodes = {
        "p-orch": ctx.sender,
        "p-kid": Node(
            "p-kid", "kid", False, {labels.SPAWNED_BY: "p-orch", labels.STATE: labels.STATE_DONE}
        ),
    }
    inbox_state.sync_children(ctx)
    child = delivered(ctx)["children"][0]
    assert (child["running"], child["done"]) == (False, True)


def test_snapshot_finished_child_keeps_start_time(tmp_path):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx)
    start = ctx.now.timestamp()
    ctx.now += timedelta(minutes=5)
    _kid_with(ctx, {labels.STATE: labels.STATE_DONE})
    inbox_state.sync_children(ctx)
    ctx.now += timedelta(minutes=5)
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == start


def test_snapshot_publishes_latest_session(tmp_path):
    ctx = context(tmp_path)
    ids = ["01234567-89ab-cdef-0123-456789abcdef", "12345678-89ab-cdef-0123-456789abcdef"]
    _kid_with(ctx, {labels.AGENT_SESSIONS: json.dumps(ids)})
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["session"] == ids[-1]
    saved = json.loads((tmp_path / "inbox-state" / "p-orch.json").read_text())
    assert saved["children"][0]["session"] == ids[-1]
    _kid_with(ctx, {})
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["session"] is None
    assert (
        json.loads((tmp_path / "inbox-state" / "p-orch.json").read_text())["children"][0]["session"]
        is None
    )


# --- child status (#64) ---------------------------------------------------

from jailbee.orchestration import child_status  # noqa: E402
from jailbee.orchestration.agent_watch import AgentStatus  # noqa: E402
from jailbee.procstat import ContainerActivity  # noqa: E402


class FakeSampler:
    def __init__(self, cpu):
        self.cpu = cpu
        self.calls = []

    def sample(self, items):
        self.calls.append(list(items))
        return {i.name: ContainerActivity(cpu_percent=self.cpu) for i in items}


_REAL_PROBE_HEAD = child_status.probe_head
HEAD = {"sha": "a" * 40, "time": 1_000.0, "subject": "feat: x"}
AGENT = {
    "alive": True,
    "state": "busy",
    "since": 1.0,
    "last_event_at": 5_000.0,
    "last_tool": "Bash",
}


@pytest.fixture
def probes(monkeypatch):
    class P:
        head = (HEAD, False)
        head_calls = 0
        agent_calls = 0
        agent_status = AgentStatus(True, "claude", pid=1, proc_start=2)
        head_error = None
        incus_used: ClassVar[list] = []

    p = P()

    def probe_head(incus, full, repo, *, uid):
        p.head_calls += 1
        p.incus_used.append(incus)
        if p.head_error:
            raise p.head_error
        return p.head

    def probe(incus, name, *, uid):
        p.agent_calls += 1
        p.incus_used.append(incus)
        return p.agent_status

    monkeypatch.setattr(inbox_state.child_status, "probe_head", probe_head)
    monkeypatch.setattr(inbox_state.agent_watch, "probe", probe)
    monkeypatch.setattr(inbox_state.agent_watch, "agent_snapshot", lambda *a, **k: dict(AGENT))
    monkeypatch.setattr(inbox_state.lifecycle, "container_repo_dir", lambda *a: "/repo")
    monkeypatch.setattr(inbox_state.procstat, "read_container_pids", lambda *a, **k: [])
    monkeypatch.setattr(inbox_state.procstat, "read_container_cache", lambda *a, **k: None)
    monkeypatch.setattr(inbox_state, "_uptime", lambda: 1000.0)
    return p


def published_status(ctx):
    return delivered(ctx)["children"][0].get("status")


def _cache(cpu=None):
    cache = child_status.StatusCache()
    cache.samplers["p-orch"] = FakeSampler(cpu)
    return cache


def test_sync_children_publishes_status_for_running_child(tmp_path, probes):
    ctx = context(tmp_path)
    ctx.nodes["p-kid"] = Node(
        "p-kid", "kid", True, {labels.SPAWNED_BY: "p-orch"}, None, 77, 10, 5_000
    )
    cache = _cache(cpu=50.0)
    inbox_state.sync_children(ctx, cache)
    st = published_status(ctx)
    assert set(st) == {
        "observed_at",
        "agent",
        "cpu_percent",
        "memory_bytes",
        "longest_command",
        "head",
        "dirty",
        "last_busy_at",
        "last_to_child_at",
        "activity_at",
    }
    now = ctx.now.timestamp()
    assert st["cpu_percent"] == 50.0
    assert st["last_busy_at"] == now
    assert st["agent"] == AGENT
    assert st["head"] == HEAD and st["dirty"] is False
    assert st["memory_bytes"] == 5_000
    assert st["activity_at"] == max(now, 5_000.0, 1_000.0)
    assert st["last_to_child_at"] is None
    assert cache.samplers["p-orch"].calls[0][0].init_pid == 77


def test_sync_children_memory_subtracts_cache_and_clamps(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    ctx.nodes["p-kid"] = Node(
        "p-kid", "kid", True, {labels.SPAWNED_BY: "p-orch"}, None, 77, 10, 5_000
    )
    monkeypatch.setattr(inbox_state.procstat, "read_container_cache", lambda *a, **k: 2_000)
    inbox_state.sync_children(ctx, _cache())
    assert published_status(ctx)["memory_bytes"] == 3_000
    ctx.now += timedelta(seconds=100)
    monkeypatch.setattr(inbox_state.procstat, "read_container_cache", lambda *a, **k: 9_000)
    inbox_state.sync_children(ctx, _cache())
    assert published_status(ctx)["memory_bytes"] == 0
    ctx.now += timedelta(seconds=100)
    monkeypatch.setattr(inbox_state.procstat, "read_container_cache", lambda *a, **k: None)
    inbox_state.sync_children(ctx, _cache())
    assert published_status(ctx)["memory_bytes"] == 5_000


def test_sync_children_memory_none_without_usage(tmp_path, probes):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx, _cache())
    assert published_status(ctx)["memory_bytes"] is None


def test_sync_children_rate_limits_exec_probes(tmp_path, probes):
    ctx = context(tmp_path)
    cache = _cache()
    inbox_state.sync_children(ctx, cache)
    ctx.now += timedelta(seconds=10)
    inbox_state.sync_children(ctx, cache)
    assert probes.head_calls == 1
    assert probes.agent_calls == 1
    assert len(cache.samplers["p-orch"].calls) == 1
    ctx.now += timedelta(seconds=60)
    inbox_state.sync_children(ctx, cache)
    assert probes.head_calls == 2


def test_sync_children_exec_goes_through_bounded_incus(tmp_path, probes):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx, _cache())
    ctx.incus.with_exec_timeout.assert_called_with(inbox_state.STATUS_EXEC_TIMEOUT_S)
    assert probes.incus_used
    assert all(i is ctx.incus.with_exec_timeout.return_value for i in probes.incus_used)


def test_sync_children_status_survives_broker_restart(tmp_path, probes):
    ctx = context(tmp_path)
    ctx.nodes["p-kid"] = Node(
        "p-kid", "kid", True, {labels.SPAWNED_BY: "p-orch"}, ctx.now.timestamp() - 3600
    )
    inbox_state.sync_children(ctx, _cache(cpu=80.0))
    busy_at = published_status(ctx)["last_busy_at"]
    assert busy_at == ctx.now.timestamp()
    probes.agent_status = None  # nothing known after the restart yet
    probes.head = (None, None)
    ctx.now += timedelta(seconds=300)
    inbox_state.sync_children(ctx, _cache(cpu=None))  # fresh cache, cold sampler
    st = published_status(ctx)
    assert st["last_busy_at"] == busy_at
    # started_at (an hour earlier), agent and head are all older than busy_at.
    assert st["activity_at"] == busy_at
    assert st["head"] == HEAD
    assert st["cpu_percent"] is None


def test_sync_children_probe_failure_keeps_previous_head(tmp_path, probes):
    ctx = context(tmp_path)
    cache = _cache()
    inbox_state.sync_children(ctx, cache)
    probes.head_error = RuntimeError("boom")
    ctx.now += timedelta(seconds=120)
    inbox_state.sync_children(ctx, cache)
    assert published_status(ctx)["head"] == HEAD
    assert probes.head_calls == 2


def test_sync_children_exec_timeout_keeps_previous_and_does_not_raise(tmp_path, probes):
    ctx = context(tmp_path)
    cache = _cache()
    inbox_state.sync_children(ctx, cache)
    probes.head = (None, None)  # probe_head's answer to IncusError (timeout)
    probes.agent_status = None  # agent_watch.probe's answer to IncusError
    ctx.now += timedelta(seconds=120)
    inbox_state.sync_children(ctx, cache)
    st = published_status(ctx)
    assert st["head"] == HEAD and st["dirty"] is False
    assert st["agent"] == AGENT


def test_sync_children_real_probe_head_timeout_keeps_previous(tmp_path, monkeypatch, probes):
    ctx = context(tmp_path)
    cache = _cache()
    inbox_state.sync_children(ctx, cache)
    monkeypatch.undo()  # real probe_head, with a hanging exec
    monkeypatch.setattr(inbox_state.lifecycle, "container_repo_dir", lambda *a: "/repo")
    monkeypatch.setattr(inbox_state.agent_watch, "agent_snapshot", lambda *a, **k: dict(AGENT))
    monkeypatch.setattr(inbox_state, "_uptime", lambda: 1000.0)
    monkeypatch.setattr(inbox_state.agent_watch, "probe", lambda *a, **k: None)
    ctx.incus.with_exec_timeout.return_value.exec.side_effect = IncusError("timed out")
    ctx.now += timedelta(seconds=120)
    inbox_state.sync_children(ctx, cache)
    assert published_status(ctx)["head"] == HEAD


def test_sync_children_stopped_child_keeps_head_only(tmp_path, probes):
    ctx = context(tmp_path)
    cache = _cache(cpu=10.0)
    inbox_state.sync_children(ctx, cache)
    ctx.nodes["p-kid"] = Node("p-kid", "kid", False, {labels.SPAWNED_BY: "p-orch"})
    ctx.now += timedelta(seconds=300)
    inbox_state.sync_children(ctx, cache)
    assert published_status(ctx) == {"head": HEAD, "last_to_child_at": None}


def test_sync_children_never_observed_stopped_child_has_null_status(tmp_path, probes):
    ctx = context(tmp_path)
    ctx.nodes["p-kid"] = Node("p-kid", "kid", False, {labels.SPAWNED_BY: "p-orch"})
    inbox_state.sync_children(ctx, _cache())
    assert delivered(ctx)["children"][0]["status"] is None


def test_sync_children_without_cache_has_no_status(tmp_path, probes):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx)
    assert "status" not in delivered(ctx)["children"][0]
    assert probes.head_calls == 0


def test_sync_children_without_cache_preserves_durable_status(tmp_path, probes):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx, _cache(cpu=30.0))
    inbox_state.sync_children(ctx)
    saved = json.loads((tmp_path / "inbox-state" / "p-orch.json").read_text())
    assert saved["children"][0]["status"]["head"] == HEAD


def test_sync_children_hostile_subject_is_cleaned_by_real_probe(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    monkeypatch.setattr(inbox_state.child_status, "probe_head", _REAL_PROBE_HEAD)
    hostile = "\x1b[2J\x07" + "x" * 10_000
    ctx.incus.with_exec_timeout.return_value.exec.return_value = (
        f"{'a' * 40}\x1f100\x1f{hostile}\nCLEAN\n"
    )
    inbox_state.sync_children(ctx, _cache())
    subject = published_status(ctx)["head"]["subject"]
    assert subject.startswith("?[2J?x")
    assert len(subject) == child_status.SUBJECT_LIMIT and subject.endswith("…")
    assert not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in subject)


def test_sync_children_rejects_hostile_sha_through_real_probe(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    monkeypatch.setattr(inbox_state.child_status, "probe_head", _REAL_PROBE_HEAD)
    ctx.incus.with_exec_timeout.return_value.exec.return_value = (
        "\x1b[2J" + "a" * 1_000_000 + "\x1f100\x1fs\nCLEAN\n"
    )
    inbox_state.sync_children(ctx, _cache())
    assert published_status(ctx)["head"] is None
    assert len(ctx.incus.exec_with_input.call_args.args[2]) < 10_000


@pytest.mark.parametrize(("cpu", "busy"), [(4.99, False), (5.0, True), (5.01, True)])
def test_cpu_threshold_boundary(tmp_path, probes, cpu, busy):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx, _cache(cpu=cpu))
    expected = ctx.now.timestamp() if busy else None
    assert published_status(ctx)["last_busy_at"] == expected


def test_activity_at_ignores_far_future_child_supplied_times(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    now = ctx.now.timestamp()
    probes.head = ({**HEAD, "time": now + 10_000}, False)
    monkeypatch.setattr(
        inbox_state.agent_watch,
        "agent_snapshot",
        lambda *a, **k: {**AGENT, "last_event_at": now + 20_000},
    )
    cache = _cache()
    inbox_state.sync_children(ctx, cache)
    assert published_status(ctx)["activity_at"] < now + 1
    # Not clamped to now: it stays at the unknown-signals floor round after round.
    ctx.now += timedelta(seconds=120)
    inbox_state.sync_children(ctx, cache)
    assert published_status(ctx)["activity_at"] < now + 1


def test_activity_at_counts_child_time_within_slack(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    now = ctx.now.timestamp()
    monkeypatch.setattr(
        inbox_state.agent_watch,
        "agent_snapshot",
        lambda *a, **k: {**AGENT, "last_event_at": now + 4},
    )
    inbox_state.sync_children(ctx, _cache())
    assert published_status(ctx)["activity_at"] == now + 4


def test_failed_agent_snapshot_does_not_lower_activity_or_last_event(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    now = ctx.now.timestamp()
    event = now - 30
    snaps = [{**AGENT, "last_event_at": event}, {**AGENT, "state": None, "last_event_at": None}]
    monkeypatch.setattr(inbox_state.agent_watch, "agent_snapshot", lambda *a, **k: snaps.pop(0))
    probes.head = (None, None)
    cache = _cache()
    inbox_state.sync_children(ctx, cache)
    first = published_status(ctx)["activity_at"]
    assert first == max(now, 0) or first >= event
    ctx.now += timedelta(seconds=40)
    inbox_state.sync_children(ctx, cache)
    st = published_status(ctx)
    assert st["agent"]["last_event_at"] == event
    assert st["activity_at"] >= first


def test_activity_at_is_monotonic_and_fresh_for_a_recreated_child(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    now = ctx.now.timestamp()
    events = [now - 10, None]
    monkeypatch.setattr(
        inbox_state.agent_watch,
        "agent_snapshot",
        lambda *a, **k: {**AGENT, "last_event_at": events.pop(0)},
    )
    probes.head = (None, None)
    cache = _cache()
    inbox_state.sync_children(ctx, cache)
    first = published_status(ctx)["activity_at"]
    assert first == now - 10 or first == now
    # Drop the carried event time so only the monotonic floor can hold it up.
    saved = json.loads((tmp_path / "inbox-state" / "p-orch.json").read_text())
    saved["children"][0]["status"]["agent"] = None
    (tmp_path / "inbox-state" / "p-orch.json").write_text(json.dumps(saved))
    ctx.now += timedelta(seconds=40)
    inbox_state.sync_children(ctx, cache)
    assert published_status(ctx)["activity_at"] >= first
    # A recreated child (new key) starts fresh.
    ctx.nodes["p-kid"] = Node(
        "p-kid", "kid", True, {labels.SPAWNED_BY: "p-orch", "volatile.uuid": "new"}, now + 100
    )
    ctx.now += timedelta(seconds=40)
    events.append(None)
    inbox_state.sync_children(ctx, cache)
    assert published_status(ctx)["activity_at"] == now + 100


def test_repo_dir_comes_from_node_config_without_an_incus_call(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    ctx.nodes["p-kid"] = Node(
        "p-kid", "kid", True, {labels.SPAWNED_BY: "p-orch", "user.jailbee.repo_dir": "/r/x"}
    )
    calls = []
    monkeypatch.setattr(
        inbox_state.lifecycle, "container_repo_dir", lambda *a: calls.append(a) or "/fallback"
    )
    seen = []
    monkeypatch.setattr(
        inbox_state.child_status,
        "probe_head",
        lambda incus, full, repo, *, uid: seen.append(repo) or (HEAD, False),
    )
    inbox_state.sync_children(ctx, _cache())
    assert seen == ["/r/x"] and calls == []


def test_repo_dir_falls_back_to_bounded_incus_when_label_missing(tmp_path, probes, monkeypatch):
    ctx = context(tmp_path)
    calls = []
    monkeypatch.setattr(
        inbox_state.lifecycle, "container_repo_dir", lambda *a: calls.append(a) or "/fallback"
    )
    inbox_state.sync_children(ctx, _cache())
    assert len(calls) == 1 and calls[0][1] is ctx.incus.with_exec_timeout.return_value


def test_agent_host_pid_matches_nspid_and_start(monkeypatch):
    from jailbee.procstat import ProcSample

    samples = {10: ProcSample("claude", 0, 77), 11: ProcSample("claude", 0, 78)}
    monkeypatch.setattr(inbox_state.procstat, "read_nspid", lambda pid: {10: 5, 11: 5}[pid])
    agent = AgentStatus(True, "claude", pid=5, proc_start=78)
    assert inbox_state._agent_host_pid(samples, agent) == 11
    assert inbox_state._agent_host_pid(samples, None) is None
    assert inbox_state._agent_host_pid(samples, AgentStatus(True, "claude")) is None


def _orch_context(tmp_path, orch, kid):
    parent = Node(orch, orch, True, {})
    child = Node(kid, kid, True, {labels.SPAWNED_BY: orch}, None, None, 0)
    return ActionContext(
        cfg=MagicMock(),
        gcfg=MagicMock(),
        incus=MagicMock(),
        root=tmp_path,
        sender=parent,
        nodes={parent.full: parent, child.full: child},
        now=datetime(2026, 10, 30, 12, tzinfo=UTC),
    )


def test_two_orchestrators_sharing_one_cache_both_get_cpu(tmp_path, probes):
    import dataclasses
    import time

    cache = child_status.StatusCache()  # real samplers
    a = _orch_context(tmp_path, "orch-a", "kid-a")
    b = _orch_context(tmp_path, "orch-b", "kid-b")
    seen = []
    for round_no in range(4):
        for ctx, kid in ((a, "kid-a"), (b, "kid-b")):
            node = dataclasses.replace(ctx.nodes[kid], cpu_usage_ns=round_no * 10_000_000_000)
            ctx.nodes[kid] = node
            ctx.now += timedelta(seconds=100)
            time.sleep(0.002)
            inbox_state.sync_children(ctx, cache)
            seen.append((round_no, kid, published_status(ctx)["cpu_percent"]))
    for round_no, kid, cpu in seen:
        if round_no == 0:
            assert cpu is None
        else:
            assert cpu is not None, (round_no, kid)


def test_note_delivery_creates_and_preserves(tmp_path):
    inbox_state.note_delivery(tmp_path, "p-orch", "p-kid", 42.0)
    path = tmp_path / "inbox-state" / "p-orch.json"
    saved = json.loads(path.read_text())
    assert saved["children"][0]["full"] == "p-kid"
    assert saved["children"][0]["last_to_child_at"] == 42.0
    saved["children"][0]["status"] = {"head": None}
    saved["children"].append({"full": "p-other", "last_to_child_at": 1.0})
    path.write_text(json.dumps(saved))
    inbox_state.note_delivery(tmp_path, "p-orch", "p-kid", 50.0)
    after = json.loads(path.read_text())
    assert after["children"][0]["last_to_child_at"] == 50.0
    assert after["children"][0]["status"] == {"head": None}
    assert after["children"][1] == {"full": "p-other", "last_to_child_at": 1.0}


def test_note_delivery_swallows_oserror(tmp_path):
    (tmp_path / "inbox-state").write_text("a file, not a directory")
    inbox_state.note_delivery(tmp_path, "p-orch", "p-kid", 1.0)


def test_last_to_child_at_published_after_note_delivery(tmp_path, probes):
    ctx = context(tmp_path)
    inbox_state.note_delivery(tmp_path, "p-orch", "p-kid", 123.0)
    inbox_state.sync_children(ctx, _cache())
    assert published_status(ctx)["last_to_child_at"] == 123.0
    assert published_status(ctx)["activity_at"] >= ctx.now.timestamp()
