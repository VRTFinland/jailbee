"""CLI forwarding for the upgrade-wide Docker seed opt-out."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app


@pytest.mark.parametrize("args,expected", [([], True), (["--no-docker-seed"], False)])
def test_upgrade_forwards_docker_seed_policy(mocker, tmp_path, args, expected):
    mocker.patch("jailbee.dashboard.model.registered_repo_roots", return_value=[tmp_path])
    mocker.patch("jailbee.incus.Incus")
    run = mocker.patch("jailbee.upgrade_all.run_upgrade_all", return_value=[])

    result = CliRunner().invoke(app, ["upgrade", *args])

    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["docker_seed"] is expected
