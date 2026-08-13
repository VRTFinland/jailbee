"""Tests for `gie base usage`. Fully mocked — no real Incus daemon."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config import load_config
from jailbee.db.models import RegisteredRepo

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg():
    return load_config(FIXTURES / "full_config.yaml")


def _register(db_engine, *prefixes):
    from sqlmodel import Session

    with Session(db_engine) as s:
        for p in prefixes:
            s.add(
                RegisteredRepo(
                    container_prefix=p,
                    repo_root=f"/repos/{p}",
                    registered_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        s.commit()


def test_usage_default_lists_live_and_archives(mocker):
    cfg = _cfg()
    alias = cfg.golden.alias
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_images.return_value = [
        {"aliases": [{"name": alias}], "size": 2000},
        {"aliases": [{"name": f"{alias}-2026-07-20"}], "size": 500},
        {"aliases": [{"name": f"{alias}-2026-07-18"}], "size": 300},
    ]
    result = CliRunner().invoke(app, ["base", "usage"])
    assert result.exit_code == 0, result.stdout
    assert alias in result.stdout
    assert "2026-07-20" in result.stdout
    assert "Total" in result.stdout
    # prunable = archives only = 800 bytes, not the 2000-byte live image
    assert "800" in result.stdout.replace(",", "")


def test_usage_no_images(mocker):
    cfg = _cfg()
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_images.return_value = []
    result = CliRunner().invoke(app, ["base", "usage"])
    assert result.exit_code == 0, result.stdout
    assert "No golden images found" in result.stdout


def test_usage_all_aggregates_across_repos(mocker, db_engine):
    _register(db_engine, "foo", "bar")
    mocker.patch("jailbee.db.get_engine", return_value=db_engine)
    mocker.patch(
        "jailbee.cli._load_or_exit",
        side_effect=AssertionError("must not load cwd config in --all mode"),
    )
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_images.return_value = [
        {"aliases": [{"name": "foo-base"}], "size": 2000},
        {"aliases": [{"name": "foo-base-2026-07-20"}], "size": 500},
        {"aliases": [{"name": "bar-base-2026-07-18"}], "size": 300},
    ]
    result = CliRunner().invoke(app, ["base", "usage", "--all"])
    assert result.exit_code == 0, result.stdout
    assert "foo-base" in result.stdout
    assert "bar-base" in result.stdout
