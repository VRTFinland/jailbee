from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jailbee.orchestration import actions, agent_watch, labels
from jailbee.orchestration.protocol import RestartManifest
from jailbee.orchestration.runner import Runner
from jailbee.orchestration.state import load_queue, load_reservations
from jailbee.orchestration.watch import WatchEntry, load_watch, register
from tests.test_orchestration_lifecycle_ops import Rig, seams  # noqa: F401


def restart(**kwargs) -> RestartManifest:
    return RestartManifest.model_validate(
        dict(version=1, id="r-new", type="restart", child="c1", prompt="new task", agent_only=True)
        | kwargs
    )


@pytest.fixture
def session(monkeypatch):
    from tests.conftest import make_config, with_agent

    monkeypatch.setattr("tests.test_orchestration_lifecycle_ops.make_config", lambda path: with_agent(make_config(path), "claude", enabled=True, prompt_args='"$(cat "$JAILBEE_INITIAL_PROMPT_FILE")"'))
    launch = MagicMock()
    monkeypatch.setattr(actions, "live_job_kind", MagicMock(return_value=None))
    monkeypatch.setattr(agent_watch, "probe", MagicMock(return_value=agent_watch.AgentStatus(True, "claude")))
    return launch


@pytest.mark.parametrize("running,extra,detail", [
    (False, {}, "must be running"),
    (True, {}, "working"),
    (True, {"autostart_profile": "heavy"}, "autostart_profile"),
])
def test_agent_only_refusal(tmp_path, seams, session, running, extra, detail):
    rig = Rig(tmp_path, running=running)
    out = actions.handle_restart(rig.ctx(), restart(**extra))
    assert out.state == "refused" and detail in out.detail
    seams["boot"].assert_not_called()
    rig.incus.config_unset_checked.assert_not_called()
    assert load_queue(tmp_path) == {}


@pytest.mark.parametrize("mode", ["keep", "change", "default"])
def test_agent_only_relaunch_at_capacity_without_boot_or_queue(tmp_path, seams, session, monkeypatch, mode):
    from jailbee.orchestration import agent_session

    old = Runner(agent="claude", model="sonnet")
    selected = Runner(agent="claude", model="opus")
    rig = Rig(tmp_path, running=True, limit=1, config={labels.STATE: labels.STATE_DONE, labels.RUNNER: old.to_label()})
    monkeypatch.setattr(agent_session, "relaunch", session)
    monkeypatch.setattr(actions, "runner_problems", MagicMock(return_value=[]))
    monkeypatch.setattr(actions.ActionContext, "default_runner", lambda self: (selected, "repo"))
    opts = {} if mode == "keep" else {"runner": selected.model_dump() if mode == "change" else None}
    out = actions.handle_restart(rig.ctx(), restart(on_done="keep", **opts))
    assert out.state == "started"
    seams["boot"].assert_not_called()
    seams["stop"].assert_not_called()
    session.assert_called_once_with(rig.cfg, rig.incus, rig.child.full,
        actions.child_preamble("orch") + "\n\nnew task", runner=old if mode == "keep" else selected, old_agent="claude")
    entry, = load_watch(tmp_path)[rig.sender.full]
    assert entry.ref == "r-new" and entry.supervise and entry.agent_only and not entry.confirmed
    assert load_queue(tmp_path) == {} and load_reservations(tmp_path) == {}
    rig.incus.config_unset_checked.assert_any_call(rig.child.full, labels.STATE)
    rig.incus.config_unset_checked.assert_any_call(rig.child.full, labels.DONE_SHA)
    rig.incus.config_set.assert_any_call(rig.child.full, labels.ON_DONE, "keep")
    if mode == "keep":
        assert not any(c.args[1] == labels.RUNNER for c in rig.incus.config_set.call_args_list)
    else:
        rig.incus.config_set.assert_any_call(rig.child.full, labels.RUNNER, selected.to_label())


