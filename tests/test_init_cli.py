"""Tests for the CLI-level `gie init` orchestration.

The bulk of init-command tests live in test_init.py and exercise
`run_init` directly. These tests focus on the wiring added in
2026-05-19 for the egress pool auto-refresh: systemd unit install
and pool registration must happen as part of the `gie init` CLI path.
"""

from __future__ import annotations

import os
from pathlib import Path

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app


def test_cli_init_installs_units_and_registers_repo(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    # Stand up a real fixture config so load_config succeeds.
    fixtures = Path(__file__).parent / "fixtures"

    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=fixtures / "full_config.yaml",
    )
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=mocker.Mock(
            docker_registry_mirror=mocker.Mock(enabled=False),
        ),
    )
    mocker.patch("jailbee.incus.Incus", return_value=mocker.Mock())
    mocker.patch("jailbee.init_command.run_init")

    install_mock = mocker.patch(
        "jailbee.init_command.install_systemd_units",
    )
    register_mock = mocker.patch("jailbee.egress_pool.register_repo")
    mocker.patch("jailbee.db.get_engine")
    # Avoid touching real loginctl
    proc = mocker.Mock(stdout="Linger=yes\n")
    mocker.patch("subprocess.run", return_value=proc)
    # Avoid a real privilege prompt from the linger tip
    mocker.patch.dict(os.environ, {"USER": "testuser"})

    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    install_mock.assert_called_once()
    register_mock.assert_called_once()
