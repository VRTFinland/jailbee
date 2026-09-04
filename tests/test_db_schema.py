"""Schema and migration tests for jailbee.db."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from jailbee.db import CURRENT_SCHEMA_VERSION


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
    assert meta.version == CURRENT_SCHEMA_VERSION


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
    assert meta.version == CURRENT_SCHEMA_VERSION


def _register(engine, prefix: str = "alpha") -> None:
    from jailbee.db.models import RegisteredRepo

    with Session(engine) as s:
        s.add(
            RegisteredRepo(
                container_prefix=prefix,
                repo_root=f"/home/u/{prefix}",
                registered_at=datetime(2026, 5, 19, tzinfo=UTC),
            )
        )
        s.commit()


def _set_version(engine, version: int) -> None:
    from jailbee.db.models import SchemaMeta

    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        assert meta is not None
        meta.version = version
        s.add(meta)
        s.commit()


def test_a_newer_schema_is_used_as_is_never_wiped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running an older jailbee against a newer DB must not destroy state.

    This is the everyday case for anyone who switches branches or rolls a
    release back, and the registry it used to drop is not regenerable: the
    dashboard then files every repo but the current directory's under a
    view-only "(orphan)" group, and the refresh timer stops refreshing them,
    with nothing said. Migrations are additive, so a newer schema is a
    superset this version can read and write.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    from jailbee.db import CURRENT_SCHEMA_VERSION, _ensure_schema, get_engine
    from jailbee.db.models import RegisteredRepo, SchemaMeta

    engine = get_engine()
    _register(engine)
    _set_version(engine, CURRENT_SCHEMA_VERSION + 1)

    _ensure_schema(engine)

    with Session(engine) as s:
        assert [r.container_prefix for r in s.exec(select(RegisteredRepo)).all()] == ["alpha"]
        meta = s.get(SchemaMeta, 1)
    assert meta is not None
    assert meta.version == CURRENT_SCHEMA_VERSION + 1, "the newer version must not be walked back"


def test_unreachable_version_keeps_a_backup_of_the_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reset stays for a version no migration chain can reach, but it may
    not be the end of the data: a copy is kept next to the database."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    from jailbee.db import _ensure_schema, get_engine
    from jailbee.db.models import RegisteredRepo

    engine = get_engine()
    _register(engine)
    _set_version(engine, 0)  # below the first migration step: unreachable

    _ensure_schema(engine)

    with Session(engine) as s:
        assert s.exec(select(RegisteredRepo)).all() == []

    (backup,) = list((tmp_path / "jailbee").glob("state.sqlite.bak-v*"))
    saved = create_engine(f"sqlite:///{backup}")
    with Session(saved) as s:
        assert [r.container_prefix for r in s.exec(select(RegisteredRepo)).all()] == ["alpha"]


def test_a_second_reset_does_not_clobber_the_first_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    from jailbee.db import _ensure_schema, get_engine

    engine = get_engine()
    _register(engine, "alpha")
    _set_version(engine, 0)
    _ensure_schema(engine)

    _register(engine, "beta")
    _set_version(engine, 0)
    _ensure_schema(engine)

    backups = sorted((tmp_path / "jailbee").glob("state.sqlite.bak-v*"))
    assert len(backups) == 2, f"expected both backups kept, got {backups}"


def test_get_engine_is_cached_so_the_schema_check_runs_once_per_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker,
) -> None:
    """A long-running process must not re-bootstrap the schema forever.

    `dashboard.registered_repo_configs` calls `get_engine` on every refresh
    tick, so an unbounded re-check meant a dashboard left open across an
    upgrade kept applying its own (older) idea of the schema to a database a
    newer jailbee had already migrated — the loop that emptied the repo
    registry. One check per process also drops a `create_all` round-trip from
    every tick.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    import jailbee.db as db

    spy = mocker.spy(db, "_ensure_schema")
    first = db.get_engine()
    second = db.get_engine()

    assert first is second
    assert spy.call_count == 1


