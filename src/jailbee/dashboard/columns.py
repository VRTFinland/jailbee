"""Which dashboard columns are shown, how wide, and the stored column preferences.

Frontend-agnostic. The horizontal viewport geometry lives in
:mod:`jailbee.dashboard.viewport`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.text import Text

from jailbee import table_format
from jailbee.config import (
    DASHBOARD_DEFAULT_HIDE,
    ColumnConfig,
)
from jailbee.dashboard import format as dfmt
from jailbee.dashboard.model import RepoGroup, global_config_or_defaults
from jailbee.dashboard.viewport import column_viewport
from jailbee.db.view_prefs import ViewState, load_view_state, save_view_state
from jailbee.lifecycle import (
    ContainerInfo,
    ls_field_specs,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine


FieldSpecCI = table_format.FieldSpec[ContainerInfo]

# Changes to the dashboards' default column set that a stored view must follow,
# as (version, retired name, replacement names at its position). A stored view
# records the highest version it has been through in `view_prefs.columns_version`,
# so each rule runs once: a user who turns a retired column back on keeps it.
_COLUMN_SET_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "mem", ("mem_used", "mem_pct")),
    (1, "issues", ("outbox",)),
    (1, "doing", ()),
)
COLUMNS_VERSION = 1
"""The newest version in `_COLUMN_SET_MIGRATIONS`; a seeded view starts here."""


def migrate_column_set(
    columns: Sequence[str], from_version: int
) -> tuple[tuple[str, ...], list[str]]:
    """``columns`` with every rule newer than ``from_version`` applied.

    Returns the new set and one human-readable line per rule that changed it
    (empty when none did). A replacement takes the retired name's position, and
    a name the set already holds is not repeated.
    """
    result = list(columns)
    applied: list[str] = []
    for version, old, new in _COLUMN_SET_MIGRATIONS:
        if version <= from_version or old not in result:
            continue
        result = [n for name in result for n in (new if name == old else (name,))]
        applied.append(f"{old} → {' + '.join(new)}" if new else f"{old} removed")
    return tuple(dict.fromkeys(result)), applied


def column_set_migration_notice(applied: Sequence[str]) -> str:
    """The one notice a front-end shows after :func:`migrate_column_set` changed its view."""
    return (
        f"Dashboard columns updated: {', '.join(applied)}. "
        "Settings (S) can turn any of them back on."
    )


def seed_view_state(
    engine: Engine, frontend: str, *, on_migration: Callable[[str], None] | None = None
) -> ViewState:
    """``frontend``'s view state, seeding its columns on first use.

    The ``dashboard:`` config block is deprecated. It is read exactly once
    per front-end — here — so that upgrading changes nobody's columns, and is
    inert afterwards: a later edit to the YAML must not reach back into a
    front-end the user has since configured through its own UI.

    Only the **global** layer is consulted. The seeded value becomes a
    personal setting that applies in every repo, so seeding it from whichever
    repo the user happened to launch from first would let one repo's block
    silently define their view everywhere. A repo-level block is reported as
    deprecated *and* as not seeded by ``Config.validate_runtime``.

    A stored column set is filtered against :func:`all_column_names` on the
    way out, falling back to :func:`default_columns` if nothing survives —
    ``decode_names`` only validates JSON shape, not column vocabulary, so a
    renamed or removed column would otherwise reach both front-ends raw. The
    retired ``ahead_diff`` is migrated to ``target_diff`` with a visible notice
    before this filter, and that rename is written back so the notice
    appears once. The versioned column-set migration
    (``_COLUMN_SET_MIGRATIONS``) runs before the filter too: it is applied once
    per front-end, recorded in ``columns_version``, and raises one notice only
    when a rule changed the set. Each
    front-end's own last-column guard (``jailbee.dashboard.settings.toggle_setting``
    here, ``MainWindow._toggle_column`` in the Qt window) counts the *stored*
    length, so a phantom name inflates that count without ever being a real,
    keepable column — reaching zero real columns from a single ordinary
    toggle. Filtering here, before either guard sees the set, is what keeps
    that count honest.

    Apart from those two write-backs (the ``ahead_diff`` rename and the
    versioned column-set migration), this function never writes: the filtered
    value is only returned, not saved back over the stored row. That does **not** mean an unknown
    name survives in storage, though — the filtered value becomes the
    long-lived ``enabled`` / ``self._enabled_columns`` each front-end holds
    for the rest of the session, and *unrelated* actions save that same
    value verbatim (folding a repo group, in both the TUI and the Qt
    window, saves a `ViewState` built from it). So the first save triggered
    by anything, not just a columns edit, drops the unknown name from
    storage for good. A column removed in one release and reintroduced in
    a later one will not come back for a user who reopens the dashboard and
    triggers any such save in between. This is accepted, not an oversight:
    preserving it would mean threading an unfiltered set through both
    front-ends' save sites, or teaching :mod:`jailbee.db.view_prefs` the
    column vocabulary it deliberately knows nothing about, for a narrow
    scenario not judged worth that machinery.
    """
    state = load_view_state(engine, frontend)
    if state.columns is not None:
        notice = stored_column_migration_notice(state.columns)
        if notice is not None:
            if on_migration is not None:
                on_migration(notice)
            # Persist the rename alone, so the notice is shown once rather than
            # on every launch until some unrelated action saves the view. Only
            # `ahead_diff` is rewritten; other names keep the no-write rule.
            renamed = ("target_diff" if n == "ahead_diff" else n for n in state.columns)
            state = replace(state, columns=tuple(dict.fromkeys(renamed)))
            save_view_state(engine, frontend, state)
        if state.columns is not None and state.columns_version < COLUMNS_VERSION:
            migrated, applied = migrate_column_set(state.columns, state.columns_version)
            if applied and on_migration is not None:
                on_migration(column_set_migration_notice(applied))
            # Written back even when no rule matched, so the version check is
            # what stops a re-run — not the absence of the retired names.
            state = replace(
                state, columns=migrated or default_columns(), columns_version=COLUMNS_VERSION
            )
            save_view_state(engine, frontend, state)
        stored = state.columns or ()
        # Canonicalized *before* the filter: a stored set predating the
        # `claude_group` -> `group` rename holds a name `all_column_names` no
        # longer knows, and per this function's own contract the first save
        # after that drops it for good — so a user who had the column on would
        # silently and permanently lose it. The config-block half of the same
        # rename is handled in the loaders (`sanitize_column_blocks`); this is
        # the half that lives in the front-end's saved state instead.
        from jailbee.config.models_columns import canonical_ls_field

        known = frozenset(all_column_names())
        filtered = tuple(dict.fromkeys(c for n in stored if (c := canonical_ls_field(n)) in known))
        return replace(state, columns=filtered or default_columns())
    gcfg = global_config_or_defaults()
    seeded = replace(
        state,
        columns=enabled_from_column_config(gcfg.dashboard),
        columns_version=COLUMNS_VERSION,
    )
    save_view_state(engine, frontend, seeded)
    return seeded


def stored_column_migration_notice(columns: Sequence[str] | None) -> str | None:
    """Human-facing notice before a stored view's retired column is migrated."""
    if columns is not None and "ahead_diff" in columns:
        from jailbee.config.models_columns import RETIRED_DIFF_FIELD_NOTICE

        return f"Saved dashboard column: {RETIRED_DIFF_FIELD_NOTICE}; showing target_diff instead"
    return None


