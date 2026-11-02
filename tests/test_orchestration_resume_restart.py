import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from jailbee.orchestration import actions, labels, state
from jailbee.orchestration.protocol import RestartManifest
from tests.test_orchestration_lifecycle_ops import Rig
from tests.test_orchestration_lifecycle_ops import seams as lifecycle_seams


@pytest.fixture
def seams(monkeypatch):
    return lifecycle_seams.__wrapped__(monkeypatch)


A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def manifest(resume=None, **kwargs):
    return RestartManifest.model_validate(
        dict(version=1, id="r1", type="restart", child="c1", prompt="go", resume=resume) | kwargs
    )


@pytest.mark.parametrize("value", [None, "last", A])
def test_protocol_accepts_resume(value):
    assert manifest(value).resume == value


@pytest.mark.parametrize("value", ["LAST", "../x", A.upper()])
def test_protocol_rejects_invalid_resume(value):
    with pytest.raises(ValidationError):
        manifest(value)


@pytest.fixture
def adapter(monkeypatch):
    adapter = MagicMock()
    adapter.resume_args.return_value = "--resume"
    adapter.session_exists.return_value = True
    monkeypatch.setattr("jailbee.accounts.adapters.base.get_adapter", lambda name: adapter)
    monkeypatch.setattr(
        "jailbee.agents.primary_agent", lambda cfg: type("Agent", (), {"name": "claude"})()
    )
    return adapter


def test_restart_resume_last_resolves_latest(tmp_path, seams, adapter):
    rig = Rig(tmp_path, config={labels.AGENT_SESSIONS: json.dumps([A, B])})
    assert actions.handle_restart(rig.ctx(), manifest("last")).state == "started"
    rig.incus.config_set.assert_any_call(rig.child.full, labels.RESUME, B)
    assert labels.valid_session_id(adapter.resume_args.call_args.args[0])


def test_restart_without_resume_unsets_label(tmp_path, seams):
    rig = Rig(tmp_path)
    assert actions.handle_restart(rig.ctx(), manifest()).state == "started"
    rig.incus.config_unset_checked.assert_any_call(rig.child.full, labels.RESUME)


@pytest.mark.parametrize(
    "requested,history,exists,capable,detail",
    [
        (B, [A], True, True, f"session `{B}` was not recorded for child `c1`"),
        ("last", [], True, True, "child `c1` has no recorded agent session"),
        (A, [A], False, True, f"session `{A}` has no transcript any more"),
        ("last", [], True, False, "agent `claude` cannot resume a session"),
    ],
)
def test_restart_refusals(tmp_path, seams, adapter, requested, history, exists, capable, detail):
    rig = Rig(tmp_path, config={labels.AGENT_SESSIONS: json.dumps(history)})
    adapter.session_exists.return_value = exists
    adapter.resume_args.return_value = "--resume" if capable else None
    assert actions.handle_restart(rig.ctx(), manifest(requested)) == actions.Outcome(
        "refused", detail
    )
    seams["boot"].assert_not_called()


def test_queued_restart_keeps_resolved_id(tmp_path, seams, adapter, monkeypatch):
    rig = Rig(tmp_path, config={labels.AGENT_SESSIONS: json.dumps([A])})
    monkeypatch.setattr(actions, "running_children", lambda ctx: 1)
    assert actions.handle_restart(rig.ctx(), manifest("last")).state == "queued"
    req = state.load_queue(tmp_path)[rig.sender.full][0]
    assert req.resume == A
    rig.child.config[labels.AGENT_SESSIONS] = json.dumps([A, B])
    monkeypatch.setattr(actions, "_still_granted", lambda *args: None)
    assert actions._start_restart(rig.ctx(), req).state == "started"
    rig.incus.config_set.assert_any_call(rig.child.full, labels.RESUME, A)


def test_queue_file_without_resume_key_loads(tmp_path):
    req = state.QueuedRequest("r", "child", "restart", None, "now", "", None, "go", {})
    state.save_queue(tmp_path, {"orch": [req]})
    path = tmp_path / state.QUEUE_FILE
    data = json.loads(path.read_text())
    for entry in data["queues"]["orch"]:
        entry.pop("resume", None)
    path.write_text(json.dumps(data))
    assert state.load_queue(tmp_path)["orch"][0].resume is None
