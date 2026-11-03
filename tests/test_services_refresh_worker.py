import json
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from jailbee import background
from jailbee.db.models import JOB_REFRESH, BackgroundJob
from jailbee.orchestration import labels
from jailbee.orchestration.grant import Grant, Grantable
from jailbee.services import elevated
from tests.conftest import make_cfg


@pytest.fixture
def rig(tmp_path, mocker):
    cfg = make_cfg(tmp_path).model_copy(update={"container_prefix": "proj"})
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    SQLModel.metadata.create_all(engine)
    mocker.patch("jailbee.db.get_engine", return_value=engine)
    mocker.patch("jailbee.orchestration.state.state_root", return_value=tmp_path)
    deliver = mocker.patch("jailbee.orchestration.outbox.deliver")
    grant = Grant(grantable=Grantable(loose=True)).to_label()
    incus = Mock()
    incus.list_containers.return_value = [
        {"name": "proj-o", "status": "Running", "config": {labels.GRANT: grant}},
        {"name": "proj-c", "status": "Running", "config": {labels.SPAWNED_BY: "proj-o"}},
    ]
    path = tmp_path / "job.json"
    return cfg, engine, deliver, incus, path


def job(path, *, elevated_plan=False, force=False, mixed=False):
    step = {
        "name": "setup",
        "run": "true",
        "env": {},
        "working_dir": "",
        "timeout": 3,
        "continue_on_error": False,
    }
    plan = elevated.Plan(
        ("db", "web"),
        ("db", "web") if mixed else ("db",),
        (
            elevated.PlannedService(
                "db",
                elevated_plan,
                (step,) if elevated_plan else (),
                "loose" if elevated_plan else None,
                (),
            ),
            elevated.PlannedService("web", mixed, (step,) if mixed else (), None, ()),
        ),
    )
    path.write_text(
        json.dumps(
            {
                "child_full": "proj-c",
                "orchestrator_full": "proj-o",
                "manifest_id": "r1",
                "elevated": elevated_plan,
                "payload": plan.to_json(),
                "names": ["db", "web"] if mixed else ["db"],
                "force": force,
            }
        )
    )


def test_same_job_concurrent_workers_execute_once(rig, mocker):
    import threading

    cfg, _, _, incus, path = rig
    job(path)
    entered = threading.Event()
    release = threading.Event()
    report = elevated.RefreshReport((), True, "not running")

    def execute(*args):
        entered.set()
        assert release.wait(5)
        return report

    run = mocker.patch.object(elevated, "run_plain_refresh", side_effect=execute)
    results = []
    second_read = threading.Event()
    second_continue = threading.Event()
    original_read = type(path).read_bytes

    def read_bytes(p):
        data = original_read(p)
        if threading.current_thread().name == "second":
            second_read.set()
            assert second_continue.wait(5)
        return data

    mocker.patch.object(type(path), "read_bytes", read_bytes)
    first = threading.Thread(target=lambda: results.append(elevated.run_job(cfg, incus, path)))
    second = threading.Thread(
        name="second", target=lambda: results.append(elevated.run_job(cfg, incus, path))
    )
    # Capture the original request in the second worker, before the first starts.
    second.start()
    assert second_read.wait(5)
    first.start()
    second_continue.set()
    assert entered.wait(5)
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert run.call_count == 1


