"""Tests for `gie base prune`. Fully mocked — no real Incus daemon."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config import load_config
from jailbee.db.models import RegisteredRepo
from jailbee.golden import ArchivedImage
from jailbee.incus import IncusError

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg():
    return load_config(FIXTURES / "full_config.yaml")


def test_prune_nothing_to_do(mocker):
    mocker.patch("jailbee.cli._load_or_exit", return_value=_cfg())
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.golden.find_archived_images", return_value=[])
    result = CliRunner().invoke(app, ["base", "prune"])
    assert result.exit_code == 0, result.stdout
    assert "Nothing to prune" in result.stdout


def test_prune_yes_to_all_deletes_every_archive(mocker):
    cfg = _cfg()
    alias = cfg.golden.alias
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    mocker.patch(
        "jailbee.golden.find_archived_images",
        return_value=[
            ArchivedImage(f"{alias}-2026-07-20", date(2026, 7, 20), 500),
            ArchivedImage(f"{alias}-2026-07-18", date(2026, 7, 18), 300),
        ],
    )
    result = CliRunner().invoke(app, ["base", "prune", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout
    deleted = {c.args[0] for c in incus.image_delete.call_args_list}
    assert deleted == {f"{alias}-2026-07-20", f"{alias}-2026-07-18"}


def test_prune_days_filters_recent_archives(mocker):
    cfg = _cfg()
    alias = cfg.golden.alias
    today = date.today()
    recent = today - timedelta(days=1)  # inside a --days 30 window -> must be KEPT
    old = today - timedelta(days=400)  # outside -> must be DELETED
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    mocker.patch(
        "jailbee.golden.find_archived_images",
        return_value=[
            ArchivedImage(f"{alias}-{recent.isoformat()}", recent, 500),
            ArchivedImage(f"{alias}-{old.isoformat()}", old, 300),
        ],
    )
    result = CliRunner().invoke(app, ["base", "prune", "--days", "30", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout
    deleted = {c.args[0] for c in incus.image_delete.call_args_list}
    assert deleted == {f"{alias}-{old.isoformat()}"}  # only the old one


def test_prune_single_confirm_yes_deletes_all(mocker):
    cfg = _cfg()
    alias = cfg.golden.alias
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    mocker.patch(
        "jailbee.golden.find_archived_images",
        return_value=[
            ArchivedImage(f"{alias}-2026-07-20", date(2026, 7, 20), 500),
            ArchivedImage(f"{alias}-2026-07-18", date(2026, 7, 18), 300),
        ],
    )
    mocker.patch("jailbee.cli.typer.confirm", return_value=True)
    result = CliRunner().invoke(app, ["base", "prune"])
    assert result.exit_code == 0, result.stdout
    deleted = {c.args[0] for c in incus.image_delete.call_args_list}
    assert deleted == {f"{alias}-2026-07-20", f"{alias}-2026-07-18"}


def test_prune_single_confirm_no_deletes_none(mocker):
    cfg = _cfg()
    alias = cfg.golden.alias
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    mocker.patch(
        "jailbee.golden.find_archived_images",
        return_value=[ArchivedImage(f"{alias}-2026-07-20", date(2026, 7, 20), 500)],
    )
    mocker.patch("jailbee.cli.typer.confirm", return_value=False)
    result = CliRunner().invoke(app, ["base", "prune"])
    assert result.exit_code == 0, result.stdout
    incus.image_delete.assert_not_called()
    assert "Aborted" in result.stdout


def test_prune_skips_in_use_image_with_warning(mocker):
    cfg = _cfg()
    alias = cfg.golden.alias
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.image_delete.side_effect = [IncusError("Image is currently in use"), None]
    mocker.patch(
        "jailbee.golden.find_archived_images",
        return_value=[
            ArchivedImage(f"{alias}-2026-07-20", date(2026, 7, 20), 500),
            ArchivedImage(f"{alias}-2026-07-18", date(2026, 7, 18), 300),
        ],
    )
    result = CliRunner().invoke(app, ["base", "prune", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout  # one failure does not abort
    attempted = [c.args[0] for c in incus.image_delete.call_args_list]
    assert attempted == [f"{alias}-2026-07-20", f"{alias}-2026-07-18"]
    assert "in use" in result.stdout.lower() or "skip" in result.stdout.lower()
    assert "2026-07-18" in result.stdout


def test_prune_non_in_use_error_is_not_mislabeled(mocker):
    cfg = _cfg()
    alias = cfg.golden.alias
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.image_delete.side_effect = IncusError("storage pool error")
    mocker.patch(
        "jailbee.golden.find_archived_images",
        return_value=[
            ArchivedImage(f"{alias}-2026-07-20", date(2026, 7, 20), 500),
        ],
    )
    result = CliRunner().invoke(app, ["base", "prune", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout  # a failure never aborts the batch
    assert "in use" not in result.stdout.lower()
    assert "storage pool error" in result.stdout


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


def test_prune_all_aggregates_across_repos(mocker, db_engine):
    _register(db_engine, "foo", "bar")
    mocker.patch("jailbee.db.get_engine", return_value=db_engine)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_images.return_value = [
        {"aliases": [{"name": "foo-base"}], "size": 2000},  # live, kept
        {"aliases": [{"name": "foo-base-2026-07-20"}], "size": 500},
        {"aliases": [{"name": "bar-base-2026-07-18"}], "size": 300},
    ]
    result = CliRunner().invoke(app, ["base", "prune", "--all", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout
    deleted = {c.args[0] for c in incus.image_delete.call_args_list}
    assert deleted == {"foo-base-2026-07-20", "bar-base-2026-07-18"}
    assert "foo-base" not in deleted  # live never deleted


def test_prune_all_does_not_require_cwd_config(mocker, db_engine):
    _register(db_engine, "foo")
    mocker.patch("jailbee.db.get_engine", return_value=db_engine)
    mocker.patch(
        "jailbee.cli._load_or_exit",
        side_effect=AssertionError("must not load cwd config in --all mode"),
    )
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_images.return_value = [
        {"aliases": [{"name": "foo-base-2026-07-20"}], "size": 500},
    ]
    result = CliRunner().invoke(app, ["base", "prune", "--all", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout
    assert {c.args[0] for c in incus.image_delete.call_args_list} == {"foo-base-2026-07-20"}


def test_prune_all_no_registered_repos(mocker, db_engine):
    mocker.patch("jailbee.db.get_engine", return_value=db_engine)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_images.return_value = []
    result = CliRunner().invoke(app, ["base", "prune", "--all", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout
    assert "Nothing to prune" in result.stdout


def test_prune_all_continues_past_in_use(mocker, db_engine):
    _register(db_engine, "foo", "bar")
    mocker.patch("jailbee.db.get_engine", return_value=db_engine)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_images.return_value = [
        {"aliases": [{"name": "foo-base-2026-07-20"}], "size": 500},
        {"aliases": [{"name": "bar-base-2026-07-18"}], "size": 300},
    ]
    incus.image_delete.side_effect = [IncusError("Image is currently in use"), None]
    result = CliRunner().invoke(app, ["base", "prune", "--all", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout
    attempted = [c.args[0] for c in incus.image_delete.call_args_list]
    assert len(attempted) == 2  # loop continued past the in-use failure


def test_prune_never_deletes_live_alias(mocker):
    """Defense-in-depth: exercise the real find_archived_images, not a stub."""
    cfg = _cfg()
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    alias = cfg.golden.alias
    incus.list_images.return_value = [
        {"aliases": [{"name": alias}], "size": 999},  # live — must never be deleted
        {"aliases": [{"name": f"{alias}-2026-07-20"}], "size": 500},  # dated archive
    ]
    result = CliRunner().invoke(app, ["base", "prune", "--yes-to-all"])
    assert result.exit_code == 0, result.stdout
    assert incus.image_delete.call_count == 1
    called_aliases = [c.args[0] for c in incus.image_delete.call_args_list]
    assert called_aliases == [f"{alias}-2026-07-20"]
    assert alias not in called_aliases
