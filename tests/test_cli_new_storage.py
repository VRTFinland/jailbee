"""`jailbee new` creates the container on the pool `--storage` / `defaults.storage_pool` names."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app
from tests.test_cli_new_detach import _new_container_that_detaches, _setup_new

runner = CliRunner()


def _pools(mocker, *names: str) -> None:
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_storage_pools.return_value = [{"name": n} for n in names]


def _new(tmp_path, mocker, *args: str, config_pool: str | None = None, pools=("default", "cow")):
    cfg = _setup_new(tmp_path, mocker)
    _pools(mocker, *pools)
    cfg.defaults.storage_pool = config_pool
    new_container = _new_container_that_detaches(mocker)
    result = runner.invoke(app, ["new", "feat-a", "--mount", "--no-attach", *args])
    return result, new_container


def test_no_pool_named_leaves_it_to_the_profile(tmp_path, mocker):
    result, new_container = _new(tmp_path, mocker)
    assert result.exit_code == 0, result.output
    assert new_container.call_args.args[2].storage_pool is None


def test_config_pool_is_used(tmp_path, mocker):
    result, new_container = _new(tmp_path, mocker, config_pool="cow")
    assert result.exit_code == 0, result.output
    assert new_container.call_args.args[2].storage_pool == "cow"


def test_flag_overrides_config(tmp_path, mocker):
    result, new_container = _new(tmp_path, mocker, "--storage", "default", config_pool="cow")
    assert result.exit_code == 0, result.output
    assert new_container.call_args.args[2].storage_pool == "default"


def test_unknown_pool_exits_2_naming_the_candidates_before_creating_anything(tmp_path, mocker):
    result, new_container = _new(tmp_path, mocker, "--storage", "nope")
    assert result.exit_code == 2
    assert "nope" in result.output
    assert "default" in result.output and "cow" in result.output
    new_container.assert_not_called()