@pytest.mark.parametrize("operation", ["stop", "restart", "destroy", "rights", "pending"])
def test_mutations_serialize_with_refresh_claim(rig, mocker, operation):
    import threading
    from datetime import UTC, datetime

    from jailbee.global_config import GlobalConfig
    from jailbee.orchestration import actions, finishing, rights
    from jailbee.orchestration.protocol import (
        DestroyManifest,
        RestartManifest,
        RightsManifest,
        StopManifest,
    )

    cfg, _, _, incus, path = rig
    job(path)
    orch = actions.Node("proj-o", "o", True, {})
    child = actions.Node("proj-c", "c", True, {labels.SPAWNED_BY: "proj-o"})
    ctx = actions.ActionContext(
        cfg,
        GlobalConfig(),
        incus,
        path.parent,
        orch,
        {orch.full: orch, child.full: child},
        datetime.now(UTC),
    )
    entered = threading.Event()
    proceed = threading.Event()
    done = threading.Event()

    def mutation(*args):
        entered.set()
        assert proceed.wait(5)
        return actions.done("done") if operation != "pending" else None

    target, name, manifest = {
        "stop": (
            actions,
            "_handle_stop_locked",
            StopManifest(version=1, id="s1", type="stop", child="c"),
        ),
        "restart": (
            actions,
            "_handle_restart_locked",
            RestartManifest(version=1, id="r1", type="restart", child="c", prompt="p"),
        ),
        "destroy": (
            actions,
            "_handle_destroy_locked",
            DestroyManifest(version=1, id="d1", type="destroy", child="c"),
        ),
        "rights": (
            rights,
            "_handle_rights_locked",
            RightsManifest(version=1, id="x1", type="rights", child="c", revoke={"loose": True}),
        ),
        "pending": (finishing, "_settle_destroys_locked", None),
    }[operation]
    mocker.patch.object(target, name, side_effect=mutation)
    original = background.claim_job

    def claim(*args, **kwargs):
        assert done.is_set()
        return original(*args, **kwargs)

    mocker.patch.object(background, "claim_job", side_effect=claim)
    mocker.patch.object(
        elevated, "run_plain_refresh", return_value=elevated.RefreshReport((), True, "none")
    )

    def invoke():
        if operation == "pending":
            finishing.settle_destroys(ctx)
        else:
            getattr(
                target,
                {
                    "stop": "handle_stop",
                    "restart": "handle_restart",
                    "destroy": "handle_destroy",
                    "rights": "handle_rights",
                }[operation],
            )(ctx, manifest)
        done.set()

    results = []
    a = threading.Thread(target=invoke)
    b = threading.Thread(target=lambda: results.append(elevated.run_job(cfg, incus, path)))
    a.start()
    assert entered.wait(5)
    b.start()
    proceed.set()
    a.join(5)
    b.join(5)
    assert done.is_set() and not b.is_alive()
    assert results == [0]
    assert background.claim_job.called


def test_plain_worker_delivers_and_releases(rig, mocker):
    cfg, engine, deliver, incus, path = rig
    job(path)
    report = elevated.RefreshReport(
        (elevated.ServiceOutcome("db", "prepared, restarted"),), True, "not running"
    )
    plain = mocker.patch.object(elevated, "run_plain_refresh", return_value=report)
    pause = mocker.patch.object(elevated, "pause_agent")
    assert elevated.run_job(cfg, incus, path) == 0
    assert deliver.call_args.args[3].detail == report.summary()
    assert deliver.call_args.args[3].state == "done"
    assert plain.call_args.args[3] == ("db",)
    pause.assert_not_called()
    assert not path.exists()
    with Session(engine) as session:
        assert session.get(BackgroundJob, "proj-c") is None


def test_elevated_worker_uses_accepted_plan_and_force(rig, mocker):
    cfg, _engine, deliver, incus, path = rig
    job(path, elevated_plan=True, force=True)
    mocker.patch("jailbee.orchestration.agent_watch.probe", return_value=Mock())
    mocker.patch.object(elevated, "foreign_agents", return_value=[])
    mocker.patch("jailbee.docker_daemon.mirror_endpoint_or_none", return_value=None)
    watch = mocker.patch("jailbee.orchestration.watch.register")
    report = elevated.RefreshReport(
        (elevated.ServiceOutcome("db", "prepared, restarted"),), True, "resumed"
    )

    def execute(*args, **kwargs):
        assert args[3].services[1].network is None
        assert kwargs["force"] is True
        kwargs["before_resume"]()
        assert watch.called
        return report

    mocker.patch.object(elevated, "run_refresh", side_effect=execute)
    assert elevated.run_job(cfg, incus, path) == 0
    assert deliver.call_args.args[3].detail == report.summary()
    assert incus.config_unset_checked.call_count == 5


