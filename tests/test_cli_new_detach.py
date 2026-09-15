"""The foreground commands hand their deferred autostart stages over.

`new`, `start` and `restart` each run the blocking stages themselves and
then spawn `_autostart-worker` for the rest. This file covers that hand-off
and the `--wait` / `--no-wait` overrides; the supervisor itself is
test_autostart_supervisor.py's and test_cli_boot.py's business.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jailbee.autostart_plan import AutostartPlan
from jailbee.cli import app
from tests.conftest import make_cfg

runner = CliRunner()


def _stage(name: str):
    from jailbee.config import AutostartStage

    return AutostartStage(stage=name, steps=[{"name": "s", "run": "true"}])  # type: ignore[arg-type]


# ---- `jailbee new`


def _setup_new(tmp_path: Path, mocker):
    """A `jailbee new --mount` run with everything but the autostart wiring stubbed."""
    from jailbee.egress_pool import RefreshResult

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, after_new="none")
    object.__setattr__(cfg, "container_prefix", "myrepo")

    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_config_path_or_none", return_value=None)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=mocker.Mock(docker_registry_mirror=mocker.Mock(enabled=False)),
    )
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    mocker.patch("jailbee.egress_pool.register_repo")
    mocker.patch(
        "jailbee.egress_pool.refresh_pool",
        return_value=RefreshResult(container_prefix="myrepo", status="ok"),
    )
    mocker.patch("jailbee.db.get_engine")
    mocker.patch("jailbee.upgrade.advice_lines", return_value=[])
    mocker.patch("jailbee.setup_command.consume_hint", return_value=[])
    mocker.patch("jailbee.cli._preflight_cache_pools")
    mocker.patch("jailbee.cli._finalize_new")
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    return cfg


def _new_container_that_detaches(mocker, trigger: str = "on_start"):
    """Stand in for `new_container`, invoking the caller's `on_detach`."""

    def fake(cfg, incus, opts, *, on_phase=None, confirm_fn=None, on_detach=None):
        if on_detach is not None:
            on_detach(cfg.autostart, trigger, "/home/dev/myrepo")
        return "myrepo-feat-a"

    return mocker.patch("jailbee.lifecycle.new_container", side_effect=fake)


def test_new_spawns_the_autostart_worker_when_a_stage_detaches(tmp_path, mocker):
    _setup_new(tmp_path, mocker)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")
    _new_container_that_detaches(mocker)

    result = runner.invoke(app, ["new", "feat-a", "--mount", "--no-attach"])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 1
    assert spawn.call_args.args[2] == "myrepo-feat-a"
    assert spawn.call_args.kwargs["from_trigger"] == "on_start"
    assert spawn.call_args.kwargs["repo_dir"] == "/home/dev/myrepo"


def test_new_no_wait_reaches_both_the_planner_and_the_spawner(tmp_path, mocker):
    """The worker re-plans from the job file: hand it `override=None` and it
    computes zero detached stages, runs nothing and exits 0 — every deferred
    stage lost in silence."""
    _setup_new(tmp_path, mocker)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")
    new_container = _new_container_that_detaches(mocker)

    result = runner.invoke(app, ["new", "feat-a", "--mount", "--no-attach", "--no-wait"])

    assert result.exit_code == 0, result.output
    assert new_container.call_args.args[2].autostart_override == "no_wait"
    assert spawn.call_args.kwargs["override"] == "no_wait"


def test_new_wait_is_passed_through_as_the_opt_out(tmp_path, mocker):
    _setup_new(tmp_path, mocker)
    mocker.patch("jailbee.cli._spawn_autostart_worker")
    new_container = _new_container_that_detaches(mocker)

    result = runner.invoke(app, ["new", "feat-a", "--mount", "--no-attach", "--wait"])

    assert result.exit_code == 0, result.output
    assert new_container.call_args.args[2].autostart_override == "wait"


def test_new_without_the_flags_leaves_the_override_unset(tmp_path, mocker):
    """No flag must mean "whatever the config says", not an implicit `wait`."""
    _setup_new(tmp_path, mocker)
    mocker.patch("jailbee.cli._spawn_autostart_worker")
    new_container = _new_container_that_detaches(mocker)

    result = runner.invoke(app, ["new", "feat-a", "--mount", "--no-attach"])

    assert result.exit_code == 0, result.output
    assert new_container.call_args.args[2].autostart_override is None


def test_new_wait_and_no_wait_are_mutually_exclusive(tmp_path, mocker):
    _setup_new(tmp_path, mocker)
    mocker.patch("jailbee.lifecycle.new_container", return_value="myrepo-feat-a")

    result = runner.invoke(app, ["new", "feat-a", "--mount", "--wait", "--no-wait"])

    assert result.exit_code == 2, result.output
    assert "mutually exclusive" in result.output


