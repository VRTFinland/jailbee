"""SQLite-backed runtime state for jailbee (pool, registered repos, refresh log).

The database lives at `${XDG_STATE_HOME:-~/.local/state}/jailbee/state.sqlite`.
Schema is bootstrapped on first connection. Forward migrations are applied
in place (non-destructive); an unreachable version falls back to a destructive
drop-and-recreate (pool data is regenerable from DNS).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session, SQLModel, create_engine

from jailbee.db.models import SchemaMeta

CURRENT_SCHEMA_VERSION = 7


def state_dir() -> Path:
    """Return the jailbee state directory under XDG_STATE_HOME."""
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "jailbee"
    return Path.home() / ".local" / "state" / "jailbee"


def get_engine() -> Engine:
    """Return a SQLite engine for `state.sqlite`, creating + bootstrapping on first call."""
    path = state_dir()
    path.mkdir(parents=True, exist_ok=True)
    db_path = path / "state.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    _ensure_schema(engine)
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
    """v6 -> v7: add the egress_override table. ``create_all`` (run before the
    migration loop in ``_ensure_schema``) already creates the new table, so
    this step is an idempotent no-op guard whose job is to let the version
    bump to 7, mirroring how fresh tables are handled elsewhere."""
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


def _ensure_schema(engine: Engine) -> None:
    """Create missing tables; run forward migrations; reset only as a fallback.

    Fresh DBs get every table (with all current columns) from
    ``create_all``. An existing DB at an older version is migrated in
    place via ``_MIGRATIONS`` (non-destructive). A version we cannot
    reach with the registered steps (downgrade, or a gap in the chain)
    falls back to the historical destructive drop-and-recreate.
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

    # Unreachable by forward migration (downgrade / missing step): the
    # historical destructive reset. Pool data is regenerable from DNS.
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=CURRENT_SCHEMA_VERSION))
        s.commit()
