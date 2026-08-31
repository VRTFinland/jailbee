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
    # Unrelated to this test's concern (systemd units / pool registration):
    # without this, `_record_upgrade_action` runs a real query against the
    # bare-mocked engine above and SQLAlchemy emits a spurious SAWarning.
    # `test_cli_init_records_an_apply_watermark` covers the watermark itself.
    mocker.patch("jailbee.upgrade.record")
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


def _init_invocation(mocker: MockerFixture) -> None:
    """The common `jailbee init` mocking, minus the state DB.

    `jailbee.db.get_engine` is deliberately *not* patched here: these tests
    assert on what init wrote to `repo_upgrade_state`, and `_isolate_state_dir`
    already gives the test its own `state.sqlite`.
    """
    fixtures = Path(__file__).parent / "fixtures"

    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=fixtures / "full_config.yaml",
    )
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=mocker.Mock(docker_registry_mirror=mocker.Mock(enabled=False)),
    )
    mocker.patch("jailbee.incus.Incus", return_value=mocker.Mock())
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("jailbee.egress_pool.register_repo")
    mocker.patch("subprocess.run", return_value=mocker.Mock(stdout="Linger=yes\n"))
    mocker.patch.dict(os.environ, {"USER": "testuser"})


def _upgrade_rows() -> list:
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RepoUpgradeState

    with Session(get_engine()) as session:
        return list(session.exec(select(RepoUpgradeState)).all())


def test_cli_init_records_an_apply_watermark(mocker: MockerFixture) -> None:
    """`init` writes what `apply` writes (profiles, ACL, shared dirs), so a
    freshly inited repo must not then be told to run `jailbee apply` for the
    very changes `jailbee init` just made."""
    _init_invocation(mocker)
    mocker.patch("jailbee.init_command.run_init")

    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    rows = _upgrade_rows()
    assert len(rows) == 1
    assert rows[0].apply_observed is True
    assert rows[0].base_build_observed is False, "init builds no image"


def test_cli_init_records_nothing_when_run_init_fails(mocker: MockerFixture) -> None:
    """Profiles that were never written must not silence the advice."""
    _init_invocation(mocker)
    mocker.patch("jailbee.init_command.run_init", side_effect=RuntimeError("profile exists"))

    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 1

    assert _upgrade_rows() == []


def test_cli_init_reports_pool_error_legibly(mocker: MockerFixture) -> None:
    """`ensure_pools` raises `PoolError` (not `RuntimeError`) when a pool root
    holds both migrated slots and un-migrated loose cache content. Without a
    matching `except` clause in `cli.py`'s `init` command, this escapes as an
    unhandled traceback instead of the same clean "error: ..." + exit 1 that
    every other `run_init` failure gets."""
    from jailbee.pool import PoolError

    _init_invocation(mocker)
    mocker.patch(
        "jailbee.init_command.run_init",
        side_effect=PoolError("holds both pool slots and loose cache content"),
    )

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 1
    assert "holds both pool slots and loose cache content" in result.output
