"""Tests for background job tracking (BackgroundJob table + accessors)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine


def _engine():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


def test_start_job_inserts_row() -> None:
    from jailbee import background
    from jailbee.db.models import BackgroundJob

    engine = _engine()
    now = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        background.start_job(
            s,
            container_name="sampleapp-feat-foo",
            container_prefix="sampleapp",
            branch="feat/foo",
            pid=4242,
            log_path="/tmp/x.log",
            now=now,
        )
        row = s.get(BackgroundJob, "sampleapp-feat-foo")
    assert row is not None
    assert row.phase == background.PHASE_STARTING
    assert row.pid == 4242
    assert row.container_prefix == "sampleapp"
    assert row.branch == "feat/foo"


def test_set_phase_updates_existing_row() -> None:
    from jailbee import background
    from jailbee.db.models import BackgroundJob

    engine = _engine()
    now = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    later = datetime(2026, 6, 3, 12, 5, tzinfo=UTC)
    with Session(engine) as s:
        background.start_job(
            s,
            container_name="c",
            container_prefix="sampleapp",
            branch=None,
            pid=1,
            log_path="/l",
            now=now,
        )
        background.set_phase(s, "c", background.PHASE_CLONING, now=later)
        row = s.get(BackgroundJob, "c")
    assert row is not None
    assert row.phase == background.PHASE_CLONING
    assert row.updated_at == later


def test_fail_job_sets_failed_and_error() -> None:
    from jailbee import background
    from jailbee.db.models import BackgroundJob

    engine = _engine()
    now = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        background.start_job(
            s,
            container_name="c",
            container_prefix="sampleapp",
            branch=None,
            pid=1,
            log_path="/l",
            now=now,
        )
        background.fail_job(s, "c", "boom", now=now)
        row = s.get(BackgroundJob, "c")
    assert row is not None
    assert row.phase == background.PHASE_FAILED
    assert row.error_msg == "boom"


def test_delete_job_removes_row() -> None:
    from jailbee import background
    from jailbee.db.models import BackgroundJob

    engine = _engine()
    now = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        background.start_job(
            s,
            container_name="c",
            container_prefix="sampleapp",
            branch=None,
            pid=1,
            log_path="/l",
            now=now,
        )
        background.delete_job(s, "c")
        assert s.get(BackgroundJob, "c") is None


def test_list_jobs_filters_by_prefix() -> None:
    from jailbee import background

    engine = _engine()
    now = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        background.start_job(
            s,
            container_name="sampleapp-a",
            container_prefix="sampleapp",
            branch=None,
            pid=1,
            log_path="/l",
            now=now,
        )
        background.start_job(
            s,
            container_name="OTHER-b",
            container_prefix="OTHER",
            branch=None,
            pid=2,
            log_path="/l",
            now=now,
        )
        ops = background.list_jobs(s, "sampleapp")
    assert set(ops) == {"sampleapp-a"}


def test_worker_alive_true_for_self_false_for_dead() -> None:
    import os

    from jailbee import background

    assert background.worker_alive(os.getpid()) is True
    # PID 2**31-1 is effectively guaranteed absent on Linux.
    assert background.worker_alive(2**31 - 1) is False


def test_start_job_records_op_kind_destroy() -> None:
    from jailbee import background
    from jailbee.db.models import JOB_CREATE, JOB_DESTROY, BackgroundJob

    engine = _engine()
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        background.start_job(
            s,
            container_name="sampleapp-del",
            container_prefix="sampleapp",
            branch=None,
            pid=7,
            log_path="/l",
            now=now,
            op_kind=JOB_DESTROY,
        )
        background.start_job(
            s,
            container_name="sampleapp-make",
            container_prefix="sampleapp",
            branch="feat/x",
            pid=8,
            log_path="/l",
            now=now,
        )
        delrow = s.get(BackgroundJob, "sampleapp-del")
        makerow = s.get(BackgroundJob, "sampleapp-make")
    assert delrow is not None and delrow.op_kind == JOB_DESTROY
    assert delrow.phase == background.PHASE_STARTING
    # Default kind is create, so the existing `gie new` callers are unchanged.
    assert makerow is not None and makerow.op_kind == JOB_CREATE
    # Destroy phase constants exist.
    assert (background.PHASE_STOPPING, background.PHASE_DELETING) == ("stopping", "deleting")
    # Re-exported for callers that import from background.
    assert (background.JOB_CREATE, background.JOB_DESTROY) == ("create", "destroy")


def test_job_roundtrip_preserves_options() -> None:
    from pathlib import Path

    from jailbee import background
    from jailbee.lifecycle import NewContainerOptions

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=True,
        mirror_endpoint=("10.0.0.1", 3128),
        mirror_ca_path=Path("/tmp/ca.crt"),
        base=None,
        mount=False,
        base_branch_label="develop",
        pr=7,
        clone_commit="beefcafe1234",
    )
    job = background.op_to_job(opts, container_name="sampleapp-feat-foo", log_path="/l")
    loaded, container_name, log_path = background.job_to_opts(job)

    assert container_name == "sampleapp-feat-foo"
    assert log_path == "/l"
    assert loaded.pr == 7
    assert loaded.clone_commit == "beefcafe1234"
    assert loaded == opts


def test_clearable_true_for_failed_phase_even_with_live_worker(mocker) -> None:
    from jailbee import background

    mocker.patch.object(background, "worker_alive", return_value=True)
    assert background.clearable(background.PHASE_FAILED, 1234) is True


def test_clearable_true_for_mid_phase_with_dead_worker(mocker) -> None:
    from jailbee import background

    mocker.patch.object(background, "worker_alive", return_value=False)
    assert background.clearable(background.PHASE_CLONING, 1234) is True


def test_clearable_false_for_mid_phase_with_live_worker(mocker) -> None:
    from jailbee import background

    mocker.patch.object(background, "worker_alive", return_value=True)
    assert background.clearable(background.PHASE_CLONING, 1234) is False


def test_job_label_terminal_phase_is_the_bare_phase(mocker) -> None:
    from jailbee import background

    mocker.patch.object(background, "worker_alive", return_value=False)
    assert background.job_label(background.PHASE_FAILED, 1234) == "failed"


def test_job_label_names_the_phase_a_dead_worker_died_in(mocker) -> None:
    from jailbee import background

    mocker.patch.object(background, "worker_alive", return_value=False)
    assert background.job_label(background.PHASE_CLONING, 1234) == "cloning (worker gone)"


def test_job_label_live_worker_is_the_bare_phase(mocker) -> None:
    from jailbee import background

    mocker.patch.object(background, "worker_alive", return_value=True)
    assert background.job_label(background.PHASE_CLONING, 1234) == "cloning"


def test_job_label_live_destroy_job_in_starting_renders_destroying(mocker) -> None:
    from jailbee import background
    from jailbee.db.models import JOB_DESTROY

    mocker.patch.object(background, "worker_alive", return_value=True)
    assert background.job_label(background.PHASE_STARTING, 1234, kind=JOB_DESTROY) == "destroying"


def test_job_label_dead_destroy_job_in_starting_keeps_worker_gone_suffix(mocker) -> None:
    """A dead destroy job must not be hidden behind the friendlier name."""
    from jailbee import background
    from jailbee.db.models import JOB_DESTROY

    mocker.patch.object(background, "worker_alive", return_value=False)
    assert (
        background.job_label(background.PHASE_STARTING, 1234, kind=JOB_DESTROY)
        == "starting (worker gone)"
    )


def test_job_label_create_job_in_starting_is_unaffected_by_kind(mocker) -> None:
    from jailbee import background
    from jailbee.db.models import JOB_CREATE

    mocker.patch.object(background, "worker_alive", return_value=True)
    assert background.job_label(background.PHASE_STARTING, 1234, kind=JOB_CREATE) == "starting"


def test_job_label_or_empty_is_empty_for_no_job() -> None:
    from jailbee import background

    assert background.job_label_or_empty(None, None) == ""


def test_job_label_or_empty_returns_bare_phase_for_pidless_row() -> None:
    """Only reachable from a hand-built ContainerInfo; every populated row
    has a non-null pid."""
    from jailbee import background

    assert background.job_label_or_empty(background.PHASE_CLONING, None) == "cloning"


def test_job_label_or_empty_delegates_to_job_label(mocker) -> None:
    from jailbee import background

    mocker.patch.object(background, "worker_alive", return_value=False)
    assert background.job_label_or_empty(background.PHASE_CLONING, 1234) == "cloning (worker gone)"


def _seed(session, name: str, *, phase: str, pid: int = 1234) -> None:
    from jailbee import background

    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    background.start_job(
        session,
        container_name=name,
        container_prefix="sampleapp",
        branch=None,
        pid=pid,
        log_path="/l",
        now=now,
    )
    if phase == background.PHASE_FAILED:
        background.fail_job(session, name, "boom", now=now)
    elif phase != background.PHASE_STARTING:
        background.set_phase(session, name, phase, now=now)


def test_clear_job_deletes_a_failed_row_and_reports_failed(mocker) -> None:
    from jailbee import background
    from jailbee.db.models import BackgroundJob

    mocker.patch.object(background, "worker_alive", return_value=True)
    engine = _engine()
    with Session(engine) as s:
        _seed(s, "c", phase=background.PHASE_FAILED)
        outcome = background.clear_job(s, "c")
        assert s.get(BackgroundJob, "c") is None
    assert outcome.cleared is True
    assert outcome.reason == "failed"
    assert outcome.kind == "create"
    assert outcome.phase == background.PHASE_FAILED


def test_clear_job_deletes_a_stale_row_and_reports_the_last_phase(mocker) -> None:
    from jailbee import background
    from jailbee.db.models import BackgroundJob

    mocker.patch.object(background, "worker_alive", return_value=False)
    engine = _engine()
    with Session(engine) as s:
        _seed(s, "c", phase=background.PHASE_CLONING, pid=999)
        outcome = background.clear_job(s, "c")
        assert s.get(BackgroundJob, "c") is None
    assert outcome.cleared is True
    assert outcome.reason == "stale"
    assert outcome.phase == background.PHASE_CLONING
    assert outcome.pid == 999


def test_clear_job_refuses_a_live_job_and_keeps_the_row(mocker) -> None:
    from jailbee import background
    from jailbee.db.models import BackgroundJob

    mocker.patch.object(background, "worker_alive", return_value=True)
    engine = _engine()
    with Session(engine) as s:
        _seed(s, "c", phase=background.PHASE_CLONING, pid=4242)
        outcome = background.clear_job(s, "c")
        assert s.get(BackgroundJob, "c") is not None
    assert outcome.cleared is False
    assert outcome.reason == "running"
    assert outcome.phase == background.PHASE_CLONING
    assert outcome.pid == 4242


def test_clear_job_on_unknown_name_reports_missing() -> None:
    from jailbee import background

    engine = _engine()
    with Session(engine) as s:
        outcome = background.clear_job(s, "nope")
    assert outcome.cleared is False
    assert outcome.reason == "missing"
    assert outcome.kind is None


def test_list_all_jobs_crosses_repo_prefixes() -> None:
    from jailbee import background

    engine = _engine()
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        for prefix, name in (("alpha", "alpha-a"), ("beta", "beta-b")):
            background.start_job(
                s,
                container_name=name,
                container_prefix=prefix,
                branch=None,
                pid=1,
                log_path="/l",
                now=now,
            )
        assert set(background.list_all_jobs(s)) == {"alpha-a", "beta-b"}
        assert set(background.list_jobs(s, "alpha")) == {"alpha-a"}


def test_op_to_job_round_trip_preserves_assume_yes():
    """Guards the known trap: a field added to only one side is silently lost."""
    from jailbee.background import job_to_opts, op_to_job
    from jailbee.lifecycle import NewContainerOptions

    opts = NewContainerOptions(
        container_branch="feature",
        name=None,
        network="strict",
        memory="4GiB",
        cpu=2,
        from_base="base",
        clone=True,
        assume_yes=True,
    )

    job = op_to_job(opts, container_name="p-feature", log_path="/tmp/l.log")
    restored, _name, _log = job_to_opts(job)

    assert restored.assume_yes is True


def test_op_to_job_round_trip_preserves_every_field():
    """Structural guard: no NewContainerOptions field may be dropped."""
    import dataclasses
    from pathlib import Path

    from jailbee.background import job_to_opts, op_to_job
    from jailbee.lifecycle import NewContainerOptions

    opts = NewContainerOptions(
        container_branch="feature",
        name="p-feature",
        network="loose",
        memory="8GiB",
        cpu=4,
        from_base="base",
        clone=True,
        autostart=False,
        mirror_endpoint=("10.0.0.1", 5000),
        mirror_ca_path=Path("/tmp/ca.crt"),
        base="main",
        mount=False,
        base_branch_label="main",
        pr=42,
        untrusted_head=True,
        clone_commit="a" * 40,
        assume_yes=True,
        # Non-default values on purpose: a dropped field whose default happens
        # to equal the value would sail through the loop below.
        approved_autostart_ref="refs/heads/feature",
        autofetch_done=True,
    )

    job = op_to_job(opts, container_name="p-feature", log_path="/tmp/l.log")
    restored, _name, _log = job_to_opts(job)

    for f in dataclasses.fields(NewContainerOptions):
        assert getattr(restored, f.name) == getattr(opts, f.name), f.name


def test_job_to_opts_tolerates_a_job_file_without_assume_yes():
    """A job written by an older gie must still load after upgrade."""
    from jailbee.background import job_to_opts, op_to_job
    from jailbee.lifecycle import NewContainerOptions

    opts = NewContainerOptions(
        container_branch="feature",
        name=None,
        network="strict",
        memory="4GiB",
        cpu=2,
        from_base="base",
        clone=True,
    )
    job = op_to_job(opts, container_name="p-feature", log_path="/tmp/l.log")
    del job["opts"]["assume_yes"]
    del job["opts"]["approved_autostart_ref"]
    del job["opts"]["untrusted_head"]
    del job["opts"]["autofetch_done"]

    restored, _name, _log = job_to_opts(job)

    assert restored.assume_yes is False
    assert restored.approved_autostart_ref is None
    assert restored.untrusted_head is False
    # False, so that worker does its own fetch: the gie that wrote this job file
    # had no foreground pre-flight to do it.
    assert restored.autofetch_done is False