def test_get_engine_caches_per_database_not_globally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two state dirs are two databases — the cache is keyed by path."""
    import jailbee.db as db

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "a"))
    first = db.get_engine()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "b"))
    second = db.get_engine()

    assert first is not second
    assert (tmp_path / "a" / "jailbee" / "state.sqlite").exists()
    assert (tmp_path / "b" / "jailbee" / "state.sqlite").exists()


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
    place straight to the current schema version: op_kind is added,
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
    # not just the next version, so this lands on 6 (v2's op_kind step, v3's
    # no-op gui_state guard, v4's card_style/collapsed_repos columns, v5's
    # no-op repo_upgrade_state guard, and v6's view_prefs copy step), not 2.
    assert meta is not None and meta.version == CURRENT_SCHEMA_VERSION
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
    CURRENT_SCHEMA_VERSION, not just the next version, so this lands
    on 6 rather than 3."""
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
    assert meta is not None and meta.version == CURRENT_SCHEMA_VERSION
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
    migrated in place straight to the current version: v4 adds both columns, a pre-existing row
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
    # Simulate the v3 gui_state shape: drop card_style (collapsed_repos is no
    # longer declared on GuiState at all, so create_all already omits it —
    # nothing to drop there), then insert a row the way a v3-era app would
    # have written it (no card_style / collapsed_repos).
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE gui_state DROP COLUMN card_style")
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
    assert meta is not None and meta.version == CURRENT_SCHEMA_VERSION
    assert repo is not None  # unrelated data preserved (non-destructive)

    # Idempotency: re-running the bootstrap must not error or change state.
    _ensure_schema(engine)
    with Session(engine) as s:
        meta2 = s.get(SchemaMeta, 1)
    assert meta2 is not None and meta2.version == CURRENT_SCHEMA_VERSION


def test_migrate_to_v4_is_idempotent() -> None:
    from jailbee.db import _migrate_to_v4

    engine = create_engine("sqlite:///:memory:")
    # card_style already present via create_all. collapsed_repos is not —
    # GuiState no longer declares it, so create_all builds the current
    # model's shape without it — put it back by hand so this test still
    # exercises the "already present" guard for both columns, the same way
    # the v6 tests restore the pre-v6 physical shape.
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE gui_state ADD COLUMN collapsed_repos VARCHAR")
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


def test_v4_db_migrates_to_current_adding_repo_upgrade_state() -> None:
    """An existing v4 DB gains the new table and lands on the current version
    without losing unrelated data. `create_all` makes the table;
    `_migrate_to_v5` only lets the version bump — the same shape as v3's
    gui_state step."""
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
        assert meta is not None and meta.version == CURRENT_SCHEMA_VERSION
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


def test_v4_db_migrates_to_current_moving_collapsed_repos_to_view_prefs() -> None:
    """An existing v4 DB gains the view_prefs table, and the Qt card view's
    folded set moves from gui_state.collapsed_repos to view_prefs('qt').
    The physical gui_state column is deliberately left in place: SQLite
    column drops are avoidable here and an unused nullable column is
    harmless. Unrelated data survives."""
    from jailbee.db import _ensure_schema
    from jailbee.db.models import RegisteredRepo, SchemaMeta, ViewPrefs

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=4))
        s.add(RegisteredRepo(container_prefix="p", repo_root="/r", registered_at=now))
        s.commit()
    # Simulate a v4 DB: view_prefs does not exist yet, and gui_state still
    # has the physical collapsed_repos column the v4-era Qt app wrote to.
    # `create_all` builds the *current* schema, where GuiState no longer
    # declares that column, so the older shape has to be put back by hand —
    # the mirror image of test_v3_db_migrates_to_v4 dropping columns.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE view_prefs")
        conn.exec_driver_sql("ALTER TABLE gui_state ADD COLUMN collapsed_repos VARCHAR")
        conn.exec_driver_sql(
            "INSERT INTO gui_state "
            "(id, layout, table_header_state, refresh_interval, refresh_paused, "
            " card_style, collapsed_repos) "
            "VALUES (1, 'cards', NULL, NULL, 0, 'compact', '[\"gisgro\",\"other\"]')"
        )

    _ensure_schema(engine)

    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        repo = s.get(RegisteredRepo, "p")
        qt = s.get(ViewPrefs, "qt")
    assert meta is not None and meta.version == CURRENT_SCHEMA_VERSION
    assert repo is not None  # unrelated data preserved (non-destructive)
    assert qt is not None
    assert qt.folded_repos == '["gisgro","other"]'
    assert qt.columns is None  # columns are seeded by the front-end, not here


