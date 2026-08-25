"""`jailbee base build` — upgrade-watermark bookkeeping."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

runner = CliRunner()


FIXTURES = Path(__file__).parent / "fixtures"


def _invoke(mocker, *, raises=None):
    from jailbee.cli import app

    mocker.patch("jailbee.incus.Incus")
    build = mocker.patch("jailbee.golden.build_golden_image")
    if raises is not None:
        build.side_effect = raises
    args = ["base", "build", "--config", str(FIXTURES / "full_config.yaml")]
    return runner.invoke(app, args), build


def test_successful_build_records_an_observed_watermark(mocker) -> None:
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RepoUpgradeState

    result, build = _invoke(mocker)

    assert result.exit_code == 0, result.output
    assert build.call_count == 1
    with Session(get_engine()) as session:
        rows = list(session.exec(select(RepoUpgradeState)).all())
    assert len(rows) == 1
    assert rows[0].base_build_observed is True


def test_failed_build_records_nothing(mocker) -> None:
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RepoUpgradeState
    from jailbee.incus import IncusError

    result, _ = _invoke(mocker, raises=IncusError("apt failed"))

    assert result.exit_code == 1
    with Session(get_engine()) as session:
        rows = list(session.exec(select(RepoUpgradeState)).all())
    assert all(row.base_build_observed is False for row in rows)
