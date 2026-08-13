"""CLI tests for `gie net refresh / status / unregister`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def test_net_refresh_no_args_calls_refresh_all(mocker: MockerFixture) -> None:
    from jailbee.egress_pool import RefreshResult

    mocker.patch("jailbee.cli._load_global")
    mocker.patch("jailbee.incus.Incus")
    refresh_all = mocker.patch(
        "jailbee.egress_pool.refresh_all",
        return_value={"X": RefreshResult(container_prefix="X", status="ok")},
    )
    mocker.patch("jailbee.db.get_engine")

    result = runner.invoke(app, ["net", "refresh"])
    assert result.exit_code == 0, result.output
    refresh_all.assert_called_once()


def test_net_refresh_json_output(mocker: MockerFixture) -> None:
    from jailbee.egress_pool import RefreshResult

    mocker.patch("jailbee.cli._load_global")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.egress_pool.refresh_all",
        return_value={
            "X": RefreshResult(
                container_prefix="X",
                status="ok",
                added=[("github.com", "1.1.1.1")],
            ),
        },
    )
    mocker.patch("jailbee.db.get_engine")

    result = runner.invoke(app, ["net", "refresh", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["X"]["status"] == "ok"


def test_net_refresh_silent_on_noop(mocker: MockerFixture) -> None:
    from jailbee.egress_pool import RefreshResult

    mocker.patch("jailbee.cli._load_global")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.egress_pool.refresh_all",
        return_value={"X": RefreshResult(container_prefix="X", status="ok")},
    )
    mocker.patch("jailbee.db.get_engine")

    result = runner.invoke(app, ["net", "refresh"])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""  # silent on full no-op


def test_net_refresh_exits_nonzero_on_acl_error(mocker: MockerFixture) -> None:
    from jailbee.egress_pool import RefreshResult

    mocker.patch("jailbee.cli._load_global")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(
        "jailbee.egress_pool.refresh_all",
        return_value={
            "X": RefreshResult(
                container_prefix="X",
                status="acl_error",
                error="nft EBUSY",
            ),
        },
    )
    mocker.patch("jailbee.db.get_engine")

    result = runner.invoke(app, ["net", "refresh"])
    assert result.exit_code == 1
    combined = (result.output or "") + (result.stderr or "")
    assert "nft EBUSY" in combined


def test_net_install_calls_install_systemd_units(mocker: MockerFixture) -> None:
    install_mock = mocker.patch(
        "jailbee.init_command.install_systemd_units",
    )

    result = runner.invoke(app, ["net", "install"])
    assert result.exit_code == 0, result.output
    install_mock.assert_called_once()


def test_net_unregister_removes_row(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    from jailbee.db.models import RegisteredRepo

    mock_cfg = mocker.Mock()
    mock_cfg.container_prefix = "X"
    mocker.patch("jailbee.cli.load_config", return_value=mock_cfg)
    mocker.patch("jailbee.cli.find_repo_config", return_value=tmp_path)

    fake_session: Any = mocker.MagicMock()
    fake_session.__enter__.return_value = fake_session
    fake_session.get.return_value = RegisteredRepo(
        container_prefix="X",
        repo_root=str(tmp_path),
        registered_at=mocker.ANY,
    )
    mocker.patch("sqlmodel.Session", return_value=fake_session)
    mocker.patch("jailbee.db.get_engine")

    result = runner.invoke(app, ["net", "unregister", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    fake_session.delete.assert_called_once()
    fake_session.commit.assert_called_once()