def test_migrate_to_v6_does_not_clobber_an_existing_qt_row() -> None:
    """_ensure_schema re-runs the whole chain if the process dies before the
    version bump, so the copy must insert only when no qt row exists —
    otherwise a crash would revert folds the user has since changed."""
    from jailbee.db import _migrate_to_v6
    from jailbee.db.models import ViewPrefs

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ViewPrefs(frontend="qt", columns='["name"]', folded_repos="[]"))
        s.commit()
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE gui_state ADD COLUMN collapsed_repos VARCHAR")
        conn.exec_driver_sql(
            "INSERT INTO gui_state "
            "(id, layout, refresh_paused, card_style, collapsed_repos) "
            "VALUES (1, 'cards', 0, 'compact', '[\"stale\"]')"
        )
        _migrate_to_v6(conn)
        _migrate_to_v6(conn)  # idempotent

    with Session(engine) as s:
        qt = s.get(ViewPrefs, "qt")
    assert qt is not None
    assert qt.folded_repos == "[]"  # untouched
    assert qt.columns == '["name"]'


def test_view_prefs_rows_are_independent_per_frontend() -> None:
    from jailbee.db.models import ViewPrefs

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ViewPrefs(frontend="tui", columns='["name","state"]'))
        s.add(ViewPrefs(frontend="qt", columns='["name","ip"]'))
        s.commit()
        tui = s.get(ViewPrefs, "tui")
        qt = s.get(ViewPrefs, "qt")
    assert tui is not None and tui.columns == '["name","state"]'
    assert qt is not None and qt.columns == '["name","ip"]'


def test_host_setup_state_is_a_singleton_with_empty_timestamps() -> None:
    """A fresh row owes both the hint and the setup run — neither has happened."""
    from jailbee.db.models import HostSetupState

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as s:
        s.add(HostSetupState(id=1))
        s.commit()
        row = s.get(HostSetupState, 1)

    assert row is not None
    assert row.setup_at is None
    assert row.setup_version is None
    assert row.hint_shown_at is None


def test_host_setup_state_keeps_utc_timestamps() -> None:
    from jailbee.db.models import HostSetupState

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    now = datetime(2026, 8, 26, 9, 30, tzinfo=UTC)
    with Session(engine) as s:
        s.add(HostSetupState(id=1, setup_at=now, setup_version="1.2.0", hint_shown_at=now))
        s.commit()
        row = s.get(HostSetupState, 1)

    assert row is not None
    assert row.setup_at == now
    assert row.hint_shown_at == now


def test_v6_db_migrates_to_current_adding_host_setup_state() -> None:
    """An existing v6 DB gains the new table and lands on the current version
    without losing unrelated data. `create_all` makes the table;
    `_migrate_to_v7` only lets the version bump — the same shape as v5."""
    from jailbee.db import CURRENT_SCHEMA_VERSION, _ensure_schema
    from jailbee.db.models import HostSetupState, RegisteredRepo, SchemaMeta

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 8, 26, tzinfo=UTC)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=6))
        s.add(RegisteredRepo(container_prefix="sampleapp", repo_root="/r", registered_at=now))
        s.commit()
    # Simulate the v6 shape: the table did not exist yet.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE host_setup_state")

    _ensure_schema(engine)

    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        repo = s.get(RegisteredRepo, "sampleapp")
        assert meta is not None and meta.version == CURRENT_SCHEMA_VERSION
        assert repo is not None, "unrelated data preserved (non-destructive)"
        s.add(HostSetupState(id=1, setup_version="1.2.0", setup_at=now))
        s.commit()


