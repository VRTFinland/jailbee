"""CLI tests for the `jailbee job` group (ls / log / clear)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session
from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import make_cfg

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _cfg(tmp_path: Path, mocker, prefix: str = "myrepo"):
    repo = tmp_path / prefix
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", prefix)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    return cfg


def _seed(name: str, *, prefix: str = "myrepo", phase: str, pid: int = 4242, log_path: str = "/l"):
    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix=prefix,
            branch=None,
            pid=pid,
            log_path=log_path,
            now=NOW,
        )
        if phase == background.PHASE_FAILED:
            background.fail_job(s, name, "autostart step 'deps' failed", now=NOW)
        elif phase != background.PHASE_STARTING:
            background.set_phase(s, name, phase, now=NOW)


def _row_exists(name: str) -> bool:
    """Read the job table directly — every test gets a fresh state DB from the
    autouse `_isolate_state_dir` fixture (tests/conftest.py:81), which makes a
    numbered tmp dir per test, so rows never leak between tests."""
    from jailbee.db import get_engine
    from jailbee.db.models import BackgroundJob

    with Session(get_engine()) as s:
        return s.get(BackgroundJob, name) is not None


def _out(result) -> str:
    return result.stdout + (result.stderr or "")


def test_job_ls_lists_this_repos_jobs_with_the_error(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-feat-x", phase=background.PHASE_FAILED)

    result = CliRunner().invoke(app, ["job", "ls"])

    assert result.exit_code == 0, _out(result)
    assert "feat-x" in result.stdout
    assert "failed" in result.stdout
    assert "autostart step" in result.stdout


def test_job_ls_empty_prints_a_message(tmp_path, mocker) -> None:
    _cfg(tmp_path, mocker)

    result = CliRunner().invoke(app, ["job", "ls"])

    assert result.exit_code == 0, _out(result)
    assert "no background jobs" in result.stdout.lower()


def test_job_ls_hides_other_repos_unless_all_repos(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("other-feat-y", prefix="other", phase=background.PHASE_FAILED)

    scoped = CliRunner().invoke(app, ["job", "ls"])
    assert "feat-y" not in scoped.stdout

    everything = CliRunner().invoke(app, ["job", "ls", "--all-repos"])
    assert everything.exit_code == 0, _out(everything)
    assert "other-feat-y" in everything.stdout


def test_job_ls_json_emits_the_untruncated_error(tmp_path, mocker) -> None:
    import json

    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-feat-x", phase=background.PHASE_FAILED)

    result = CliRunner().invoke(app, ["job", "ls", "-o", "json"])

    assert result.exit_code == 0, _out(result)
    payload = json.loads(result.stdout)
    assert payload[0]["error"] == "autostart step 'deps' failed"


def test_job_clear_removes_a_failed_row_without_touching_the_container(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-feat-x", phase=background.PHASE_FAILED)
    destroy = mocker.patch("jailbee.lifecycle.destroy_container")

    result = CliRunner().invoke(app, ["job", "clear", "feat-x"])

    assert result.exit_code == 0, _out(result)
    assert "Cleared failed create job for 'feat-x'" in result.stdout
    assert "container untouched" in result.stdout
    assert not _row_exists("myrepo-feat-x")
    destroy.assert_not_called()


def test_job_clear_works_when_no_container_ever_existed(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-ghost", phase=background.PHASE_FAILED)
    incus_cls = mocker.patch("jailbee.incus.Incus")
    incus_cls.return_value.exists.return_value = False

    result = CliRunner().invoke(app, ["job", "clear", "ghost"])

    assert result.exit_code == 0, _out(result)
    assert not _row_exists("myrepo-ghost")


def test_job_clear_reports_the_last_phase_for_a_dead_worker(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-feat-y", phase=background.PHASE_CLONING, pid=999)
    mocker.patch.object(background, "worker_alive", return_value=False)

    result = CliRunner().invoke(app, ["job", "clear", "feat-y"])

    assert result.exit_code == 0, _out(result)
    assert "stale create job" in result.stdout
    assert "cloning" in result.stdout
    assert not _row_exists("myrepo-feat-y")


def test_job_clear_refuses_a_live_job_and_points_at_the_log(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-feat-z", phase=background.PHASE_CLONING, pid=1234)
    mocker.patch.object(background, "worker_alive", return_value=True)

    result = CliRunner().invoke(app, ["job", "clear", "feat-z"])

    assert result.exit_code == 1
    combined = _out(result)
    assert "still running" in combined
    assert "phase=cloning" in combined
    assert "pid 1234" in combined
    assert "jailbee job log feat-z --follow" in combined
    assert _row_exists("myrepo-feat-z")


def test_job_clear_unknown_name_exits_one(tmp_path, mocker) -> None:
    _cfg(tmp_path, mocker)

    result = CliRunner().invoke(app, ["job", "clear", "nope"])

    assert result.exit_code == 1
    assert "no background job for 'nope'" in _out(result)


def test_job_clear_reports_missing_when_row_vanishes_between_lookup_and_clear(
    tmp_path, mocker
) -> None:
    """TOCTOU: lookup_background_job finds the row, but clear_job's own read
    (a moment later) does not — e.g. a worker's delete_job or `jailbee destroy`
    raced it. clear_job then returns reason="missing", and the CLI must
    report the same "no background job for" message as the up-front check,
    not the "still running" message meant for reason="running"."""
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-feat-x", phase=background.PHASE_FAILED)
    mocker.patch.object(
        background,
        "clear_job",
        return_value=background.ClearOutcome(cleared=False, reason="missing"),
    )

    result = CliRunner().invoke(app, ["job", "clear", "feat-x"])

    assert result.exit_code == 1
    combined = _out(result)
    assert "no background job for 'feat-x'" in combined
    assert "still running" not in combined


def test_job_clear_all_clears_dead_and_skips_live(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-dead", phase=background.PHASE_FAILED)
    _seed("myrepo-live", phase=background.PHASE_CLONING, pid=1234)
    mocker.patch.object(background, "worker_alive", side_effect=lambda pid: pid == 1234)

    result = CliRunner().invoke(app, ["job", "clear", "--all"])

    assert result.exit_code == 0, _out(result)
    assert not _row_exists("myrepo-dead")
    assert _row_exists("myrepo-live")
    assert "still running" in _out(result)


def test_job_clear_name_and_all_are_mutually_exclusive(tmp_path, mocker) -> None:
    _cfg(tmp_path, mocker)

    result = CliRunner().invoke(app, ["job", "clear", "feat-x", "--all"])

    assert result.exit_code == 2
    assert "mutually exclusive" in _out(result)


def test_job_clear_without_name_or_all_lists_candidates(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    _seed("myrepo-feat-x", phase=background.PHASE_FAILED)

    result = CliRunner().invoke(app, ["job", "clear"])

    assert result.exit_code == 1
    combined = _out(result)
    assert "--all" in combined
    assert "feat-x" in combined


def test_job_log_prints_the_workers_log(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    log = tmp_path / "worker.log"
    log.write_text("cloning repo\ndone\n")
    _seed("myrepo-feat-x", phase=background.PHASE_FAILED, log_path=str(log))

    result = CliRunner().invoke(app, ["job", "log", "feat-x"])

    assert result.exit_code == 0, _out(result)
    assert "cloning repo" in result.stdout


def test_job_log_missing_file_names_the_path(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    missing = tmp_path / "gone.log"
    _seed("myrepo-feat-x", phase=background.PHASE_FAILED, log_path=str(missing))

    result = CliRunner().invoke(app, ["job", "log", "feat-x"])

    assert result.exit_code == 1
    assert str(missing) in _out(result)


def test_job_log_unknown_name_exits_one(tmp_path, mocker) -> None:
    _cfg(tmp_path, mocker)

    result = CliRunner().invoke(app, ["job", "log", "nope"])

    assert result.exit_code == 1
    assert "no background job for 'nope'" in _out(result)


def test_job_log_follow_delegates_to_follow_log(tmp_path, mocker) -> None:
    from jailbee import background

    _cfg(tmp_path, mocker)
    log = tmp_path / "worker.log"
    log.write_text("x\n")
    _seed("myrepo-feat-x", phase=background.PHASE_FAILED, log_path=str(log))
    follow = mocker.patch("jailbee.jobs.follow_log")

    result = CliRunner().invoke(app, ["job", "log", "feat-x", "--follow"])

    assert result.exit_code == 0, _out(result)
    follow.assert_called_once()
    assert follow.call_args.args[0] == log