def default_columns() -> tuple[str, ...]:
    """The built-in dashboard column set, in canonical field-spec order.

    What a front-end renders before anyone has touched its settings, and the
    reset target. `DASHBOARD_DEFAULT_HIDE` names the columns the dashboards
    drop from the `ls` set: REPO is redundant under per-repo grouping, the
    wide GIT STATUS combo and the JSON-only full_name add noise, and TTL is
    folded into the NETWORK cell.
    """
    specs = ls_field_specs(now=datetime.now(UTC), all_repos=False)
    return tuple(
        f.name
        for f in specs
        if table_format.shows_by_default_in_dashboard(f) and f.name not in DASHBOARD_DEFAULT_HIDE
    )


def enabled_from_column_config(columns: ColumnConfig) -> tuple[str, ...]:
    """Resolve a legacy ``dashboard:`` block into an enabled-name tuple.

    The one remaining dashboard use of ``table_format.apply_column_config``,
    confined to seeding a front-end's `view_prefs` row from the deprecated
    config block (see ``seed_view_state``). Going through the old resolver is
    what guarantees the seeded set is *exactly* what that block used to
    render, including its two quirks: an explicit ``fields`` list wins
    outright, and ``hide`` replaces the built-in list rather than extending
    it.

    That guarantee holds fully for a ``fields:`` block — naming a column
    forces ``default_dashboard=True`` on it (see
    ``table_format.apply_column_config``), overriding whatever the current
    built-in default says. It does **not** hold for a ``hide:``-shaped
    block (``fields`` empty/absent): a column *not* named in ``hide``
    passes through with its current spec unchanged, so its inclusion here
    is decided by :func:`table_format.shows_by_default_in_dashboard` as it
    stands *today* — not as it stood when the block was written. IP left
    the dashboard defaults in this same release (Part 1), so a ``hide:``
    block that never mentioned ``ip`` seeds a set without it, even though
    that block used to render IP for its user.
    """
    resolved = table_format.apply_column_config(
        ls_field_specs(now=datetime.now(UTC), all_repos=False),
        fields=columns.fields,
        hide=columns.hide,
    )
    return tuple(f.name for f in resolved if table_format.shows_by_default_in_dashboard(f))


