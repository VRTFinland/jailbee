"""Tests for jailbee.egress_pool.register_repo."""

from __future__ import annotations

from pathlib import Path

from pytest_mock import MockerFixture
from sqlmodel import Session, select

from jailbee.db.models import RegisteredRepo


def test_register_repo_inserts_new(
    db_session: Session,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.egress_pool import register_repo

    cfg = mocker.Mock()
    cfg.container_prefix = "SampleApp"
    cfg.repo_root = tmp_path

    register_repo(db_session, cfg)

    rows = db_session.exec(select(RegisteredRepo)).all()
    assert len(rows) == 1
    assert rows[0].container_prefix == "SampleApp"
    assert rows[0].repo_root == str(tmp_path.resolve())


def test_register_repo_idempotent(
    db_session: Session,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.egress_pool import register_repo

    cfg = mocker.Mock()
    cfg.container_prefix = "SampleApp"
    cfg.repo_root = tmp_path

    register_repo(db_session, cfg)
    register_repo(db_session, cfg)

    rows = db_session.exec(select(RegisteredRepo)).all()
    assert len(rows) == 1


def test_register_repo_updates_path_on_mv(
    db_session: Session,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.egress_pool import register_repo

    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.mkdir()
    new_path.mkdir()

    cfg1 = mocker.Mock()
    cfg1.container_prefix = "SampleApp"
    cfg1.repo_root = old_path
    register_repo(db_session, cfg1)

    cfg2 = mocker.Mock()
    cfg2.container_prefix = "SampleApp"
    cfg2.repo_root = new_path
    register_repo(db_session, cfg2)

    rows = db_session.exec(select(RegisteredRepo)).all()
    assert len(rows) == 1
    assert rows[0].repo_root == str(new_path.resolve())


def test_register_repo_clears_stale_prefix_at_same_path(
    db_session: Session,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.egress_pool import register_repo

    cfg_a = mocker.Mock()
    cfg_a.container_prefix = "OldName"
    cfg_a.repo_root = tmp_path
    register_repo(db_session, cfg_a)

    cfg_b = mocker.Mock()
    cfg_b.container_prefix = "NewName"
    cfg_b.repo_root = tmp_path
    register_repo(db_session, cfg_b)

    rows = db_session.exec(select(RegisteredRepo)).all()
    prefixes = {r.container_prefix for r in rows}
    assert prefixes == {"NewName"}
