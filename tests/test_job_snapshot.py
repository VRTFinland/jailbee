"""Job snapshots remain coherent when a worker finishes between gather and display."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session

from jailbee import background, jobs
from jailbee.dashboard.format import dashboard_cell, row_phase
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.notifications import NotificationQueue, job_rows
from jailbee.db import get_engine
from jailbee.lifecycle import list_containers, ls_field_specs
from jailbee.state_service.protocol import Snapshot, decode, encode

JOB_CASES = [
    ("autostart", "autostart"),
    ("create", "creating"),
    ("boot", "starting"),
    ("boot", "autostart"),  # restart uses the same boot job kind
    ("destroy", "deleting"),
]


@pytest.mark.parametrize(
    ("kind", "phase"),
    JOB_CASES,
)
def test_successful_worker_exit_does_not_turn_a_gathered_job_into_an_error(
    make_cfg, tmp_path, monkeypatch, mocker, kind, phase
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path)
    name = f"{cfg.container_prefix}-work"
    incus = MagicMock()
    incus.list_containers.return_value = []
    alive = mocker.patch("jailbee.background.worker_alive", return_value=True)
    with Session(get_engine()) as session:
        background.start_job(
            session,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="work",
            pid=4242,
            log_path="/log",
            now=datetime.now(UTC),
            op_kind=kind,
        )
        background.set_phase(session, name, phase, now=datetime.now(UTC))

    containers = list_containers(cfg, incus, with_background=True)
    with Session(get_engine()) as session:
        job = background.list_jobs(session, cfg.container_prefix)[name]
    assert containers[0].job_live
    snapshot = Snapshot(
        1,
        datetime.now(UTC),
        False,
        [RepoGroup(cfg.container_prefix, str(tmp_path), None, containers)],
    )
    decoded = decode(encode(snapshot))
    assert isinstance(decoded, Snapshot)
    container = decoded.groups[0].containers[0]

    # Success removes the tracking row before the worker exits. Display still
    # holds the previously gathered row until the next snapshot arrives.
    with Session(get_engine()) as session:
        background.delete_job(session, name)
    alive.return_value = False

    queue = NotificationQueue()
    queue.set_jobs(job_rows(decoded.groups))
    assert queue.counts().attention == 0
    assert not queue.jobs[0].phase.endswith(" (dead)")
    assert row_phase(container) != "failed"
    now = datetime.now(UTC)
    field = next(f for f in ls_field_specs(now=now) if f.name == "job")
    assert "(dead)" not in field.cell(container)
    assert "(dead)" not in field.json(container)
    assert "[red]" not in dashboard_cell(field, container, now)
    job_field = next(f for f in jobs.job_field_specs(now=now, all_repos=False) if f.name == "phase")
    assert "(dead)" not in job_field.cell(job)
    from jailbee.qtui.model import cell_tooltip
    from jailbee.tui import container_options

    assert "(dead)" not in cell_tooltip(container, field).split("\n", 1)[1]
    assert "(dead)" not in container_options([container])[0].title
    assert list_containers(cfg, incus, with_background=True) == []


@pytest.mark.parametrize("kind", ["autostart", "create", "boot", "destroy"])
def test_successful_exit_during_job_collection_does_not_publish_a_dead_row(
    make_cfg, tmp_path, monkeypatch, mocker, kind
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path)
    name = f"{cfg.container_prefix}-work"
    incus = MagicMock()
    incus.list_containers.return_value = []
    with Session(get_engine()) as session:
        background.start_job(
            session,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="work",
            pid=4242,
            log_path="/log",
            now=datetime.now(UTC),
            op_kind=kind,
        )

    def finish_before_probe(pid):
        with Session(get_engine()) as session:
            background.delete_job(session, name)
        return False

    mocker.patch("jailbee.background.worker_alive", side_effect=finish_before_probe)
    assert list_containers(cfg, incus, with_background=True) == []


def test_replacement_job_finishing_during_collection_is_not_dead(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    name = "repo-work"

    def start(pid):
        with Session(get_engine()) as session:
            background.start_job(
                session,
                container_name=name,
                container_prefix="repo",
                branch="work",
                pid=pid,
                log_path="/log",
                now=datetime.now(UTC),
            )

    start(4242)

    def probe(pid):
        if pid == 4242:
            start(4243)
        else:
            with Session(get_engine()) as session:
                background.delete_job(session, name)
        return False

    mocker.patch("jailbee.background.worker_alive", side_effect=probe)
    with Session(get_engine()) as session:
        assert background.list_jobs(session, "repo") == {}


@pytest.mark.parametrize(("kind", "phase"), JOB_CASES)
def test_worker_killed_mid_phase_still_publishes_one_error(
    make_cfg, tmp_path, monkeypatch, mocker, kind, phase
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path)
    name = f"{cfg.container_prefix}-work"
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": name,
            "status": "Running",
            "config": {},
            "profiles": ["default", f"{cfg.container_prefix}-base"],
            "state": None,
        }
    ]
    incus.config_get.return_value = None
    alive = mocker.patch("jailbee.background.worker_alive", return_value=True)
    with Session(get_engine()) as session:
        background.start_job(
            session,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="work",
            pid=4242,
            log_path="/log",
            now=datetime.now(UTC),
            op_kind=kind,
        )
        background.set_phase(session, name, phase, now=datetime.now(UTC))

    queue = NotificationQueue()
    for live in (True, False, False):
        # A killed worker cannot delete its tracking row on success.
        alive.return_value = live
        containers = list_containers(cfg, incus, with_background=True)
        queue.set_jobs(job_rows([RepoGroup(cfg.container_prefix, str(tmp_path), None, containers)]))
        assert queue.counts().attention == (0 if live else 1)
        assert queue.attention_items() == (frozenset() if live else {("job", name)})
        assert (row_phase(containers[0]) == "failed") is not live
        assert queue.jobs[0].phase.endswith(" (dead)") is not live

    with Session(get_engine()) as session:
        assert name in background.list_all_jobs(session)
        outcome = background.clear_job(session, name)
    assert outcome.cleared and outcome.reason == "stale"
    containers = list_containers(cfg, incus, with_background=True)
    queue.set_jobs(job_rows([RepoGroup(cfg.container_prefix, str(tmp_path), None, containers)]))
    assert queue.counts().attention == 0


def test_observed_live_is_none_on_a_db_loaded_row(make_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path)
    name = f"{cfg.container_prefix}-work"
    with Session(get_engine()) as session:
        background.start_job(
            session,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="work",
            pid=4242,
            log_path="/log",
            now=datetime.now(UTC),
            op_kind="create",
        )
    with Session(get_engine()) as session:
        job = background.get_job(session, name)
        assert job is not None
        assert job.observed_live is None
        job.observe_live(True)
        assert job.observed_live is True
