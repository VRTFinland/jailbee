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
    cfg.is_synthetic.return_value = False

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
    cfg.is_synthetic.return_value = False

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
    cfg1.is_synthetic.return_value = False
    register_repo(db_session, cfg1)

    cfg2 = mocker.Mock()
    cfg2.container_prefix = "SampleApp"
    cfg2.repo_root = new_path
    cfg2.is_synthetic.return_value = False
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
    cfg_a.is_synthetic.return_value = False
    register_repo(db_session, cfg_a)

    cfg_b = mocker.Mock()
    cfg_b.container_prefix = "NewName"
    cfg_b.repo_root = tmp_path
    cfg_b.is_synthetic.return_value = False
    register_repo(db_session, cfg_b)

    rows = db_session.exec(select(RegisteredRepo)).all()
    prefixes = {r.container_prefix for r in rows}
    assert prefixes == {"NewName"}


def test_register_repo_records_synthetic_flag(
    db_session: Session,
    tmp_path: Path,
    make_cfg,
) -> None:
    from jailbee.egress_pool import register_repo

    cfg = make_cfg(tmp_path / "tutkimus")
    cfg._synthetic = True

    register_repo(db_session, cfg)

    row = db_session.get(RegisteredRepo, "tutkimus")
    assert row is not None and row.synthetic_config is True


def test_register_repo_clears_flag_when_a_config_file_appears(
    db_session: Session,
    tmp_path: Path,
    make_cfg,
) -> None:
    """A scratch directory that later gets a real config must stop being
    treated as synthetic, or `refresh_all` would keep a stale row alive."""
    from jailbee.egress_pool import register_repo

    scratch_cfg = make_cfg(tmp_path / "tutkimus")
    scratch_cfg._synthetic = True
    register_repo(db_session, scratch_cfg)

    # Confirm the row really started out True, so the next assertion proves
    # a transition rather than passing by coincidence with the column default.
    row = db_session.get(RegisteredRepo, "tutkimus")
    assert row is not None and row.synthetic_config is True

    register_repo(db_session, make_cfg(tmp_path / "tutkimus"))

    row = db_session.get(RegisteredRepo, "tutkimus")
    assert row is not None and row.synthetic_config is False


def test_register_repo_sets_flag_when_a_config_file_disappears(
    db_session: Session,
    tmp_path: Path,
    make_cfg,
) -> None:
    from jailbee.egress_pool import register_repo

    register_repo(db_session, make_cfg(tmp_path / "tutkimus"))

    # Confirm the initial insert really wrote False, so the next assertion
    # proves a transition rather than passing by coincidence with the column
    # default (which rules out an insert path that silently no-ops on the
    # flag instead of actually recording it).
    row = db_session.get(RegisteredRepo, "tutkimus")
    assert row is not None and row.synthetic_config is False

    scratch_cfg = make_cfg(tmp_path / "tutkimus")
    scratch_cfg._synthetic = True
    register_repo(db_session, scratch_cfg)

    row = db_session.get(RegisteredRepo, "tutkimus")
    assert row is not None and row.synthetic_config is True