def all_column_names() -> tuple[str, ...]:
    """Every real column name, in canonical order — the Fields tab's list.

    The same vocabulary ``jailbee ls --fields`` accepts, including columns off
    by default in both views (``full_name``, ``git_status``, ``ip``, …): an
    enabled set decides inclusion by membership, so any of them can be turned
    on. ``repo`` is redundant under per-repo grouping but is not special-cased
    — the user may want it.
    """
    return tuple(f.name for f in ls_field_specs(now=datetime.now(UTC), all_repos=False))


def dynamic_column_names() -> frozenset[str]:
    """Columns whose ``show_if`` can prune them even when enabled.

    The settings overlay marks these so that an enabled column which does not
    appear reads as the emptiness heuristic working, not as a bug.
    """
    specs = ls_field_specs(now=datetime.now(UTC), all_repos=False)
    return frozenset(f.name for f in specs if f.show_if is not None)


def settings_repo_prefixes(groups: list[RepoGroup], folded: frozenset[str]) -> tuple[str, ...]:
    """The Repos tab's list: what is on screen, plus what is folded away.

    A folded repo whose containers have since gone draws no group at all, so
    listing only ``groups`` would leave it folded forever with no way back.
    Deduped, on-screen groups first, absent folded prefixes sorted after them.

    This is a snapshot taken once, when the overlay opens (see
    ``open_settings_overlay`` in ``run()``) — a repo registered or a
    container created/destroyed while the Repos tab is open does not appear
    or disappear from the list until the overlay is closed and reopened.
    """
    on_screen = [g.prefix for g in groups]
    return tuple(dict.fromkeys(on_screen + sorted(folded)))


def visible_fields(
    now: datetime,
    all_containers: list[ContainerInfo],
    enabled: Sequence[str] | None = None,
) -> list[FieldSpecCI]:
    return _select_visible_fields(now, all_containers, enabled, apply_conditions=True)


def _dashboard_cell_for(spec: FieldSpecCI, now: datetime) -> Callable[[ContainerInfo], str]:
    def cell(container: ContainerInfo) -> str:
        return dfmt.dashboard_cell(spec, container, now)

    return cell


