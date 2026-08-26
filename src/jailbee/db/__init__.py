"""SQLite-backed runtime state for jailbee (pool, registered repos, refresh log).

The database lives at `${XDG_STATE_HOME:-~/.local/state}/jailbee/state.sqlite`.
Schema is bootstrapped on first connection. Forward migrations are applied in
place (non-destructive). A *newer* database than this version knows about is
used as-is — migrations are additive, so it is a superset — because the
alternative, resetting it, silently destroys state that cannot be rebuilt (see
`_ensure_schema`). Only a version no migration chain can reach still falls back
to a drop-and-recreate, and that one keeps a copy of the database first.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session, SQLModel, create_engine

from jailbee.db.models import SchemaMeta

log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 7


def state_dir() -> Path:
    """Return the jailbee state directory under XDG_STATE_HOME."""
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "jailbee"
    return Path.home() / ".local" / "state" / "jailbee"


_ENGINES: dict[Path, Engine] = {}
_ENGINE_LOCK = threading.Lock()


def get_engine() -> Engine:
    """Return a SQLite engine for `state.sqlite`, bootstrapping it once.

    Cached per database path, for the lifetime of the process. Callers treat
    this as cheap — `dashboard.registered_repo_configs` calls it on every
    refresh tick — and without the cache each of those calls opened a new
    connection pool *and* re-ran `_ensure_schema`, `create_all` included.

    Running the schema check once per process is also what keeps a stale
    process honest: a dashboard left open across an upgrade goes on serving
    the schema it started with instead of repeatedly re-asserting its own
    (older) idea of it over a database a newer jailbee has since migrated.
    Migrations are additive, so the rows and tables it did not create are
    simply invisible to it until it is restarted.

    Locked because both dashboards refresh from a worker thread while the
    UI thread reads the same database.
    """
    path = state_dir()
    path.mkdir(parents=True, exist_ok=True)
    db_path = (path / "state.sqlite").resolve()
    with _ENGINE_LOCK:
        cached = _ENGINES.get(db_path)
        if cached is not None:
            return cached
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"timeout": 30, "check_same_thread": False},
        )
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        _ensure_schema(engine)
        _ENGINES[db_path] = engine
        return engine


def _migrate_to_v2(conn: Connection) -> None:
    """v1 -> v2: add background_op.op_kind, back-filling existing rows to
    'create'. Idempotent: a no-op if the column already exists."""
    cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(background_op)")}
    if "op_kind" not in cols:
        conn.exec_driver_sql(
            "ALTER TABLE background_op ADD COLUMN op_kind VARCHAR NOT NULL DEFAULT 'create'"
        )


def _migrate_to_v3(conn: Connection) -> None:
    """v2 -> v3: add the gui_state table. ``create_all`` (run before the
    migration loop in ``_ensure_schema``) already creates the new table, so
    this step is an idempotent no-op guard whose job is to let the version
    bump to 3, mirroring how fresh tables are handled elsewhere."""
    return None


def _migrate_to_v4(conn: Connection) -> None:
    """v3 -> v4: add gui_state.card_style and gui_state.collapsed_repos.
    Idempotent per-column: each ADD COLUMN is independently guarded by its
    own ``PRAGMA table_info`` check, so re-running is a no-op only where a
    column already exists — not a whole-step no-op if just one is missing."""
    cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(gui_state)")}
    if "card_style" not in cols:
        conn.exec_driver_sql(
            "ALTER TABLE gui_state ADD COLUMN card_style VARCHAR NOT NULL DEFAULT 'compact'"
        )
    if "collapsed_repos" not in cols:
        conn.exec_driver_sql("ALTER TABLE gui_state ADD COLUMN collapsed_repos VARCHAR")


def _migrate_to_v5(conn: Connection) -> None:
    """v4 -> v5: add the repo_upgrade_state table. ``create_all`` (run before
    the migration loop in ``_ensure_schema``) already creates the new table,
    so this step is an idempotent no-op guard whose job is to let the version
    bump to 5 — the same shape as ``_migrate_to_v3``."""
    return None


def _migrate_to_v6(conn: Connection) -> None:
    """v5 -> v6: move the Qt card view's folded set from
    ``gui_state.collapsed_repos`` into ``view_prefs('qt').folded_repos``.

    ``create_all`` (run before the migration loop in ``_ensure_schema``)
    already created ``view_prefs``, so this step only copies. It inserts
    only when no ``qt`` row exists: ``_ensure_schema`` re-runs the whole
    chain if the process dies before the version bump, and a second copy
    would revert folds the user has changed since. The physical
    ``gui_state.collapsed_repos`` column is deliberately left in place —
    SQLite column drops are avoidable here, and an unused nullable column
    is harmless.
    """
    cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(gui_state)")}
    if "collapsed_repos" not in cols:
        return
    conn.exec_driver_sql(
        "INSERT INTO view_prefs (frontend, columns, folded_repos) "
        "SELECT 'qt', NULL, collapsed_repos FROM gui_state "
        "WHERE id = 1 AND collapsed_repos IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM view_prefs WHERE frontend = 'qt')"
    )


def _migrate_to_v7(conn: Connection) -> None:
    """v6 -> v7: add the host_setup_state table. ``create_all`` (run before
    the migration loop in ``_ensure_schema``) already creates the new table,
    so this step is an idempotent no-op guard whose job is to let the version
    bump to 7 — the same shape as ``_migrate_to_v5``."""
    return None


# target_version -> non-destructive migration step
_MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
    7: _migrate_to_v7,
}


def _backup_database(engine: Engine, from_version: int) -> Path | None:
    """Copy the database next to itself, returning the copy (None if in-memory).

    Uses SQLite's own backup API rather than `shutil.copy` because the
    database runs in WAL mode: copying the `.sqlite` file alone can leave
    committed rows behind in the `-wal` sidecar.

    Never overwrites an existing backup — a second reset from the same
    version would otherwise replace the copy holding the original data with
    a copy of the already-emptied one.
    """
    raw = engine.url.database
    if not raw or raw == ":memory:":
        return None
    src = Path(raw)
    if not src.exists():
        return None
    dest = src.with_name(f"{src.name}.bak-v{from_version}")
    n = 1
    while dest.exists():
        dest = src.with_name(f"{src.name}.bak-v{from_version}.{n}")
        n += 1
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest


def _ensure_schema(engine: Engine) -> None:
    """Create missing tables; run forward migrations; reset only as a fallback.

    Fresh DBs get every table (with all current columns) from
    ``create_all``. An existing DB at an older version is migrated in place
    via ``_MIGRATIONS`` (non-destructive).

    A **newer** database is used exactly as it is. Every migration is
    additive, so a newer schema is a superset this version can read and
    write, and the version is deliberately left alone — walking it back
    would make the next newer jailbee re-run migrations over data that has
    already seen them. Resetting here used to be the behaviour, on the
    grounds that "pool data is regenerable from DNS"; that reasoning does
    not survive contact with the rest of the tables. `registered_repo` in
    particular is the dashboard's only way to map a container back to its
    repo and the refresh timer's only work list, and nothing rebuilds it:
    the loss shows up as every repo but the current directory's rendering
    as a view-only "(orphan)" group and as pools that quietly stop being
    refreshed, with no error anywhere. Downgrades are routine — a rollback,
    or a maintainer moving between branches — so this path must not be
    destructive.

    A version no chain of registered steps can reach (a gap) still falls
    back to the historical drop-and-recreate, but keeps a copy of the
    database first and says where it went.
    """
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        meta = s.get(SchemaMeta, 1)
        if meta is None:
            s.add(SchemaMeta(id=1, version=CURRENT_SCHEMA_VERSION))
            s.commit()
            return
        current = meta.version

    if current == CURRENT_SCHEMA_VERSION:
        return

    if current > CURRENT_SCHEMA_VERSION:
        log.warning(
            "db: %s is at schema v%d, newer than this jailbee (v%d) — using it as-is",
            engine.url.database,
            current,
            CURRENT_SCHEMA_VERSION,
        )
        return

    if current < CURRENT_SCHEMA_VERSION and all(
        v in _MIGRATIONS for v in range(current + 1, CURRENT_SCHEMA_VERSION + 1)
    ):
        # Each step in _MIGRATIONS MUST be idempotent: if the process
        # crashes between this block and the version-bump session below,
        # _ensure_schema re-runs all steps from `current` on next startup.
        with engine.begin() as conn:
            for v in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
                _MIGRATIONS[v](conn)
        with Session(engine) as s:
            meta = s.get(SchemaMeta, 1)
            assert meta is not None
            meta.version = CURRENT_SCHEMA_VERSION
            s.add(meta)
            s.commit()
        return

    # Unreachable by forward migration (a gap in the chain): the historical
    # destructive reset, but not before a copy is put aside.
    backup = _backup_database(engine, current)
    log.warning(
        "db: schema v%d cannot be migrated to v%d — resetting the database%s",
        current,
        CURRENT_SCHEMA_VERSION,
        f"; previous contents saved to {backup}" if backup is not None else "",
    )
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=CURRENT_SCHEMA_VERSION))
        s.commit()
