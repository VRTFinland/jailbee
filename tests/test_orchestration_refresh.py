from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.orchestration import actions, labels
from jailbee.orchestration.protocol import RefreshManifest
from tests.test_orchestration_actions import _ctx, _grant, _node


def test_refresh_refuses_stopped_child(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant()}), [
        _node("proj-c", running=False, **{labels.SPAWNED_BY: "proj-o"})
    ])
    manifest = RefreshManifest(version=1, id="r1", type="refresh", child="c", services=["db"])
    assert actions.dispatch(ctx, manifest) == actions.Outcome("refused", "child `c` is not running")


def test_refresh_refuses_busy_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant()}), [
        _node("proj-c", **{labels.SPAWNED_BY: "proj-o"})
    ])
    monkeypatch.setattr(actions, "live_job_kind", lambda name: "boot")
    manifest = RefreshManifest(version=1, id="r1", type="refresh", child="c", services=["db"])
    assert actions.dispatch(ctx, manifest) == actions.Outcome(
        "refused", "child `c` is busy (boot running); refresh again when it finishes"
    )


@pytest.fixture
def refresh_rig(tmp_path, mocker):
    import json
    from jailbee.incus import ExecResult
    from jailbee.services.elevated import Plan, PlannedService
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant()}), [_node("proj-c", **{labels.SPAWNED_BY: "proj-o"})])
    plan = Plan(("db",), ("db",), (PlannedService("db", False, (), None, ()),))
    mocker.patch.object(actions, "live_job_kind", return_value=None)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    run = mocker.patch("jailbee.container_app.run_in_container", return_value=ExecResult(0, json.dumps(plan.to_json()).encode(), b""))
    mocker.patch("jailbee.db.state_dir", return_value=tmp_path)
    return ctx, run


def test_accepted_plan_is_persisted_with_force(refresh_rig, mocker, tmp_path):
    import json
    from types import SimpleNamespace
    ctx, run = refresh_rig
    launch = mocker.patch("jailbee.create_flow.launch_refresh_background", return_value=SimpleNamespace(log_path=Path("/log")))
    m = RefreshManifest(version=1, id="r1", type="refresh", child="c", services=["db"], force=True)
    out = actions.dispatch(ctx, m)
    assert out.state == "started" and "/log" in out.detail
    job = json.loads((tmp_path / "refresh-jobs/r1.json").read_text())
    assert job == {"child_full": "proj-c", "orchestrator_full": "proj-o", "manifest_id": "r1", "elevated": False, "payload": json.loads(run.return_value.stdout), "names": ["db"], "force": True}
    assert launch.call_args.kwargs["job_path"] == tmp_path / "refresh-jobs/r1.json"
    assert run.call_args.args[3] == ["service", "--repo", "/repo", "plan", "--op", "refresh", "--json", "db"]


def test_plan_failure_and_launch_failure(refresh_rig, mocker, tmp_path):
    from jailbee.incus import ExecResult
    ctx, run = refresh_rig
    m = RefreshManifest(version=1, id="r1", type="refresh", child="c", services=["db"])
    good = run.return_value
    run.return_value = ExecResult(1, b"", b"unknown service")
    assert actions.dispatch(ctx, m) == actions.Outcome("refused", "unknown service")
    run.return_value = good
    mocker.patch("jailbee.create_flow.launch_refresh_background", side_effect=OSError("failed"))
    assert actions.dispatch(ctx, m).state == "failed"
    assert not (tmp_path / "refresh-jobs/r1.json").exists()


def test_elevated_probe_and_foreign_agents_fail_closed(refresh_rig, mocker):
    import json
    from jailbee.incus import ExecResult
    from jailbee.services.elevated import Plan, PlannedService
    from jailbee.orchestration import refresh
    ctx, run = refresh_rig
    step = {"name": "setup", "run": "true", "env": {}, "working_dir": "", "timeout": 3, "continue_on_error": False}
    plan = Plan(("db",), ("db",), (PlannedService("db", True, (step,), "loose", ()),))
    run.return_value = ExecResult(0, json.dumps(plan.to_json()).encode(), b"")
    policy = mocker.patch.object(refresh, "orchestrator_policy", return_value=[])
    probe = mocker.patch.object(refresh.agent_watch, "probe", return_value=None)
    m = RefreshManifest(version=1, id="r1", type="refresh", child="c", services=["db"])
    assert "could not determine" in actions.dispatch(ctx, m).detail
    probe.return_value = mocker.Mock()
    mocker.patch.object(refresh, "foreign_agents", return_value=["claude (pid 12)"])
    assert "claude (pid 12)" in actions.dispatch(ctx, m).detail
    policy.return_value = ["loose forbidden"]
    assert actions.dispatch(ctx, m).detail == "refresh needs loose forbidden"


@pytest.mark.parametrize("body", [
    {"type": "stop", "child": "c"},
    {"type": "destroy", "child": "c", "force": True},
    {"type": "restart", "child": "c", "prompt": "hello"},
    {"type": "rights", "child": "c", "revoke": {"loose": True}},
])
def test_mutations_refused_during_refresh(tmp_path, monkeypatch, body):
    from jailbee.orchestration.protocol import StopManifest, DestroyManifest, RestartManifest, RightsManifest
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant()}), [_node("proj-c", **{labels.SPAWNED_BY: "proj-o"})])
    monkeypatch.setattr(actions, "live_job_kind", lambda name: "refresh")
    ctx.incus.config_get.return_value = _grant()
    cls = {"stop": StopManifest, "destroy": DestroyManifest, "restart": RestartManifest, "rights": RightsManifest}[body["type"]]
    manifest = cls.model_validate({"version": 1, "id": "r1", **body})
    result = actions.dispatch(ctx, manifest)
    assert result.state == "refused" and "refresh" in result.detail
    ctx.incus.config_set.assert_not_called()