def test_migrate_to_v7_is_idempotent() -> None:
    from jailbee.db import _migrate_to_v7

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)  # table already present
    with engine.begin() as conn:
        _migrate_to_v7(conn)  # must not raise on an already-migrated DB
        sql = "SELECT name FROM sqlite_master WHERE type='table'"
        names = {row[0] for row in conn.exec_driver_sql(sql)}
    assert "host_setup_state" in names


def test_v7_db_migrates_to_current_adding_egress_override() -> None:
    """An existing v7 DB gains the new table and lands on the current version
    without losing unrelated data. `create_all` makes the table;
    `_migrate_to_v8` only lets the version bump — the same shape as v7.

    v7 was claimed by two branches at once (`host_setup_state` here,
    `egress_override` on the egress-override branch); a DB stamped v7 by
    either of them converges on the same schema, because both steps are
    no-op guards and `create_all` supplies whichever table is missing.
    """
    from jailbee.db import CURRENT_SCHEMA_VERSION, _ensure_schema
    from jailbee.db.models import EgressOverride, RegisteredRepo, SchemaMeta

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 8, 26, tzinfo=UTC)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=7))
        s.add(RegisteredRepo(container_prefix="sampleapp", repo_root="/r", registered_at=now))
        s.commit()
    # Simulate the v7 shape written by the *other* branch: the table is absent.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE egress_override")

    _ensure_schema(engine)

    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        repo = s.get(RegisteredRepo, "sampleapp")
        assert meta is not None and meta.version == CURRENT_SCHEMA_VERSION
        assert repo is not None, "unrelated data preserved (non-destructive)"
        s.add(EgressOverride(container_prefix="sampleapp", entry="nexus.corp:443", added_at=now))
        s.commit()


def test_migrate_to_v8_is_idempotent() -> None:
    from jailbee.db import _migrate_to_v8

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)  # table already present
    with engine.begin() as conn:
        _migrate_to_v8(conn)  # must not raise on an already-migrated DB
        sql = "SELECT name FROM sqlite_master WHERE type='table'"
        names = {row[0] for row in conn.exec_driver_sql(sql)}
    assert "egress_override" in names


def test_v9_db_migrates_to_v10_adding_synthetic_config() -> None:
    """An existing v9 DB (registered_repo without synthetic_config) is
    migrated in place straight to the current version: v10 adds the column,
    a pre-existing row is back-filled to false — every repo registered
    before v10 had a config file — and unrelated data (the row itself)
    survives. Re-running the bootstrap is a no-op."""
    from jailbee.db import _ensure_schema
    from jailbee.db.models import RegisteredRepo, SchemaMeta

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=9))
        s.commit()
    # Simulate the v9 registered_repo shape: drop synthetic_config, then
    # insert a row the way a v9-era app would have written it (no
    # synthetic_config column at all).
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE registered_repo DROP COLUMN synthetic_config")
        conn.exec_driver_sql(
            "INSERT INTO registered_repo "
            "(container_prefix, repo_root, registered_at, last_refresh_at) "
            "VALUES ('myrepo', '/tmp/myrepo', :n, NULL)",
            {"n": now.isoformat()},
        )

    _ensure_schema(engine)

    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(registered_repo)")}
    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        repo = s.get(RegisteredRepo, "myrepo")
    assert "synthetic_config" in cols
    assert repo is not None
    assert repo.synthetic_config is False  # back-filled default
    assert meta is not None and meta.version == CURRENT_SCHEMA_VERSION

    # Idempotency: re-running the bootstrap must not error or change state.
    _ensure_schema(engine)
    with Session(engine) as s:
        meta2 = s.get(SchemaMeta, 1)
    assert meta2 is not None and meta2.version == CURRENT_SCHEMA_VERSION


def test_migrate_to_v10_is_idempotent() -> None:
    from jailbee.db import _migrate_to_v10

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    # Restore the pre-v10 physical shape so the guard is actually exercised,
    # the same way test_migrate_to_v4_is_idempotent restores collapsed_repos.
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE registered_repo DROP COLUMN synthetic_config")
        _migrate_to_v10(conn)
        _migrate_to_v10(conn)  # must not raise on an already-migrated table
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(registered_repo)")}
    assert "synthetic_config" in cols
