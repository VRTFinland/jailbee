"""SQLModel tables for jailbee's host-side runtime state.

Tables are keyed by `container_prefix` (the natural identifier of a
jailbee repo), not by ACL name or repo path. `acl_name` is always
derivable as `f"{container_prefix}-allowlist"`.

SQLite drops tzinfo on round-trip, so we apply a TypeDecorator that
re-attaches UTC on load. All jailbee timestamps are UTC; this keeps the
in-Python type ``datetime`` consistent on both sides of the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime, Dialect, TypeDecorator
from sqlmodel import Field, SQLModel


class _UTCDateTime(TypeDecorator[datetime]):
    """DateTime column that always returns timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(
        self,
        value: Any,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None
        dt: datetime = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)


class SchemaMeta(SQLModel, table=True):
    """Single-row table tracking schema version for in-place migration."""

    __tablename__ = "schema_meta"

    id: int = Field(default=1, primary_key=True)
    version: int


class PoolIP(SQLModel, table=True):
    """One IP for one hostname in one repo's ACL pool."""

    __tablename__ = "pool_ip"

    container_prefix: str = Field(primary_key=True, index=True)
    hostname: str = Field(primary_key=True)
    ip: str = Field(primary_key=True)
    first_seen: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))
    last_seen: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))


class RefreshState(SQLModel, table=True):
    """Per-repo bookkeeping: when did we last refresh and how did it go."""

    __tablename__ = "refresh_state"

    container_prefix: str = Field(primary_key=True)
    last_refresh_at: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))
    last_refresh_status: str  # "ok" | "dns_error" | "partial" | "acl_error"
    last_error_msg: str | None = None


class RegisteredRepo(SQLModel, table=True):
    """Repos the refresh timer iterates. Self-pruned on missing config."""

    __tablename__ = "registered_repo"

    container_prefix: str = Field(primary_key=True)
    repo_root: str
    registered_at: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))
    last_refresh_at: datetime | None = Field(
        default=None,
        sa_column=Column(_UTCDateTime, nullable=True),
    )


JOB_CREATE = "create"
JOB_DESTROY = "destroy"


class BackgroundJob(SQLModel, table=True):
    """A detached `jailbee new` or `jailbee destroy` job in flight (or failed).

    Inserted by the foreground when it spawns the worker, updated by the
    worker as it progresses, and deleted on success. A row that outlives
    its worker (failed, or worker killed) is what `jailbee ls` surfaces and
    `jailbee job clear` acknowledges.
    """

    # The table and the `op_kind` column keep their original names: renaming
    # a persisted SQLModel field renames its column, which would need a
    # schema migration for something no user ever sees.
    __tablename__ = "background_op"

    container_name: str = Field(primary_key=True)
    container_prefix: str = Field(index=True)
    branch: str | None = None
    phase: str
    pid: int
    log_path: str
    error_msg: str | None = None
    op_kind: str = Field(default=JOB_CREATE)
    started_at: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))
    updated_at: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))


class GuiState(SQLModel, table=True):
    """Single-row (id=1) persisted state for the Qt dashboard *widget* itself
    (layout, header, card style, refresh cadence).

    Machine-written UI state — kept in the state DB (not config.yaml) so it
    stays out of the user's hand-edited config. Written by the Qt app only;
    the Rich TUI never touches it. The folded-repos set used to live here
    too, but moved to ``ViewPrefs`` since it is dashboard *view* state
    shared in spirit with the TUI's columns, not Qt-widget plumbing.
    """

    __tablename__ = "gui_state"

    id: int = Field(default=1, primary_key=True)
    layout: str = "cards"  # "table" | "cards"
    table_header_state: str | None = None  # base64(QHeaderView.saveState())
    refresh_interval: float | None = None
    refresh_paused: bool = False
    card_style: str = "compact"  # "compact" | "grid"


class ViewPrefs(SQLModel, table=True):
    """One dashboard front-end's persisted view state.

    Keyed by front-end (``"tui"`` / ``"qt"``) because the two are
    deliberately independent: a user may want a narrow TUI and a wide Qt
    table, and neither follows the other. Machine-written UI state, so it
    lives here rather than in config.yaml — the ``dashboard:`` config block
    this replaces is deprecated (seeded once, then inert).

    ``columns`` is a JSON list of enabled column names; ``None`` means the
    built-in default set. Stored order is not significant today — the
    dashboards iterate the canonical field-spec order and filter by
    membership — but a list rather than a set leaves room for user-defined
    ordering later without a migration.

    ``folded_repos`` is a JSON list of folded repo prefixes. Prefixes that
    are not currently registered are kept, so a repo whose containers are
    momentarily gone does not silently unfold.
    """

    __tablename__ = "view_prefs"

    frontend: str = Field(primary_key=True)
    columns: str | None = None
    folded_repos: str | None = None
