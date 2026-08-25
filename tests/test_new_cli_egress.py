"""Tests for the `gie new` CLI wiring of register_repo + refresh_pool.

The lifecycle tests cover new_container itself; this test focuses on
the CLI-level glue added in 2026-05-19 so that newly-created strict
containers see a freshly-populated egress pool.
"""

from __future__ import annotations

from pathlib import Path

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app


def test_cli_new_registers_and_refreshes_before_new_container(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.egress_pool import RefreshResult

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

    register_mock = mocker.patch("jailbee.egress_pool.register_repo")
    refresh_mock = mocker.patch(
        "jailbee.egress_pool.refresh_pool",
        return_value=RefreshResult(container_prefix="X", status="ok"),
    )
    new_container_mock = mocker.patch("jailbee.lifecycle.new_container", return_value="X-test")
    mocker.patch("jailbee.db.get_engine")
    # Unrelated to this test's concern (register_repo/refresh_pool wiring):
    # without this, `_advise_upgrade`'s Session runs real queries against the
    # bare-mocked engine above and SQLAlchemy emits a spurious SAWarning.
    mocker.patch("jailbee.upgrade.advice_lines", return_value=[])

    # Use mount mode so we don't need a real .git dir at cfg.repo_root.
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["new", "smokebox", "--mount", "--no-autostart"],
    )
    assert result.exit_code == 0, result.output

    register_mock.assert_called_once()
    refresh_mock.assert_called_once()
    new_container_mock.assert_called_once()