@pytest.mark.parametrize("status,confirmed,allowed", [
    (None, True, False),
    (agent_watch.AgentStatus(False), False, False),
    (agent_watch.AgentStatus(False), True, True),
    (agent_watch.AgentStatus(False, "claude", 0), False, True),
    (agent_watch.AgentStatus(False, "claude", recorded=True), False, True),
    (agent_watch.AgentStatus(True, "claude"), True, False),
])
def test_agent_only_requires_ended_session_or_accepted_done(tmp_path, seams, session, monkeypatch, status, confirmed, allowed):
    from jailbee.orchestration import agent_session

    rig = Rig(tmp_path, running=True)
    monkeypatch.setattr(agent_session, "relaunch", session)
    monkeypatch.setattr(agent_watch, "probe", MagicMock(return_value=status))
    register(tmp_path, rig.sender.full, WatchEntry("old", rig.child.full, "spawn", rig.ctx().now.isoformat(), supervise=True, confirmed=confirmed))
    out = actions.handle_restart(rig.ctx(), restart())
    assert out.state == ("started" if allowed else "refused")
    assert session.called == allowed


@pytest.mark.parametrize("job_kind", ["boot", "create", "autostart"])
def test_agent_only_refuses_active_worker(tmp_path, seams, session, monkeypatch, job_kind):
    rig = Rig(tmp_path, running=True, config={labels.STATE: labels.STATE_DONE})
    monkeypatch.setattr(actions, "live_job_kind", MagicMock(return_value=job_kind))
    out = actions.handle_restart(rig.ctx(), restart())
    assert out.state == "refused" and "working" in out.detail


def test_agent_only_failed_launch_drops_new_watch(tmp_path, seams, session, monkeypatch):
    from jailbee.orchestration import agent_session

    rig = Rig(tmp_path, running=True, config={labels.STATE: labels.STATE_DONE})
    monkeypatch.setattr(agent_session, "relaunch", MagicMock(side_effect=RuntimeError("tmux failed")))
    out = actions.handle_restart(rig.ctx(), restart())
    assert out.state == "refused" and "tmux failed" in out.detail
    assert load_watch(tmp_path).get(rig.sender.full, []) == []


def test_agent_only_prompt_validation_refuses_before_mutation(tmp_path, seams, session, monkeypatch):
    from jailbee import initial_prompt

    rig = Rig(tmp_path, running=True, config={labels.STATE: labels.STATE_DONE})
    monkeypatch.setattr(initial_prompt, "validate_prompt_text", MagicMock(side_effect=initial_prompt.PromptError("too large")))
    out = actions.handle_restart(rig.ctx(), restart())
    assert out.state == "refused" and "too large" in out.detail
    rig.incus.config_unset_checked.assert_not_called()


def test_agent_only_requires_durable_watch(tmp_path, seams, session, monkeypatch):
    from jailbee.orchestration import agent_session

    rig = Rig(tmp_path, running=True, config={labels.STATE: labels.STATE_DONE})
    monkeypatch.setattr(agent_session, "relaunch", session)
    monkeypatch.setattr("jailbee.orchestration.watch.register", MagicMock(side_effect=OSError("disk full")))
    out = actions.handle_restart(rig.ctx(), restart())
    assert out.state == "refused" and "disk full" in out.detail
    session.assert_not_called()
    rig.incus.config_unset_checked.assert_not_called()


def test_agent_only_watch_ignores_boot_jobs_and_reports_new_session(tmp_path, seams, session, monkeypatch):
    from jailbee.orchestration import broker

    rig = Rig(tmp_path, running=True)
    ctx = rig.ctx()
    entry = WatchEntry("r-new", rig.child.full, "restart", ctx.now.isoformat(), supervise=True, agent_only=True)
    register(tmp_path, rig.sender.full, entry)
    monkeypatch.setattr(broker.watch, "verdict", MagicMock(side_effect=AssertionError("no boot verdict")))
    reply = MagicMock()
    monkeypatch.setattr(broker, "_reply", reply)
    memory = broker.BrokerMemory()
    broker._settle_watches(ctx, memory, {})
    ctx.now = ctx.now.replace(second=3)
    broker._settle_watches(ctx, memory, {})
    assert reply.call_args.args[2].state == "done"
    assert load_watch(tmp_path)[rig.sender.full][0].confirmed
    monkeypatch.setattr(agent_watch, "probe", MagicMock(return_value=agent_watch.AgentStatus(False, "claude", 0)))
    deliver = MagicMock()
    monkeypatch.setattr(broker, "deliver", deliver)
    broker._settle_watches(ctx, memory, {})
    assert deliver.call_args.args[3].kind == "blocked"
    assert load_watch(tmp_path).get(rig.sender.full, []) == []