def _select_visible_fields(
    now: datetime,
    all_containers: list[ContainerInfo],
    enabled: Sequence[str] | None,
    *,
    apply_conditions: bool,
) -> list[FieldSpecCI]:
    """Select enabled fields, optionally applying data-presence conditions.

    ``enabled`` is the front-end's enabled-name set; ``None`` means
    :func:`default_columns`. Membership decides inclusion — not
    ``default_table``, which is why a column off by default everywhere can
    be turned on here — and the field-spec list's own order decides
    rendering order, so a stored list's order is not significant.

    Qt's :func:`visible_fields` applies ``show_if`` on each render. The
    terminal applies it when taking a nonempty-column snapshot, then renders
    that snapshot without reevaluating conditions on refresh.

    Unknown names are skipped rather than rejected — a stored set can outlive
    a renamed column, and view state must not break the view.

    Returned field specs wrap lifecycle values with dashboard-only compact
    cells and labels. The standalone TTL column stays excluded from defaults.

    Shared by the terminal's snapshot selection and both Qt views.
    """

    wanted = frozenset(default_columns() if enabled is None else enabled)
    fields = [
        field_spec
        for field_spec in ls_field_specs(now=now, all_repos=False)
        if field_spec.name in wanted
        and (
            not apply_conditions or field_spec.show_if is None or field_spec.show_if(all_containers)
        )
    ]
    widths = {"state": 2, "mem": 15, "mem_used": 6, "mem_pct": 4, "outbox": 3}
    return [
        replace(
            field_spec,
            header=dfmt.dashboard_header(field_spec),
            cell=_dashboard_cell_for(field_spec, now),
            dashboard_min_width=widths.get(field_spec.name, field_spec.dashboard_min_width),
        )
        for field_spec in fields
    ]


@dataclass(frozen=True)
class TableWindow:
    """Rows ``[start, stop)`` are drawn; the counts feed the "more" markers."""

    start: int
    stop: int
    hidden_above: int
    hidden_below: int


def window_rows(heights: Sequence[int], cursor: int | None, budget: int) -> TableWindow:
    """The rows to draw in ``budget`` lines so that row ``cursor`` is visible.

    Used by the CLI's submodule picker (``cli.py``); the dashboard scrolls a
    widget instead. ``heights`` are each row's rendered line count. A hidden
    end costs one line, which the caller fills with its own "more" marker
    (the counts are in ``hidden_above`` / ``hidden_below``). The window is
    derived from the cursor alone: pinned to the top while the cursor fits there, to
    the bottom near the end, centred otherwise. A cursor row taller than the
    whole budget is still returned; the caller must clip it.
    """
    count = len(heights)
    if sum(heights) <= budget:
        return TableWindow(0, count, 0, 0)
    anchor = 0 if cursor is None else cursor

    stop, used = 0, 0
    while stop < count and used + heights[stop] <= budget - 1:
        used += heights[stop]
        stop += 1
    if anchor < stop:
        return TableWindow(0, stop, 0, count - stop)

    start, used = count, 0
    while start > 0 and used + heights[start - 1] <= budget - 1:
        start -= 1
        used += heights[start]
    if anchor >= start:
        return TableWindow(start, count, start, 0)

    inner = budget - 2
    start, stop, used = anchor, anchor + 1, heights[anchor]
    grew = True
    while grew:
        grew = False
        if stop < count and used + heights[stop] <= inner:
            used += heights[stop]
            stop += 1
            grew = True
        if start > 0 and used + heights[start - 1] <= inner:
            start -= 1
            used += heights[start]
            grew = True
    return TableWindow(start, stop, start, count - stop)


_DASHBOARD_COLUMN_BUDGETS = {
    "name": 18,
    "state": 2,
    "network": 3,
    "created": 5,
    "mem": 15,
    "mem_used": 6,
    "mem_pct": 4,
    "mode": 5,
    "wt": 9,
    "ahead_count": 3,
    "behind_count": 3,
    "conflict": 8,
    "pr": 6,
    "issues": 3,
    "outbox": 3,
    "full_name": 28,
    "repo": 16,
    "base": 20,
    "loose_until": 20,
    "ip": 15,
    "memory_limit": 14,
    "group": 16,
    "git_status": 36,
    "local_diff": 16,
    "target_diff": 16,
}


