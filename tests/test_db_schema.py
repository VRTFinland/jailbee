"""Schema and migration tests for jailbee.db."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select


def test_pool_ip_composite_primary_key() -> None:
    from jailbee.db.models import PoolIP

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    now = datetime(2026, 5, 19, 17, 0, 0, tzinfo=UTC)
    with Session(engine) as s:
        s.add(
            PoolIP(
                container_prefix="SampleApp",
                hostname="github.com",
                ip="140.82.121.3",
                first_seen=now,
                last_seen=now,
            )
        )
        s.add(
            PoolIP(
                container_prefix="SampleApp",
                hostname="github.com",
                ip="140.82.121.4",
                first_seen=now,
                last_seen=now,
            )
        )
        s.commit()
        rows = s.exec(select(PoolIP)).all()
    assert len(rows) == 2


def test_refresh_state_primary_key_is_container_prefix() -> None:
    from jailbee.db.models import RefreshState

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(
            RefreshState(
                container_prefix="SampleApp",
                last_refresh_at=now,
                last_refresh_status="ok",
            )
        )
        s.commit()
        got = s.get(RefreshState, "SampleApp")
    assert got is not None
    assert got.last_refresh_status == "ok"


def test_registered_repo_holds_path_and_prefix() -> None:
    from jailbee.db.models import RegisteredRepo

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(
            RegisteredRepo(
                container_prefix="SampleApp",
                repo_root="/home/u/dev/SampleApp",
                registered_at=now,
            )
        )
        s.commit()
        got = s.get(RegisteredRepo, "SampleApp")
    assert got is not None
    assert got.repo_root == "/home/u/dev/SampleApp"
    assert got.last_refresh_at is None


def test_get_engine_creates_db_and_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    from jailbee.db import get_engine
    from jailbee.db.models import SchemaMeta

    engine = get_engine()
    assert (tmp_path / "jailbee" / "state.sqlite").exists()

    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
    assert meta is not None
    assert meta.version == 5


def test_schema_mismatch_drops_and_recreates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    from jailbee.db import _ensure_schema, get_engine
    from jailbee.db.models import PoolIP, SchemaMeta

    engine = get_engine()
    now = datetime(2026, 5, 19, tzinfo=UTC)
    with Session(engine) as s:
        s.add(
            PoolIP(
                container_prefix="X",
                hostname="h",
                ip="1.2.3.4",
                first_seen=now,
                last_seen=now,
            )
        )
        s.commit()
        # Simulate old schema version on disk
        meta = s.get(SchemaMeta, 1)
        assert meta is not None
        meta.version = 0
        s.add(meta)
        s.commit()

    # Re-bootstrap should detect mismatch and wipe
    _ensure_schema(engine)
    with Session(engine) as s:
        rows = s.exec(select(PoolIP)).all()
        meta = s.get(SchemaMeta, 1)
    assert rows == []
    assert meta is not None
    assert meta.version == 5


def test_state_dir_respects_xdg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "custom"))

    from jailbee.db import state_dir

    assert state_dir() == tmp_path / "custom" / "jailbee"


def test_state_dir_default_when_xdg_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    from jailbee.db import state_dir

    assert state_dir() == tmp_path / ".local" / "state" / "jailbee"


def test_state_dir_uses_jailbee_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from jailbee.db import state_dir

    assert state_dir() == tmp_path / "jailbee"


def test_background_op_kind_defaults_to_create() -> None:
    from jailbee.db.models import JOB_CREATE, JOB_DESTROY, BackgroundJob

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    now = datetime(2026, 6, 4, tzinfo=UTC)
    with Session(engine) as s:
        s.add(
            BackgroundJob(
                container_name="sampleapp-a",
                container_prefix="sampleapp",
                phase="starting",
                pid=1,
                log_path="/l",
                started_at=now,
                updated_at=now,
            )
        )
        s.add(
            BackgroundJob(
                container_name="sampleapp-b",
                container_prefix="sampleapp",
                phase="stopping",
                pid=2,
                log_path="/l",
                op_kind=JOB_DESTROY,
                started_at=now,
                updated_at=now,
            )
        )
        s.commit()
        a = s.get(BackgroundJob, "sampleapp-a")
        b = s.get(BackgroundJob, "sampleapp-b")
    assert a is not None and a.op_kind == JOB_CREATE
    assert b is not None and b.op_kind == JOB_DESTROY
    assert (JOB_CREATE, JOB_DESTROY) == ("create", "destroy")


def test_v1_db_migrates_to_current_preserving_data() -> None:
    """An existing v1 DB (background_op without op_kind) is migrated in
    place straight to the current schema version (5): op_kind is added,
    existing rows keep their data, and unrelated tables (registered_repo)
    survive."""
    from sqlalchemy import text

    from jailbee.db import _ensure_schema
    from jailbee.db.models import RegisteredRepo, SchemaMeta

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 6, 4, tzinfo=UTC)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=1))
        s.add(
            RegisteredRepo(
                container_prefix="sampleapp",
                repo_root="/r",
                registered_at=now,
            )
        )
        s.commit()
    # Simulate the v1 table shape: drop the op_kind column.
    # Pass timestamps as ISO strings — sqlite3's default datetime adapter is
    # deprecated in Python 3.12+, and the test only reads op_kind back.
    iso = now.isoformat()
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE background_op DROP COLUMN op_kind")
        conn.exec_driver_sql(
            "INSERT INTO background_op "
            "(container_name, container_prefix, phase, pid, log_path, "
            " started_at, updated_at) "
            "VALUES ('sampleapp-old', 'sampleapp', 'creating', 5, '/l', :n, :n)",
            {"n": iso},
        )

    _ensure_schema(engine)

    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(background_op)")}
        kind = conn.execute(
            text("SELECT op_kind FROM background_op WHERE container_name='sampleapp-old'")
        ).scalar_one()
    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        repo = s.get(RegisteredRepo, "sampleapp")
    assert "op_kind" in cols
    assert kind == "create"  # back-filled default
    # v1 -> current: _ensure_schema always migrates to CURRENT_SCHEMA_VERSION,
    # not just the next version, so this lands on 5 (v2's op_kind step, v3's
    # no-op gui_state guard, v4's card_style/collapsed_repos columns, and v5's
    # no-op repo_upgrade_state guard), not 2.
    assert meta is not None and meta.version == 5
    assert repo is not None  # unrelated data preserved (non-destructive)


def test_migrate_to_v2_is_idempotent() -> None:
    from jailbee.db import _migrate_to_v2

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)  # op_kind already present
    with engine.begin() as conn:
        _migrate_to_v2(conn)  # must not raise on an already-migrated table
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(background_op)")}
    assert "op_kind" in cols


def test_gui_state_single_row_defaults() -> None:
    from jailbee.db.models import GuiState

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(GuiState())  # all defaults
        s.commit()
        got = s.get(GuiState, 1)
    assert got is not None
    assert got.id == 1
    assert got.layout == "cards"
    assert got.table_header_state is None
    assert got.refresh_interval is None
    assert got.refresh_paused is False


def test_v2_db_migrates_to_current_adding_gui_state() -> None:
    """An existing v2 DB gains a usable gui_state table, preserving
    unrelated data. _ensure_schema always migrates straight to
    CURRENT_SCHEMA_VERSION (5), not just the next version, so this lands
    on 5 rather than 3."""
    from datetime import UTC, datetime

    from jailbee.db import _ensure_schema
    from jailbee.db.models import GuiState, RegisteredRepo, SchemaMeta

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 7, 20, tzinfo=UTC)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=2))
        s.add(RegisteredRepo(container_prefix="p", repo_root="/r", registered_at=now))
        s.commit()

    _ensure_schema(engine)

    with Session(engine) as s:
        s.add(GuiState(id=1, layout="table"))
        s.commit()
        meta = s.get(SchemaMeta, 1)
        repo = s.get(RegisteredRepo, "p")
        state = s.get(GuiState, 1)
    assert meta is not None and meta.version == 5
    assert repo is not None  # unrelated data preserved
    assert state is not None and state.layout == "table"


def test_migrate_to_v3_is_idempotent() -> None:
    from jailbee.db import _migrate_to_v3

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        _migrate_to_v3(conn)  # must not raise; gui_state already exists
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "gui_state" in tables


def test_v3_db_migrates_to_v4_adding_gui_state_columns() -> None:
    """An existing v3 DB (gui_state without card_style/collapsed_repos) is
    migrated in place straight to v5: v4 adds both columns, a pre-existing row
    is back-filled with the default card_style, and unrelated tables
    (registered_repo) survive. Re-running the bootstrap is a no-op."""
    from jailbee.db import _ensure_schema
    from jailbee.db.models import GuiState, RegisteredRepo, SchemaMeta

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 7, 21, tzinfo=UTC)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=3))
        s.add(RegisteredRepo(container_prefix="p", repo_root="/r", registered_at=now))
        s.commit()
    # Simulate the v3 gui_state shape: drop the new columns, then insert a
    # row the way a v3-era app would have written it (no card_style /
    # collapsed_repos).
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE gui_state DROP COLUMN card_style")
        conn.exec_driver_sql("ALTER TABLE gui_state DROP COLUMN collapsed_repos")
        conn.exec_driver_sql(
            "INSERT INTO gui_state "
            "(id, layout, table_header_state, refresh_interval, refresh_paused) "
            "VALUES (1, 'table', NULL, NULL, 0)"
        )

    _ensure_schema(engine)

    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(gui_state)")}
    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        repo = s.get(RegisteredRepo, "p")
        state = s.get(GuiState, 1)
    assert {"card_style", "collapsed_repos"} <= cols
    assert state is not None
    assert state.card_style == "compact"  # back-filled default
    assert state.collapsed_repos is None
    assert meta is not None and meta.version == 5
    assert repo is not None  # unrelated data preserved (non-destructive)

    # Idempotency: re-running the bootstrap must not error or change state.
    _ensure_schema(engine)
    with Session(engine) as s:
        meta2 = s.get(SchemaMeta, 1)
    assert meta2 is not None and meta2.version == 5


def test_migrate_to_v4_is_idempotent() -> None:
    from jailbee.db import _migrate_to_v4

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)  # card_style/collapsed_repos already present
    with engine.begin() as conn:
        _migrate_to_v4(conn)  # must not raise on an already-migrated table
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(gui_state)")}
    assert {"card_style", "collapsed_repos"} <= cols


def test_repo_upgrade_state_primary_key_is_container_prefix() -> None:
    from jailbee.db.models import RepoUpgradeState

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    now = datetime(2026, 8, 25, tzinfo=UTC)
    with Session(engine) as s:
        s.add(
            RepoUpgradeState(
                container_prefix="sampleapp",
                base_build_version="1.0.0",
                base_build_observed=False,
                apply_version="1.0.0",
                apply_observed=True,
                updated_at=now,
            )
        )
        s.commit()
        got = s.get(RepoUpgradeState, "sampleapp")
    assert got is not None
    assert got.base_build_observed is False
    assert got.apply_observed is True
    assert got.updated_at == now


def test_v4_db_migrates_to_v5_adding_repo_upgrade_state() -> None:
    """An existing v4 DB gains the new table and lands on version 5 without
    losing unrelated data. `create_all` makes the table; `_migrate_to_v5` only
    lets the version bump — the same shape as v3's gui_state step."""
    from jailbee.db import _ensure_schema
    from jailbee.db.models import RegisteredRepo, RepoUpgradeState, SchemaMeta

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=4))
        s.add(RegisteredRepo(container_prefix="sampleapp", repo_root="/r", registered_at=now))
        s.commit()
    # Simulate the v4 shape: the table did not exist yet.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE repo_upgrade_state")

    _ensure_schema(engine)

    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        repo = s.get(RegisteredRepo, "sampleapp")
        assert meta is not None and meta.version == 5
        assert repo is not None, "unrelated data preserved (non-destructive)"
        s.add(
            RepoUpgradeState(
                container_prefix="sampleapp",
                base_build_version="1.0.0",
                base_build_observed=True,
                apply_version="1.0.0",
                apply_observed=True,
                updated_at=now,
            )
        )
        s.commit()


def test_migrate_to_v5_is_idempotent() -> None:
    from jailbee.db import _migrate_to_v5

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)  # table already present
    with engine.begin() as conn:
        _migrate_to_v5(conn)  # must not raise on an already-migrated DB
        sql = "SELECT name FROM sqlite_master WHERE type='table'"
        names = {row[0] for row in conn.exec_driver_sql(sql)}
    assert "repo_upgrade_state" in names