def test_busy_worker_delivers_failure_without_execution(rig, mocker):
    from datetime import UTC, datetime

    cfg, engine, deliver, incus, path = rig
    job(path)
    with Session(engine) as session:
        assert background.claim_job(
            session,
            container_name="proj-c",
            container_prefix="proj",
            pid=12345,
            phase="refresh",
            now=datetime.now(UTC),
            op_kind=JOB_REFRESH,
        )
    mocker.patch.object(background, "worker_alive", return_value=True)
    run = mocker.patch.object(elevated, "run_plain_refresh")
    assert elevated.run_job(cfg, incus, path) == 1
    assert "busy" in deliver.call_args.args[3].detail
    run.assert_not_called()
    assert not path.exists()
    with Session(engine) as session:
        assert session.get(BackgroundJob, "proj-c").pid == 12345


def test_delivery_failure_preserves_job(rig, mocker):
    cfg, engine, deliver, incus, path = rig
    job(path)
    deliver.side_effect = OSError("disk full")
    execute = mocker.patch.object(elevated, "run_plain_refresh", side_effect=RuntimeError("broken"))
    assert elevated.run_job(cfg, incus, path) == 1
    assert path.exists()
    with Session(engine) as session:
        assert session.get(BackgroundJob, "proj-c") is None
    saved_id = json.loads(path.read_text())["final_status"]["id"]
    deliver.side_effect = None
    assert elevated.run_job(cfg, incus, path) == 1
    assert deliver.call_args.args[3].id == saved_id
    assert execute.call_count == 1
    assert "broken" in deliver.call_args.args[3].detail
    assert not path.exists()


def test_final_persist_failure_never_reexecutes(rig, mocker):
    from jailbee.orchestration import state

    cfg, engine, _deliver, incus, path = rig
    job(path)
    original = state.atomic_write_text

    def write(target, text):
        if "final_status" in json.loads(text):
            raise OSError("disk full")
        original(target, text)

    mocker.patch.object(state, "atomic_write_text", side_effect=write)
    execute = mocker.patch.object(
        elevated, "run_plain_refresh", return_value=elevated.RefreshReport((), True, "not running")
    )
    assert elevated.run_job(cfg, incus, path) == 1
    mocker.patch.object(background, "worker_alive", return_value=False)
    assert elevated.run_job(cfg, incus, path) == 1
    assert execute.call_count == 1
    with Session(engine) as session:
        assert session.get(BackgroundJob, "proj-c") is None


def test_release_error_reports_failed_with_service_outcome(rig, mocker):
    cfg, _engine, deliver, incus, path = rig
    job(path)
    mocker.patch.object(
        elevated,
        "run_plain_refresh",
        return_value=elevated.RefreshReport(
            (elevated.ServiceOutcome("db", "restarted"),), True, "not running"
        ),
    )
    mocker.patch.object(background, "release_job", side_effect=RuntimeError("release failed"))
    assert elevated.run_job(cfg, incus, path) == 1
    item = deliver.call_args.args[3]
    assert item.state == "failed"
    assert "db: restarted" in item.detail and "release failed" in item.detail


def test_crash_before_cleanup_delivers_conservative_failure(rig, mocker):
    cfg, _engine, deliver, incus, path = rig
    job(path)
    report = elevated.RefreshReport(
        (elevated.ServiceOutcome("db", "restarted"),), True, "not running"
    )
    execute = mocker.patch.object(elevated, "run_plain_refresh", return_value=report)
    release = mocker.patch.object(background, "release_job", side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        elevated.run_job(cfg, incus, path)
    release.side_effect = None
    assert elevated.run_job(cfg, incus, path) == 1
    item = deliver.call_args.args[3]
    assert item.state == "failed" and "cleanup unconfirmed" in item.detail
    assert "db: restarted" in item.detail
    assert execute.call_count == 1


def test_concurrent_claims_only_one_succeeds(rig, mocker):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime
    from threading import Barrier

    cfg, engine, _, _, _ = rig
    mocker.patch.object(background, "worker_alive", return_value=True)
    barrier = Barrier(2)

    def claim(pid):
        with Session(engine) as session:
            barrier.wait()
            return background.claim_job(
                session,
                container_name="proj-c",
                container_prefix=cfg.container_prefix,
                pid=pid,
                phase="refresh",
                now=datetime.now(UTC),
                op_kind=JOB_REFRESH,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, [12345, 12346])) == [False, True]


