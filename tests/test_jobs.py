"""Tests for jobs.py — presentation helpers for `gie job` (no DB, no subprocess)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _job(**over):
    from jailbee.db.models import BackgroundJob

    kwargs = {
        "container_name": "myrepo-feat-x",
        "container_prefix": "myrepo",
        "branch": "feat/x",
        "phase": "failed",
        "pid": 4242,
        "log_path": "/logs/myrepo-feat-x.log",
        "error_msg": "boom",
        "op_kind": "create",
        "started_at": NOW - timedelta(minutes=12),
        "updated_at": NOW,
    }
    kwargs.update(over)
    return BackgroundJob(**kwargs)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=45), "45s"),
        (timedelta(minutes=12), "12m"),
        (timedelta(hours=3), "3h"),
        (timedelta(days=2), "2d"),
        (timedelta(seconds=-5), "0s"),  # clock skew must not render negatives
    ],
)
def test_format_age(delta, expected) -> None:
    from jailbee import jobs

    assert jobs.format_age(NOW - delta, NOW) == expected


def test_truncate_error_placeholder_for_none() -> None:
    from jailbee import jobs

    assert jobs.truncate_error(None) == "—"
    assert jobs.truncate_error("") == "—"


def test_truncate_error_collapses_newlines_and_shortens() -> None:
    from jailbee import jobs

    assert jobs.truncate_error("step failed\n  see log") == "step failed see log"
    long = "x" * 100
    out = jobs.truncate_error(long, limit=10)
    assert out == "x" * 9 + "…"


def test_job_field_specs_default_table_hides_repo_for_one_repo() -> None:
    from jailbee import jobs

    names = [f.name for f in jobs.job_field_specs(now=NOW, all_repos=False) if f.default_table]
    assert names == ["name", "kind", "phase", "pid", "age", "error", "log"]


def test_job_field_specs_all_repos_shows_repo_and_full_names() -> None:
    from jailbee import jobs

    specs = {f.name: f for f in jobs.job_field_specs(now=NOW, all_repos=True)}
    assert specs["repo"].default_table is True
    assert specs["name"].cell(_job()) == "myrepo-feat-x"


def test_job_field_specs_name_is_short_within_one_repo() -> None:
    from jailbee import jobs

    specs = {f.name: f for f in jobs.job_field_specs(now=NOW, all_repos=False)}
    assert specs["name"].cell(_job()) == "feat-x"


def test_job_field_specs_phase_cell_marks_a_dead_worker(mocker) -> None:
    from jailbee import background, jobs

    mocker.patch.object(background, "worker_alive", return_value=False)
    specs = {f.name: f for f in jobs.job_field_specs(now=NOW, all_repos=False)}
    assert "worker gone" in specs["phase"].cell(_job(phase="cloning"))
    # A failed row is labelled by its phase, not by the worker probe.
    assert "worker gone" not in specs["phase"].cell(_job(phase="failed"))


def test_job_field_specs_phase_cell_shows_destroying_for_live_destroy_job(mocker) -> None:
    """A live `destroy`-kind job in `starting` reads 'destroying' here too,
    matching `gie ls`'s JOB column (lifecycle._job_cell) exactly."""
    from jailbee import background, jobs
    from jailbee.db.models import JOB_DESTROY

    mocker.patch.object(background, "worker_alive", return_value=True)
    specs = {f.name: f for f in jobs.job_field_specs(now=NOW, all_repos=False)}
    cell = specs["phase"].cell(_job(phase="starting", op_kind=JOB_DESTROY))
    assert "destroying" in cell
    assert "[yellow]" in cell  # alive: not the dead/red styling


def test_job_field_specs_phase_cell_dead_destroy_job_keeps_worker_gone(mocker) -> None:
    """A dead destroy job must not be hidden behind the friendlier name."""
    from jailbee import background, jobs
    from jailbee.db.models import JOB_DESTROY

    mocker.patch.object(background, "worker_alive", return_value=False)
    specs = {f.name: f for f in jobs.job_field_specs(now=NOW, all_repos=False)}
    cell = specs["phase"].cell(_job(phase="starting", op_kind=JOB_DESTROY))
    assert "starting (worker gone)" in cell
    assert "[red]" in cell


def test_job_field_specs_json_keeps_the_full_error_and_iso_timestamp() -> None:
    from jailbee import jobs

    specs = {f.name: f for f in jobs.job_field_specs(now=NOW, all_repos=False)}
    job = _job(error_msg="a" * 200)
    assert specs["error"].json(job) == "a" * 200
    assert specs["age"].json(job) == job.started_at.isoformat()


def test_follow_log_writes_existing_content_then_appended_content(tmp_path: Path) -> None:
    from jailbee import jobs

    log = tmp_path / "job.log"
    log.write_text("first\n")
    written: list[str] = []
    appended = {"done": False}

    def fake_sleep(_seconds: float) -> None:
        if appended["done"]:
            raise KeyboardInterrupt
        with log.open("a") as fh:
            fh.write("second\n")
        appended["done"] = True

    jobs.follow_log(log, write=written.append, sleep=fake_sleep)

    assert "".join(written) == "first\nsecond\n"


def test_follow_log_returns_on_keyboard_interrupt(tmp_path: Path) -> None:
    from jailbee import jobs

    log = tmp_path / "job.log"
    log.write_text("only\n")

    def fake_sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    written: list[str] = []
    jobs.follow_log(log, write=written.append, sleep=fake_sleep)  # must not raise
    assert written == ["only\n"]
