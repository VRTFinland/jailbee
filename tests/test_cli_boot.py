"""CLI tests for background `jailbee start` / `jailbee restart`.

The foreground behaviour of both commands lives in test_cli_restart.py and
test_cli.py; this file covers the detached path — flag resolution, the
spawned worker's argv and job row, the in-flight guard, and the worker
command itself — plus what a *foreground* boot does to a job row left behind
by an earlier background one.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session
from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import make_cfg

runner = CliRunner()


def _setup(tmp_path: Path, mocker, *, background: bool = False):
    """Wire up a cfg with container_prefix='myrepo' and a resolved container."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    if background:
        object.__setattr__(cfg.boot, "background", True)

    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-a"))
    boot = mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.cli._post_start_actions")
    return cfg, boot


def _patch_popen(mocker, pid: int = 5555):
    popen = mocker.patch("jailbee.cli.subprocess.Popen")
    popen.return_value = mocker.MagicMock(pid=pid)
    return popen


def _jobs():
    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        return background.list_jobs(s, "myrepo")


def _insert_job(pid: int, phase: str, kind: str | None = None):
    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_BOOT

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-feat-a",
            container_prefix="myrepo",
            branch=None,
            pid=pid,
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=kind or JOB_BOOT,
        )
        background.set_phase(s, "myrepo-feat-a", phase, now=datetime.now(UTC))


DEAD_PID = 2**22 - 1  # above /proc/sys/kernel/pid_max: never a live process


# ---- spawn side


def test_restart_background_spawns_worker_and_returns(tmp_path, mocker):
    from jailbee import background
    from jailbee.db.models import JOB_BOOT

    _cfg, boot = _setup(tmp_path, mocker)
    popen = _patch_popen(mocker)

    result = runner.invoke(app, ["restart", "feat-a", "--background"])

    assert result.exit_code == 0, result.output
    boot.assert_not_called()
    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_boot-worker"]
    assert argv[argv.index("--name") + 1] == "myrepo-feat-a"
    assert "--restart" in argv

    row = _jobs()["myrepo-feat-a"]
    assert row.pid == 5555
    assert row.op_kind == JOB_BOOT
    assert row.phase == background.PHASE_STARTING


def test_start_background_spawns_worker_without_restart_flag(tmp_path, mocker):
    """`jailbee start` must not reboot a running container just because it
    went through the shared worker."""
    _setup(tmp_path, mocker)
    popen = _patch_popen(mocker)

    result = runner.invoke(app, ["start", "feat-a", "--background"])

    assert result.exit_code == 0, result.output
    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_boot-worker"]
    assert "--restart" not in argv


def test_background_forwards_no_autostart_to_the_worker(tmp_path, mocker):
    _setup(tmp_path, mocker)
    popen = _patch_popen(mocker)

    result = runner.invoke(app, ["restart", "feat-a", "--background", "--no-autostart"])

    assert result.exit_code == 0, result.output
    assert "--no-autostart" in popen.call_args.args[0]


def test_restart_defaults_to_foreground(tmp_path, mocker):
    _cfg, boot = _setup(tmp_path, mocker)
    popen = _patch_popen(mocker)

    result = runner.invoke(app, ["restart", "feat-a"])

    assert result.exit_code == 0, result.output
    popen.assert_not_called()
    boot.assert_called_once()


def test_restart_honours_boot_background_config(tmp_path, mocker):
    _cfg, boot = _setup(tmp_path, mocker, background=True)
    popen = _patch_popen(mocker)

    result = runner.invoke(app, ["restart", "feat-a"])

    assert result.exit_code == 0, result.output
    popen.assert_called_once()
    boot.assert_not_called()


def test_start_honours_boot_background_config(tmp_path, mocker):
    """The one key covers both commands."""
    _cfg, boot = _setup(tmp_path, mocker, background=True)
    popen = _patch_popen(mocker)

    result = runner.invoke(app, ["start", "feat-a"])

    assert result.exit_code == 0, result.output
    popen.assert_called_once()
    boot.assert_not_called()


