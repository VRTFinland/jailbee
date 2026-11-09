"""The foreground commands hand their deferred autostart stages over.

`new`, `start` and `restart` each run the blocking stages themselves and
then spawn `_autostart-worker` for the rest. This file covers that hand-off
and the `--wait` / `--no-wait` overrides; the supervisor itself is
test_autostart_supervisor.py's and test_cli_boot.py's business.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.autostart_plan import AutostartPlan
from jailbee.cli import app
from tests.conftest import make_cfg, memory_engine

runner = CliRunner()


def _stage(name: str):
    from jailbee.config import AutostartStage

    return AutostartStage(stage=name, steps=[{"name": "s", "run": "true"}])  # type: ignore[arg-type]


# ---- `jailbee new`


def _setup_new(tmp_path: Path, mocker):
    """A `jailbee new --mount` run with everything but the autostart wiring stubbed."""
    from jailbee.egress_pool import RefreshResult
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, after_new="none")
    object.__setattr__(cfg, "container_prefix", "myrepo")

    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_config_path_or_none", return_value=None)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(docker_registry_mirror=DockerRegistryMirror(enabled=False)),
    )
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    mocker.patch("jailbee.egress_pool.register_repo")
    mocker.patch(
        "jailbee.egress_pool.refresh_pool",
        return_value=RefreshResult(container_prefix="myrepo", status="ok"),
    )
    mocker.patch("jailbee.db.get_engine", return_value=memory_engine())
    mocker.patch("jailbee.upgrade.advice_lines", return_value=[])
    mocker.patch("jailbee.setup_command.consume_hint", return_value=[])
    mocker.patch("jailbee.create_flow.preflight_cache_pools")
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


def test_new_hands_the_supervisor_the_endpoint_the_blocking_stages_ran_with(tmp_path, mocker):
    """`new_cmd` drops the mirror endpoint when its CA cert is missing and the
    net mode is loose — "half a mirror is not a mirror". Re-deriving it for the
    hand-off (`_mirror_endpoint_or_none` does no CA check) would pin
    `jailbee-registry-mirror.incus` into /etc/hosts for the deferred stages
    only, against a proxy the container has no CA for."""
    from jailbee.global_config import GlobalConfig

    _setup_new(tmp_path, mocker)
    gcfg = GlobalConfig()
    # No `ca/ca.crt` under it, which is the whole point.
    object.__setattr__(gcfg.docker_registry_mirror, "data_dir", tmp_path / "registry")
    mocker.patch("jailbee.cli._load_global", return_value=gcfg)
    mocker.patch("jailbee.create_flow.mirror_wanted", return_value=True)
    mocker.patch("jailbee.docker_daemon.compute_mirror_endpoint", return_value=("10.0.0.5", 3128))
    # What the real helper would return here: it never looks at the CA.
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=("10.0.0.5", 3128))
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")
    new_container = _new_container_that_detaches(mocker)

    result = runner.invoke(app, ["new", "feat-a", "--mount", "--no-attach", "--net", "loose"])

    assert result.exit_code == 0, result.output
    # Pin that the CA-missing branch is the one that ran, so the two
    # assertions below cannot pass because the mirror was never wanted.
    assert "Mirror CA cert not found" in result.output
    assert new_container.call_args.args[2].mirror_endpoint is None
    assert spawn.call_args.kwargs["mirror_endpoint"] is None


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
    # It *does* hand `new_container` a handler — one that runs the deferred
    # stages here rather than spawning a second process for them.
    handler = new_container.call_args.kwargs["on_detach"]
    run_detached = mocker.patch("jailbee.autostart.run_detached")
    handler(cfg.autostart, "on_start", "/r")
    assert spawn.call_count == 0
    assert run_detached.call_count == 1


# ---- `jailbee start` / `jailbee restart`


def _setup_boot(tmp_path: Path, mocker, *, detached: bool):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")

    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-a"))
    mocker.patch("jailbee.cli._resolve_config_path_or_none", return_value=None)
    mocker.patch("jailbee.create_flow.preflight_cache_pools")
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.cli._clear_superseded_boot_job")
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.cli._post_create_gui_launches")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/myrepo")
    mocker.patch("jailbee.autostart.inject_github_token")
    mocker.patch("jailbee.hosts.sync_hosts")
    mocker.patch("jailbee.egress_proxy.sync_container")
    mocker.patch(
        "jailbee.branch_config.effective_autostart_config", side_effect=lambda cfg, *a, **kw: cfg
    )
    run_autostart = mocker.patch(
        "jailbee.autostart.run_autostart",
        return_value=AutostartPlan(blocking=[], detached=[_stage("deps")] if detached else []),
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


def test_restart_wait_reaches_the_planner_as_the_override(tmp_path, mocker):
    """`run_autostart` is mocked here, so this pins the kwarg and nothing
    more. That `override="wait"` really keeps every stage in the foreground
    is test_apply.py's `test_restart_one_runs_every_stage_in_the_foreground`,
    which drives the real planner."""
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
    run_detached = mocker.patch("jailbee.autostart.run_detached")
    mocker.patch("jailbee.cli._track_job")

    result = runner.invoke(app, ["_boot-worker", "--name", "myrepo-feat-a"])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 0
    # The supervisor's algorithm still runs — in this process.
    assert run_detached.call_count == 1
    spec = run_detached.call_args.args[2]
    assert spec.container_name == "myrepo-feat-a"
    assert spec.from_trigger == "on_start"
    assert spec.repo_dir == "/home/dev/myrepo"


@pytest.mark.parametrize("presence", [True, False])
def test_detach_handlers_forward_branch_services_presence(tmp_path, mocker, presence):
    from jailbee import cli

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")
    handler = cli._autostart_detach_handler(
        cfg, incus, config_path=None, full_name="c", mirror_endpoint=None, override=None
    )
    handler(cfg.autostart, "on_create", "/r", presence)
    assert spawn.call_args.kwargs["services_present"] is presence

    run = mocker.patch("jailbee.autostart.run_detached")
    inline = cli._inline_autostart_handler(
        cfg,
        incus,
        engine=None,
        full_name="c",
        log_path=str(tmp_path / "log"),
        mirror_endpoint=None,
        override=None,
    )
    inline(cfg.autostart, "on_create", "/r", presence)
    assert run.call_args.args[2].services_present is presence


@pytest.mark.parametrize("command", ["start", "restart"])
@pytest.mark.parametrize("yes", [False, True])
def test_start_uses_branch_autostart_for_profile_and_detached_job(tmp_path, mocker, command, yes):
    from jailbee.config import Autostart, AutostartStep

    run = _setup_boot(tmp_path, mocker, detached=True)
    host = make_cfg(tmp_path / "myrepo", services={"db": {"run": "sleep infinity"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=host)
    branch = host.model_copy(update={
        "autostart": Autostart(on_start=[AutostartStep(name="branch", run="branch-command")]),
        "services": {},
    })
    loader = mocker.patch(
        "jailbee.branch_config.effective_autostart_config", return_value=branch, create=True
    )
    profile = mocker.patch("jailbee.autostart_profiles.resolve_profile", return_value=None)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = runner.invoke(app, [command, "feat-a", *(["--yes"] if yes else [])])

    assert result.exit_code == 0, result.output
    assert host.services and not branch.services
    assert loader.call_args.args[0] is host
    assert loader.call_args.kwargs == {"assume_yes": yes, "allow_prompt": True}
    assert profile.call_args.args[0] is branch
    assert run.call_args.args[0].autostart.on_start[0].run == "branch-command"
    assert spawn.call_args.kwargs["autostart"].on_start[0].run == "branch-command"
    assert spawn.call_args.kwargs["services_present"] is False