def test_new_worker_does_not_spawn_a_supervisor(tmp_path, mocker):
    """A background `jailbee new` is already a detached worker: it continues
    past the boundary in-process rather than handing to a second one, whose
    `start_job` would replace its own job row."""
    import json

    from jailbee import background

    cfg = _setup_new(tmp_path, mocker)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")
    mocker.patch("jailbee.cli._track_job")
    mocker.patch("jailbee.cli._finalize_new")
    new_container = mocker.patch("jailbee.lifecycle.new_container", return_value="myrepo-feat-a")

    from jailbee.lifecycle import NewContainerOptions

    opts = NewContainerOptions(
        container_branch="",
        name="myrepo-feat-a",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base=cfg.golden.alias,
        clone=False,
        mount=True,
    )
    job_file = tmp_path / "job.json"
    job_file.write_text(
        json.dumps(background.op_to_job(opts, container_name="myrepo-feat-a", log_path="/l"))
    )

    result = runner.invoke(app, ["_new-worker", "--job", str(job_file)])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 0
    assert new_container.call_args.kwargs.get("on_detach") is None


# ---- `jailbee start` / `jailbee restart`


def _setup_boot(tmp_path: Path, mocker, *, detached: bool):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")

    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-a"))
    mocker.patch("jailbee.cli._resolve_config_path_or_none", return_value=None)
    mocker.patch("jailbee.cli._preflight_cache_pools")
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.cli._clear_superseded_boot_job")
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.cli._post_create_gui_launches")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/myrepo")
    mocker.patch("jailbee.autostart.inject_github_token")
    run_autostart = mocker.patch(
        "jailbee.autostart.run_autostart",
        return_value=AutostartPlan(
            blocking=[], detached=[_stage("deps")] if detached else []
        ),
    )
    return run_autostart


def test_start_spawns_the_autostart_worker_when_a_stage_detaches(tmp_path, mocker):
    _setup_boot(tmp_path, mocker, detached=True)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = runner.invoke(app, ["start", "feat-a"])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 1
    assert spawn.call_args.kwargs["from_trigger"] == "on_start"
    assert spawn.call_args.kwargs["repo_dir"] == "/home/dev/myrepo"


def test_restart_spawns_the_autostart_worker_when_a_stage_detaches(tmp_path, mocker):
    _setup_boot(tmp_path, mocker, detached=True)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = runner.invoke(app, ["restart", "feat-a"])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 1


def test_start_does_not_spawn_when_nothing_detaches(tmp_path, mocker):
    _setup_boot(tmp_path, mocker, detached=False)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = runner.invoke(app, ["start", "feat-a"])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 0


def test_start_no_wait_reaches_both_the_planner_and_the_spawner(tmp_path, mocker):
    run_autostart = _setup_boot(tmp_path, mocker, detached=True)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = runner.invoke(app, ["start", "feat-a", "--no-wait"])

    assert result.exit_code == 0, result.output
    assert run_autostart.call_args.kwargs["override"] == "no_wait"
    assert spawn.call_args.kwargs["override"] == "no_wait"


def test_restart_wait_keeps_every_stage_in_the_foreground(tmp_path, mocker):
    run_autostart = _setup_boot(tmp_path, mocker, detached=False)
    mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = runner.invoke(app, ["restart", "feat-a", "--wait"])

    assert result.exit_code == 0, result.output
    assert run_autostart.call_args.kwargs["override"] == "wait"


def test_start_wait_and_no_wait_are_mutually_exclusive(tmp_path, mocker):
    _setup_boot(tmp_path, mocker, detached=False)

    result = runner.invoke(app, ["start", "feat-a", "--wait", "--no-wait"])

    assert result.exit_code == 2, result.output
    assert "mutually exclusive" in result.output


def test_restart_wait_and_no_wait_are_mutually_exclusive(tmp_path, mocker):
    _setup_boot(tmp_path, mocker, detached=False)

    result = runner.invoke(app, ["restart", "feat-a", "--wait", "--no-wait"])

    assert result.exit_code == 2, result.output
    assert "mutually exclusive" in result.output


def test_background_boot_worker_does_not_spawn_a_supervisor(tmp_path, mocker):
    """A background `start` already has a worker in flight; it continues past
    the boundary in-process rather than handing to a second process."""
    _setup_boot(tmp_path, mocker, detached=True)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")
    mocker.patch("jailbee.cli._track_job")

    result = runner.invoke(app, ["_boot-worker", "--name", "myrepo-feat-a"])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 0
