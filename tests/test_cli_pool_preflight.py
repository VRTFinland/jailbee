"""CLI tests for the cache-pool preflight.

`pool.ensure_pool_dirs` refuses a root that holds both `slots/slot-0` and
loose cache content, and every boot path runs it — `new_container` via
`allocate_startup`, `start`/`restart` via `boot_container`. Before this
preflight existed, `jailbee new` hit that refusal *after* creating the
container, attaching its GUI sockets and its port forwards, and the
detached worker reported it as a traceback in a log file.

So each command that starts in a terminal resolves the pools first, and
refuses cleanly when it cannot. The resolution itself is tested in
tests/test_pool.py; these tests cover the wiring and the refusal.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import make_cfg

runner = CliRunner()


def _unresolved(mocker, names: list[str]):
    """Report `names` as pools that could not be brought into a usable state."""
    return mocker.patch("jailbee.pool.preflight_pools", return_value=names)


# ---- jailbee new


def test_new_refuses_before_creating_anything_when_a_pool_is_unresolved(tmp_path, mocker):
    """The bug this fixes: the container was created, its GUI sockets and
    port forwards attached, and only then did `allocate_startup` raise."""
    from tests.test_cli import _setup_new_cmd_env

    _repo, new_container = _setup_new_cmd_env(tmp_path, mocker)
    _unresolved(mocker, ["gradle"])

    result = runner.invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 2, result.output
    new_container.assert_not_called()
    assert "gradle" in result.output


def test_new_refuses_before_spawning_the_background_worker(tmp_path, mocker):
    """A detached worker has no stdin, so it could only ever rediscover this
    the hard way — with the container already half-built and the error in a
    log the user has to go and `cat`."""
    from tests.test_cli import _setup_new_cmd_env

    _setup_new_cmd_env(tmp_path, mocker)
    _unresolved(mocker, ["gradle"])
    # Not `cli.subprocess.Popen`: that patch is process-wide and breaks every
    # `subprocess.run` the command makes on the way here. `op_to_job` is the
    # first thing the spawn path does and is exclusive to it.
    op_to_job = mocker.patch("jailbee.background.op_to_job")

    result = runner.invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart", "--background"])

    assert result.exit_code == 2, result.output
    op_to_job.assert_not_called()


def test_new_proceeds_once_the_pools_are_usable(tmp_path, mocker):
    from tests.test_cli import _setup_new_cmd_env

    _repo, new_container = _setup_new_cmd_env(tmp_path, mocker)
    preflight = _unresolved(mocker, [])

    result = runner.invoke(app, ["new", "feat/x", "--no-clone", "--no-autostart"])

    assert result.exit_code == 0, result.output
    new_container.assert_called_once()
    assert preflight.call_count == 1


# ---- jailbee start / restart


def _setup_boot(tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, shared_dir=tmp_path / "shared")
    object.__setattr__(cfg, "container_prefix", "myrepo")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-feat-a"))
    mocker.patch("jailbee.cli._post_start_actions")
    return mocker.patch("jailbee.lifecycle.boot_container")


def test_restart_refuses_instead_of_raising_from_boot_container(tmp_path, mocker):
    """`boot_container` -> `allocate_startup` would raise `PoolError` with no
    handler above it, printing a traceback for a situation the user can fix."""
    boot = _setup_boot(tmp_path, mocker)
    _unresolved(mocker, ["gradle"])

    result = runner.invoke(app, ["restart", "feat-a"])

    assert result.exit_code == 2, result.output
    boot.assert_not_called()
    assert "gradle" in result.output


def test_start_refuses_when_a_pool_is_unresolved(tmp_path, mocker):
    boot = _setup_boot(tmp_path, mocker)
    _unresolved(mocker, ["gradle"])

    result = runner.invoke(app, ["start", "feat-a"])

    assert result.exit_code == 2, result.output
    boot.assert_not_called()


def test_start_proceeds_once_the_pools_are_usable(tmp_path, mocker):
    boot = _setup_boot(tmp_path, mocker)
    _unresolved(mocker, [])

    result = runner.invoke(app, ["start", "feat-a"])

    assert result.exit_code == 0, result.output
    boot.assert_called_once()


def test_boot_preflight_runs_before_the_background_fork(tmp_path, mocker):
    """Same reason as `new`: the `_boot-worker` has no terminal to ask on."""
    _setup_boot(tmp_path, mocker)
    _unresolved(mocker, ["gradle"])
    spawn = mocker.patch("jailbee.cli._spawn_boot_worker")

    result = runner.invoke(app, ["restart", "feat-a", "--background"])

    assert result.exit_code == 2, result.output
    spawn.assert_not_called()


def test_preflight_declines_to_prompt_without_a_terminal(tmp_path, mocker):
    """`jailbee new < /dev/null` in a script must report, not hang or move a
    user's cache content on an answer nobody gave."""
    from jailbee.cli import _preflight_cache_pools

    cfg = make_cfg(tmp_path, shared_dir=tmp_path / "shared")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    preflight = mocker.patch("jailbee.pool.preflight_pools", return_value=[])

    _preflight_cache_pools(cfg)

    assert preflight.call_args.kwargs["confirm"] is None


def test_preflight_prompts_on_a_terminal(tmp_path, mocker):
    from jailbee.cli import _preflight_cache_pools

    cfg = make_cfg(tmp_path, shared_dir=tmp_path / "shared")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    preflight = mocker.patch("jailbee.pool.preflight_pools", return_value=[])

    _preflight_cache_pools(cfg)

    assert preflight.call_args.kwargs["confirm"] is not None


def test_preflight_names_the_pool_and_the_way_out(tmp_path, mocker):
    """The message has to carry both, or it is the old "by hand" dead end
    with extra steps."""
    import typer

    import pytest

    from jailbee.cli import _preflight_cache_pools

    cfg = make_cfg(tmp_path, shared_dir=tmp_path / "shared")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.pool.preflight_pools", return_value=["gradle", "m2"])

    with pytest.raises(typer.Exit) as exc:
        _preflight_cache_pools(cfg)

    assert exc.value.exit_code == 2