def nonempty_columns(
    groups: list[RepoGroup],
    *,
    now: datetime,
    enabled: Sequence[str] | None = None,
    folded: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Snapshot meaningful selected fields in the currently unfolded repos."""
    containers = [c for g in groups if g.prefix not in folded for c in g.containers]
    fields = _select_visible_fields(now, containers, enabled, apply_conditions=True)
    shown = tuple(
        f.name
        for f in fields
        if any(Text.from_markup(f.cell(c)).plain.strip() not in ("", "-", "—") for c in containers)
    )
    if shown:
        return shown
    selected = _select_visible_fields(now, containers, enabled, apply_conditions=False)
    return ("name",) if selected else ()


def optimize_column_widths(
    groups: list[RepoGroup],
    *,
    now: datetime,
    enabled: Sequence[str] | None = None,
    folded: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """Snapshot visible cell budgets, keyed by field with no row indentation."""
    expanded = [g for g in groups if g.prefix not in folded]
    containers = [c for g in expanded for c in g.containers]
    fields = _select_visible_fields(
        now,
        containers,
        nonempty_columns(groups, now=now, enabled=enabled, folded=folded),
        apply_conditions=False,
    )
    widths: dict[str, int] = {}
    for spec in fields:
        cells = max(
            (
                Text.from_markup(
                    c.name if spec.name == "name" and g.repo_root is None else spec.cell(c)
                ).cell_len
                for g in expanded
                for c in g.containers
            ),
            default=0,
        )
        cells = max(cells, spec.dashboard_min_width)
        if spec.dashboard_max_width is not None:
            cells = min(cells, spec.dashboard_max_width)
        widths[spec.name] = max(cells, Text.from_markup(spec.header).cell_len)
    return widths


def _dashboard_column_widths(
    fields: list[FieldSpecCI], overrides: Mapping[str, int] | None = None
) -> tuple[int, ...]:
    """Choose stable per-column budgets from field metadata and headers."""
    widths: list[int] = []
    for index, field_spec in enumerate(fields):
        cells = max(
            _DASHBOARD_COLUMN_BUDGETS.get(field_spec.name, 0),
            field_spec.dashboard_min_width,
            Text.from_markup(field_spec.header).cell_len,
        )
        if field_spec.dashboard_max_width is not None:
            cells = min(cells, field_spec.dashboard_max_width)
        if overrides is not None and field_spec.name in overrides:
            cells = overrides[field_spec.name]
        widths.append(cells + (2 if index == 0 else 0))
    return tuple(widths)


def _frame_columns(
    groups: list[RepoGroup],
    *,
    now: datetime,
    enabled: Sequence[str] | None,
    folded: frozenset[str],
    column_widths: Mapping[str, int] | None,
    shown_columns: Sequence[str] | None,
) -> tuple[list[FieldSpecCI], tuple[int, ...]]:
    """The frame's columns and their budgets: what :func:`render` lays out.

    Shared with the key loop's offset clamp, so both see the same columns.
    """
    visible = [c for g in groups if g.prefix not in folded for c in g.containers]
    fields = _select_visible_fields(
        now,
        visible,
        nonempty_columns(groups, now=now, enabled=enabled, folded=folded)
        if shown_columns is None
        else shown_columns,
        apply_conditions=False,
    )
    return fields, _dashboard_column_widths(fields, column_widths)


def clamp_column_offset(
    groups: list[RepoGroup],
    offset: int,
    *,
    now: datetime,
    enabled: Sequence[str] | None,
    folded: frozenset[str],
    column_widths: Mapping[str, int] | None,
    shown_columns: Sequence[str] | None,
    available: int,
) -> int:
    """Clamp the session offset to the frame's scrollable column geometry."""
    _, widths = _frame_columns(
        groups,
        now=now,
        enabled=enabled,
        folded=folded,
        column_widths=column_widths,
        shown_columns=shown_columns,
    )
    return column_viewport(widths, available, offset).offset
