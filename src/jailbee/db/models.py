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


class EgressOverride(SQLModel, table=True):
    """One host-local addition to a repo's egress allowlist.

    Host-local by design: these never reach a teammate, because they are not
    in `config.yaml`. Container-scope overrides do NOT live here — they are
    kept in the container's `user.jailbee.egress_extra` label so they die
    with the container (see `jailbee.egress_scope`).
    """

    __tablename__ = "egress_override"

    container_prefix: str = Field(primary_key=True, index=True)
    entry: str = Field(primary_key=True)
    added_at: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))


class RefreshState(SQLModel, table=True):
    """Per-repo bookkeeping: when did we last refresh and how did it go."""

    __tablename__ = "refresh_state"

    container_prefix: str = Field(primary_key=True)
    last_refresh_at: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))
    last_refresh_status: str  # "ok" | "dns_error" | "partial" | "acl_error"
    last_error_msg: str | None = None


class RegisteredRepo(SQLModel, table=True):
    """Repos the refresh timer iterates.

    Self-pruned when the config file is gone — unless `synthetic_config`,
    which marks a registration whose config was never a file at all (a
    directory relying on `global.yaml`'s `scratch:` block). Pruning those on
    "no config found" would let a scratch container's strict-mode ACL pool go
    stale, and strict is the default network mode.
    """

    __tablename__ = "registered_repo"

    container_prefix: str = Field(primary_key=True)
    repo_root: str
    registered_at: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))
    last_refresh_at: datetime | None = Field(
        default=None,
        sa_column=Column(_UTCDateTime, nullable=True),
    )
    synthetic_config: bool = Field(default=False)


JOB_CREATE = "create"
JOB_DESTROY = "destroy"
# `jailbee start` and `jailbee restart` share one kind: both boot a container
# and then run the same autostart, which is the part worth detaching.
JOB_BOOT = "boot"


class BackgroundJob(SQLModel, table=True):
    """A detached `jailbee new`, `jailbee destroy` or boot job in flight (or failed).

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

    Two concurrent dashboards of the *same* front-end (two `jailbee
    dashboard` processes, or two `jailbee gui` windows) share this one row
    with no merge: each write is a full overwrite, so whichever process
    saves last wins and the other's unsaved change is silently lost. This
    is not guarded against — an unusual setup, and the two front-ends
    already tolerate losing a concurrent write (see `db/view_prefs.py`).
    """

    __tablename__ = "view_prefs"

    frontend: str = Field(primary_key=True)
    columns: str | None = None
    folded_repos: str | None = None


class RepoUpgradeState(SQLModel, table=True):
    """Per-repo record of the version at which `base build` / `apply` last ran.

    Per repo, not global: both are repo-scoped operations (`golden.alias` is
    derived from `container_prefix`, `apply` runs from a repo root), so
    running `base build` in one repo must not silence another that has its
    own golden image.

    The `*_observed` flags separate a run jailbee actually saw from the
    assumption written when a repo is first seen. See `jailbee.upgrade` —
    the flag decides whether that version's own upgrade notes are considered
    satisfied: an observed run sets an exclusive lower bound (that version's
    notes are already satisfied), while an assumed one is inclusive (that
    version's notes are not yet covered).
    """

    __tablename__ = "repo_upgrade_state"

    container_prefix: str = Field(primary_key=True)
    base_build_version: str
    base_build_observed: bool
    apply_version: str
    apply_observed: bool
    updated_at: datetime = Field(sa_column=Column(_UTCDateTime, nullable=False))


class HostSetupState(SQLModel, table=True):
    """Host-level record of `jailbee setup` — one row, `id=1`.

    Unlike `RepoUpgradeState` this is not per repo: shell completions, the
    refresh timer and `~/.claude/skills` belong to the user's machine, not to
    any one checkout. Both timestamps are nullable and both are one-way:
    `setup_at` is set the first time `jailbee setup` runs anything, and
    `hint_shown_at` the one time the first-run hint is printed. Either one
    silences the hint for good — see `setup_command.consume_hint`.
    """

    __tablename__ = "host_setup_state"

    id: int = Field(default=1, primary_key=True)
    setup_at: datetime | None = Field(
        default=None,
        sa_column=Column(_UTCDateTime, nullable=True),
    )
    setup_version: str | None = None
    hint_shown_at: datetime | None = Field(
        default=None,
        sa_column=Column(_UTCDateTime, nullable=True),
    )