@pytest.mark.parametrize(
    "watch_fails,restore_fails", [(False, False), (True, False), (False, True)]
)
def test_real_sequence_uses_accepted_mixed_plan_and_supervision(
    rig, mocker, watch_fails, restore_fails
):
    from jailbee.incus import ExecResult

    cfg, engine, deliver, incus, path = rig
    job(path, elevated_plan=True, force=True, mixed=True)
    events = []
    values = {}
    incus.config_get_checked.side_effect = lambda _, key: values.get(key)
    incus.config_set.side_effect = lambda _, key, value: values.__setitem__(key, value)
    mocker.patch("jailbee.orchestration.agent_watch.probe", return_value=Mock())
    mocker.patch.object(elevated, "foreign_agents", return_value=[])
    mocker.patch("jailbee.docker_daemon.mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    snapshots = [("strict", set()), ("strict", set()), ("strict", set())]
    if restore_fails:
        snapshots[1] = RuntimeError("restore transport")
    mocker.patch.object(elevated, "_snapshot", side_effect=snapshots)
    pause = mocker.patch.object(
        elevated,
        "pause_agent",
        return_value=elevated.PausedAgent("claude", "11111111-1111-1111-1111-111111111111", None),
    )
    resume = mocker.patch.object(
        elevated, "resume_agent", side_effect=lambda *a: events.append("resume")
    )

    def register(*args):
        events.append("watch")
        if watch_fails:
            raise RuntimeError("watch disk")

    mocker.patch("jailbee.orchestration.watch.register", side_effect=register)
    stages = mocker.patch("jailbee.autostart.run_stage")
    run = mocker.patch(
        "jailbee.container_app.run_in_container", return_value=ExecResult(0, b"", b"")
    )
    result = elevated.run_job(cfg, incus, path)
    item = deliver.call_args.args[3]
    assert result == (1 if watch_fails or restore_fails else 0)
    assert item.state == ("failed" if watch_fails or restore_fails else "done")
    assert stages.call_args_list[0].args[3].network == "loose"
    if not restore_fails:
        assert stages.call_args_list[1].args[3].network is None
        assert run.call_count == 1
        assert run.call_args.args[3][-3:] == ["restart", "db", "web"]
        assert "db: prepared, restarted" in item.detail
    if watch_fails:
        assert events == ["watch"]
        assert "resume withheld: could not register agent supervision (watch disk)" in item.detail
        assert (
            "jailbee-inbox send restart --child c --agent-only "
            "--resume 11111111-1111-1111-1111-111111111111 --text 'Continue your task.'"
            in item.detail
        )
        resume.assert_not_called()
    elif restore_fails:
        assert "resume withheld: container may still be elevated" in item.detail
        assert events == []
        resume.assert_not_called()
    else:
        assert events == ["watch", "resume"]
    assert pause.call_args.kwargs["force"] is True
    with Session(engine) as session:
        assert session.get(BackgroundJob, "proj-c") is None


def test_grant_revocation_reports_failure(rig, mocker):
    cfg, engine, deliver, incus, path = rig
    job(path)
    incus.list_containers.return_value[0]["config"][labels.GRANT] = Grant(
        operations=("message", "import", "spawn")
    ).to_label()
    run = mocker.patch.object(elevated, "run_plain_refresh")
    assert elevated.run_job(cfg, incus, path) == 1
    assert "revoked" in deliver.call_args.args[3].detail
    assert deliver.call_args.args[3].state == "failed"
    run.assert_not_called()
    with Session(engine) as session:
        assert session.get(BackgroundJob, "proj-c") is None


def test_hidden_cli_delegates(rig, mocker):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg, _, _, incus, path = rig
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    worker = mocker.patch.object(elevated, "run_job", return_value=0)
    result = CliRunner().invoke(app, ["_refresh-worker", "--job", str(path)])
    assert result.exit_code == 0, result.output
    worker.assert_called_once_with(cfg, incus, path)


def test_tampered_elevation_never_executes(rig, mocker):
    cfg, _engine, deliver, incus, path = rig
    job(path, elevated_plan=True)
    data = json.loads(path.read_text())
    data["elevated"] = False
    path.write_text(json.dumps(data))
    run = mocker.patch.object(elevated, "run_refresh")
    assert elevated.run_job(cfg, incus, path) == 1
    assert not path.exists()
    run.assert_not_called()
    assert deliver.call_args.args[3].state == "failed"
    assert "elevation mismatch" in deliver.call_args.args[3].detail
