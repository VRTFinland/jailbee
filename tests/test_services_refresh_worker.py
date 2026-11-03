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
    cfg = make_cfg(tmp_path)
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


def job(path, *, elevated_plan=False, force=False):
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
        ("db",),
        (
            elevated.PlannedService(
                "db",
                elevated_plan,
                (step,) if elevated_plan else (),
                "loose" if elevated_plan else None,
                (),
            ),
            elevated.PlannedService("web", False, (), None, ()),
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
                "names": ["db"],
                "force": force,
            }
        )
    )


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
    mocker.patch.object(elevated, "run_plain_refresh", side_effect=RuntimeError("broken"))
    assert elevated.run_job(cfg, incus, path) == 1
    assert path.exists()
    with Session(engine) as session:
        assert session.get(BackgroundJob, "proj-c") is None


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


def test_tampered_elevation_never_executes(rig, mocker):
    cfg, _engine, deliver, incus, path = rig
    job(path, elevated_plan=True)
    data = json.loads(path.read_text())
    data["elevated"] = False
    path.write_text(json.dumps(data))
    run = mocker.patch.object(elevated, "run_refresh")
    assert elevated.run_job(cfg, incus, path) == 1
    assert path.exists()
    run.assert_not_called()
    deliver.assert_not_called()