def test_no_background_overrides_the_config(tmp_path, mocker):
    _cfg, boot = _setup(tmp_path, mocker, background=True)
    popen = _patch_popen(mocker)

    result = runner.invoke(app, ["restart", "feat-a", "--no-background"])

    assert result.exit_code == 0, result.output
    popen.assert_not_called()
    boot.assert_called_once()


def test_background_and_no_background_mutually_exclusive(tmp_path, mocker):
    _setup(tmp_path, mocker)

    result = runner.invoke(app, ["restart", "feat-a", "--background", "--no-background"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_background_boot_refuses_over_a_live_job(tmp_path, mocker):
    """Two workers rebooting one container would interleave their autostart
    steps, so a live job blocks the second spawn instead of replacing it."""
    _setup(tmp_path, mocker)
    popen = _patch_popen(mocker)
    _insert_job(os.getpid(), "autostart")

    result = runner.invoke(app, ["restart", "feat-a", "--background"])

    assert result.exit_code == 1
    popen.assert_not_called()
    assert _jobs()["myrepo-feat-a"].pid == os.getpid()


def test_background_boot_replaces_a_dead_job(tmp_path, mocker):
    """A job whose worker is gone is a leftover, not a conflict."""
    _setup(tmp_path, mocker)
    popen = _patch_popen(mocker)
    _insert_job(DEAD_PID, "failed")

    result = runner.invoke(app, ["restart", "feat-a", "--background"])

    assert result.exit_code == 0, result.output
    popen.assert_called_once()
    assert _jobs()["myrepo-feat-a"].pid == 5555


# ---- foreground side: what a successful boot does to a leftover row


def test_foreground_restart_clears_a_failed_boot_job(tmp_path, mocker):
    """A successful restart supersedes the failed boot the row describes, so
    `jailbee ls` must stop flagging the container without a `job clear`."""
    _setup(tmp_path, mocker)
    _insert_job(DEAD_PID, "failed")

    result = runner.invoke(app, ["restart", "feat-a"])

    assert result.exit_code == 0, result.output
    assert _jobs() == {}
    assert "failed boot job" in result.output


def test_foreground_start_clears_a_stale_boot_job(tmp_path, mocker):
    """`start` shares the clearing, and a worker that vanished mid-phase is
    just as much a leftover as an explicitly failed one."""
    _setup(tmp_path, mocker)
    _insert_job(DEAD_PID, "autostart")

    result = runner.invoke(app, ["start", "feat-a"])

    assert result.exit_code == 0, result.output
    assert _jobs() == {}
    assert "stale boot job" in result.output


def test_foreground_restart_keeps_a_failed_create_job(tmp_path, mocker):
    """A failed create means the container's setup (clone, credentials, first
    autostart) never finished — a reboot doesn't complete it, so the row must
    survive to keep saying so."""
    from jailbee.db.models import JOB_CREATE

    _setup(tmp_path, mocker)
    _insert_job(DEAD_PID, "failed", JOB_CREATE)

    result = runner.invoke(app, ["restart", "feat-a"])

    assert result.exit_code == 0, result.output
    assert _jobs()["myrepo-feat-a"].phase == "failed"


def test_foreground_restart_leaves_a_live_boot_job_alone(tmp_path, mocker):
    """A live worker is still writing to the container and would be orphaned
    by a clear, so the guarded path leaves its row in place."""
    _setup(tmp_path, mocker)
    _insert_job(os.getpid(), "autostart")

    result = runner.invoke(app, ["restart", "feat-a"])

    assert result.exit_code == 0, result.output
    assert _jobs()["myrepo-feat-a"].pid == os.getpid()


def test_foreground_restart_keeps_the_row_when_autostart_fails(tmp_path, mocker):
    """Only a boot that got all the way through clears the flag."""
    import typer

    _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._post_start_actions", side_effect=typer.Exit(1))
    _insert_job(DEAD_PID, "failed")

    result = runner.invoke(app, ["restart", "feat-a"])

    assert result.exit_code == 1
    assert _jobs()["myrepo-feat-a"].phase == "failed"


# ---- worker side


def _worker_setup(tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    return cfg


def test_boot_worker_boots_then_runs_post_start_and_clears_the_job(tmp_path, mocker):
    _worker_setup(tmp_path, mocker)
    boot = mocker.patch("jailbee.lifecycle.boot_container")
    post = mocker.patch("jailbee.cli._post_start_actions")
    _insert_job(os.getpid(), "starting")

    result = runner.invoke(app, ["_boot-worker", "--name", "myrepo-feat-a", "--restart"])

    assert result.exit_code == 0, result.output
    assert boot.call_args.kwargs["restart"] is True
    assert post.call_args.kwargs["no_autostart"] is False
    assert _jobs() == {}


def test_boot_worker_without_restart_starts_the_container(tmp_path, mocker):
    _worker_setup(tmp_path, mocker)
    boot = mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.cli._post_start_actions")
    _insert_job(os.getpid(), "starting")

    result = runner.invoke(app, ["_boot-worker", "--name", "myrepo-feat-a"])

    assert result.exit_code == 0, result.output
    assert boot.call_args.kwargs["restart"] is False


def test_boot_worker_records_the_autostart_phase(tmp_path, mocker):
    """The phase flips before autostart runs, so `jailbee ls` shows where the
    job is and an attach can go in as soon as the container is up."""
    _worker_setup(tmp_path, mocker)
    mocker.patch("jailbee.lifecycle.boot_container")
    seen: list[str] = []
    mocker.patch(
        "jailbee.cli._post_start_actions",
        side_effect=lambda *a, **kw: seen.append(_jobs()["myrepo-feat-a"].phase),
    )
    _insert_job(os.getpid(), "starting")

    result = runner.invoke(app, ["_boot-worker", "--name", "myrepo-feat-a", "--restart"])

    assert result.exit_code == 0, result.output
    assert seen == ["autostart"]


def test_boot_worker_stays_in_starting_phase_with_no_autostart(tmp_path, mocker):
    """With --no-autostart there is no autostart phase to report."""
    _worker_setup(tmp_path, mocker)
    mocker.patch("jailbee.lifecycle.boot_container")
    seen: list[str] = []
    mocker.patch(
        "jailbee.cli._post_start_actions",
        side_effect=lambda *a, **kw: seen.append(_jobs()["myrepo-feat-a"].phase),
    )
    _insert_job(os.getpid(), "starting")

    result = runner.invoke(
        app, ["_boot-worker", "--name", "myrepo-feat-a", "--restart", "--no-autostart"]
    )

    assert result.exit_code == 0, result.output
    assert seen == ["starting"]


def test_boot_worker_records_a_message_when_autostart_exits(tmp_path, mocker):
    """`_post_start_actions` reports an autostart failure itself and raises
    `typer.Exit`, whose str() is empty — the row must still say something
    better than 'unknown error'.
    """
    import typer

    _worker_setup(tmp_path, mocker)
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.cli._post_start_actions", side_effect=typer.Exit(1))
    _insert_job(os.getpid(), "starting")

    result = runner.invoke(app, ["_boot-worker", "--name", "myrepo-feat-a", "--restart"])

    assert result.exit_code == 1
    row = _jobs()["myrepo-feat-a"]
    assert row.phase == "failed"
    assert row.error_msg
    assert "log" in row.error_msg


def test_boot_worker_marks_the_job_failed_on_error(tmp_path, mocker):
    _worker_setup(tmp_path, mocker)
    mocker.patch("jailbee.lifecycle.boot_container", side_effect=RuntimeError("boom"))
    post = mocker.patch("jailbee.cli._post_start_actions")
    _insert_job(os.getpid(), "starting")

    result = runner.invoke(app, ["_boot-worker", "--name", "myrepo-feat-a", "--restart"])

    assert result.exit_code == 1
    post.assert_not_called()
    row = _jobs()["myrepo-feat-a"]
    assert row.phase == "failed"
    assert row.error_msg == "boom"
