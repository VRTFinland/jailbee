"""`jailbee autostart status|cancel`, and the stop/net guards around them."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session
from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import make_cfg

runner = CliRunner()

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
DEAD_PID = 999999


def _setup(tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-a"))
    return cfg, incus


def _insert_job(pid: int, phase: str, log_path: str, *, kind: str | None = None) -> None:
    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_AUTOSTART

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-feat-a",
            container_prefix="myrepo",
            branch=None,
            pid=pid,
            log_path=log_path,
            now=NOW,
            op_kind=kind or JOB_AUTOSTART,
        )
        background.set_phase(s, "myrepo-feat-a", phase, now=NOW)


def _write_progress(path: Path, *entries: tuple[str, str, str]) -> None:
    from jailbee.autostart_progress import ProgressEntry, append

    for i, (stage, step, state) in enumerate(entries):
        append(path, ProgressEntry(stage=stage, step=step, state=state, at=f"10:0{i}"))


def _point_at(mocker, path: Path) -> None:
    mocker.patch("jailbee.cli._autostart_progress_path", return_value=path)


def _out(result) -> str:
    return result.stdout + (result.stderr or "")


# --- status ---------------------------------------------------------------


def test_status_groups_the_steps_by_stage_under_a_phase_and_pid_header(tmp_path, mocker) -> None:
    """The header names the running stage and its pid; the rows name the steps.

    Mutation: drop the pid or the phase from the header (the whole header line
    is asserted, so neither survives), or stop naming a stage once per group,
    and this fails.
    """
    _setup(tmp_path, mocker)
    progress = tmp_path / "p.json"
    _write_progress(
        progress,
        ("deps", "uv-sync", "start"),
        ("deps", "uv-sync", "ok"),
        ("deps", "npm-ci", "start"),
        ("deps", "npm-ci", "ok"),
        ("services", "docker-up", "start"),
    )
    _insert_job(os.getpid(), "services", str(tmp_path / "x.log"))
    _point_at(mocker, progress)

    result = runner.invoke(app, ["autostart", "status", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert f"Autostart for 'feat-a': autostart:services (pid {os.getpid()})" in out
    assert "uv-sync" in out
    assert "npm-ci" in out
    assert "docker-up" in out
    # Grouped: the stage is named once, on the first of its steps. ("services"
    # is in the header too, so "deps" is the one that counts here.)
    assert out.count("deps") == 1


def test_status_calls_an_unterminated_step_running_while_the_worker_lives(
    tmp_path, mocker
) -> None:
    """A `start` with no `ok`/`fail` is genuinely in flight under a live worker.

    Mutation: render every dangling `start` as interrupted and this fails.
    """
    _setup(tmp_path, mocker)
    progress = tmp_path / "p.json"
    _write_progress(progress, ("deps", "uv-sync", "start"))
    _insert_job(os.getpid(), "deps", str(tmp_path / "x.log"))
    _point_at(mocker, progress)

    result = runner.invoke(app, ["autostart", "status", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert "running" in out
    assert "interrupted" not in out


def test_status_calls_an_unterminated_step_interrupted_when_the_worker_is_gone(
    tmp_path, mocker
) -> None:
    """The executor's abort path leaves a `start` dangling forever, so a dead
    worker's unfinished step must not read as still running.

    Mutation: decide the step's state from the entries alone (ignoring
    liveness) and this fails.
    """
    _setup(tmp_path, mocker)
    progress = tmp_path / "p.json"
    _write_progress(progress, ("deps", "uv-sync", "start"))
    _insert_job(DEAD_PID, "deps", str(tmp_path / "x.log"))
    mocker.patch("jailbee.background.worker_alive", return_value=False)
    _point_at(mocker, progress)

    result = runner.invoke(app, ["autostart", "status", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert "interrupted" in out
    assert "running" not in out


def test_status_reports_a_finished_run_step_by_step(tmp_path, mocker) -> None:
    """Every step terminal: each keeps the result it recorded, live or not.

    Mutation: map `fail` to anything but `failed` — or let liveness override a
    terminal state — and this fails. The row's phase is deliberately *not*
    `failed` here: `job_label` renders the phase into the header verbatim, so
    a terminal phase would let the assertion pass off the header alone.
    """
    _setup(tmp_path, mocker)
    progress = tmp_path / "p.json"
    _write_progress(
        progress,
        ("deps", "uv-sync", "start"),
        ("deps", "uv-sync", "ok"),
        ("deps", "npm-ci", "start"),
        ("deps", "npm-ci", "fail"),
    )
    _insert_job(DEAD_PID, "deps", str(tmp_path / "x.log"))
    mocker.patch("jailbee.background.worker_alive", return_value=False)
    _point_at(mocker, progress)

    result = runner.invoke(app, ["autostart", "status", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert "ok" in out
    assert "failed" in out
    assert "running" not in out
    assert "interrupted" not in out


def test_status_without_a_job_row_says_so(tmp_path, mocker) -> None:
    """Mutation: exit 1 (or raise on the missing row) and this fails."""
    _setup(tmp_path, mocker)

    result = runner.invoke(app, ["autostart", "status", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert "no autostart job" in out.lower()


def test_status_ignores_a_job_row_of_another_kind(tmp_path, mocker) -> None:
    """A `new --background` worker before its detach boundary owns a `create`
    row and has run no stage.

    Mutation: key the lookup on "a row exists" and this fails.
    """
    from jailbee.db.models import JOB_CREATE

    _setup(tmp_path, mocker)
    _insert_job(os.getpid(), "creating", str(tmp_path / "x.log"), kind=JOB_CREATE)

    result = runner.invoke(app, ["autostart", "status", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert "no autostart job" in out.lower()


def test_status_with_no_progress_file_yet_says_nothing_is_recorded(tmp_path, mocker) -> None:
    """A supervisor that has not reached its first step yet.

    Mutation: let the missing file raise instead of reading as empty.
    """
    _setup(tmp_path, mocker)
    _insert_job(os.getpid(), "deps", str(tmp_path / "x.log"))
    _point_at(mocker, tmp_path / "absent.json")

    result = runner.invoke(app, ["autostart", "status", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert "no steps" in out.lower()


# --- cancel ---------------------------------------------------------------


def test_cancel_signals_the_worker(tmp_path, mocker) -> None:
    """Mutation: drop the signal (report only) and this fails."""
    _setup(tmp_path, mocker)
    _insert_job(os.getpid(), "deps", str(tmp_path / "x.log"))
    sig = mocker.patch("jailbee.autostart_status.signal_worker")

    result = runner.invoke(app, ["autostart", "cancel", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    sig.assert_called_once_with(os.getpid())


def test_cancel_refuses_when_the_worker_is_already_gone(tmp_path, mocker) -> None:
    """Mutation: signal unconditionally and this fails — the pid may have been
    recycled, and the user needs `jailbee job clear`, not a kill."""
    _setup(tmp_path, mocker)
    _insert_job(DEAD_PID, "deps", str(tmp_path / "x.log"))
    mocker.patch("jailbee.background.worker_alive", return_value=False)
    sig = mocker.patch("jailbee.autostart_status.signal_worker")

    result = runner.invoke(app, ["autostart", "cancel", "feat-a"])

    out = _out(result)
    assert result.exit_code == 1
    assert "jailbee job clear" in out
    sig.assert_not_called()


def test_cancel_without_an_autostart_job_errors(tmp_path, mocker) -> None:
    """Mutation: exit 0 on a missing row and this fails — there was nothing to
    cancel, and a script must be able to tell."""
    _setup(tmp_path, mocker)
    sig = mocker.patch("jailbee.autostart_status.signal_worker")

    result = runner.invoke(app, ["autostart", "cancel", "feat-a"])

    out = _out(result)
    assert result.exit_code == 1
    assert "no autostart job" in out.lower()
    sig.assert_not_called()


# --- the guards -----------------------------------------------------------


def test_stop_refuses_while_a_supervisor_is_live(tmp_path, mocker) -> None:
    """Stopping the container would kill the stages mid-step.

    Mutation: drop the guard and `incus.stop` is called.
    """
    _, incus = _setup(tmp_path, mocker)
    _insert_job(os.getpid(), "deps", str(tmp_path / "x.log"))

    result = runner.invoke(app, ["stop", "feat-a"])

    out = _out(result)
    assert result.exit_code == 1
    assert "jailbee autostart cancel" in out
    incus.stop.assert_not_called()


def test_stop_force_skips_the_guard(tmp_path, mocker) -> None:
    """`--force` is the lever for getting a container down now; the guard is
    advice for the clean path, not a lock.

    Mutation: guard both paths and this fails.
    """
    _, incus = _setup(tmp_path, mocker)
    _insert_job(os.getpid(), "deps", str(tmp_path / "x.log"))

    result = runner.invoke(app, ["stop", "--force", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    incus.stop.assert_called_once_with("myrepo-feat-a", force=True)


def test_stop_proceeds_when_the_autostart_worker_is_gone(tmp_path, mocker) -> None:
    """A row whose worker died blocks nothing.

    Mutation: key the guard on "an autostart row exists" and this fails.
    """
    _, incus = _setup(tmp_path, mocker)
    _insert_job(DEAD_PID, "deps", str(tmp_path / "x.log"))
    mocker.patch("jailbee.background.worker_alive", return_value=False)

    result = runner.invoke(app, ["stop", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    incus.stop.assert_called_once()


def test_stop_proceeds_with_a_live_job_of_another_kind(tmp_path, mocker) -> None:
    """The guard is about autostart stages, not about background jobs at large.

    Mutation: key the guard on "a live row exists" and this fails.
    """
    from jailbee.db.models import JOB_BOOT

    _, incus = _setup(tmp_path, mocker)
    _insert_job(os.getpid(), "starting", str(tmp_path / "x.log"), kind=JOB_BOOT)

    result = runner.invoke(app, ["stop", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    incus.stop.assert_called_once()


def test_net_warns_but_proceeds_while_a_supervisor_is_live(tmp_path, mocker) -> None:
    """The detached stage's restore is compare-and-swap, so the user's choice
    survives it — warn, don't refuse.

    Mutation: refuse (as `stop` does) and this fails.
    """
    _setup(tmp_path, mocker)
    _insert_job(os.getpid(), "deps", str(tmp_path / "x.log"))
    switch = mocker.patch("jailbee.lifecycle.switch_network")

    result = runner.invoke(app, ["net", "loose", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert "autostart" in out.lower()
    assert switch.call_count == 1


def test_net_is_quiet_when_no_autostart_run_is_in_flight(tmp_path, mocker) -> None:
    """Mutation: warn unconditionally and this fails."""
    _setup(tmp_path, mocker)
    switch = mocker.patch("jailbee.lifecycle.switch_network")

    result = runner.invoke(app, ["net", "loose", "feat-a"])

    out = _out(result)
    assert result.exit_code == 0, out
    assert "autostart" not in out.lower()
    assert switch.call_count == 1
