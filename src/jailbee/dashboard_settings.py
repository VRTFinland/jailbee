"""The TUI dashboard's settings overlay: which columns show, which repos fold.

A pure state machine plus a renderer, kept out of ``dashboard.py`` because
that module is already large and this is a self-contained concern. Nothing
here touches the terminal, the database or ``lifecycle``: the field
vocabulary and the set of dynamic columns are passed in, so the overlay can
be tested without building a container list.

Every transition returns a new ``SettingsState``. The run loop owns the
current one and writes it through to ``view_prefs`` on each change — there is
no OK/Cancel, because the live table is on screen behind the panel and
shows the effect immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from rich import box
from rich.panel import Panel

if TYPE_CHECKING:
    from rich.console import RenderableType

Tab = Literal["fields", "repos"]

# The overlay is drawn *below* the live table (see module docstring), so every
# row it draws is a line the table loses to `vertical_overflow="ellipsis"`
# cropping from the bottom. Reading the live console's height here would
# couple this pure state machine's renderer to the terminal, so instead the
# window is a fixed, conservative budget: 10 rows costs the panel roughly 16
# lines total (tabs + blank + 10 rows + blank + hint + border), which fits
# comfortably under a 24-line terminal alongside a small live table. This is
# a deliberate trade-off, not a placeholder — see Important 1 in the
# 2026-08-25 whole-branch review.
_VISIBLE_ROWS = 10


@dataclass(frozen=True)
class SettingsState:
    """An open settings overlay.

    ``field_names`` is the full column vocabulary in canonical order;
    ``enabled`` is a set because stored order is not significant (see
    :func:`enabled_names`). ``repo_prefixes`` is every group the user can
    reach — those on screen plus any folded prefix that is not currently
    present, so a repo whose containers are gone can still be unfolded.
    """

    tab: Tab
    field_names: tuple[str, ...]
    enabled: frozenset[str]
    repo_prefixes: tuple[str, ...]
    folded: frozenset[str]
    index: int = 0


def open_settings(
    *,
    field_names: tuple[str, ...],
    enabled: frozenset[str],
    repo_prefixes: tuple[str, ...],
    folded: frozenset[str],
) -> SettingsState:
    """A fresh overlay on the Fields tab, cursor at the top."""
    if not field_names:
        raise ValueError("settings overlay needs at least one field name")
    return SettingsState(
        tab="fields",
        field_names=field_names,
        enabled=enabled,
        repo_prefixes=repo_prefixes,
        folded=folded,
    )


def _rows(state: SettingsState) -> tuple[str, ...]:
    """The current tab's list."""
    return state.field_names if state.tab == "fields" else state.repo_prefixes


def move_settings(state: SettingsState, delta: int) -> SettingsState:
    """Move the cursor by ``delta`` within the current tab, clamped."""
    last = max(0, len(_rows(state)) - 1)
    return replace(state, index=max(0, min(last, state.index + delta)))


def switch_tab(state: SettingsState) -> SettingsState:
    """Flip between Fields and Repos, resetting the cursor.

    The two lists differ in length, so carrying the index across could leave
    the cursor past the end of the shorter one.
    """
    return replace(state, tab="repos" if state.tab == "fields" else "fields", index=0)


def toggle_current(state: SettingsState) -> SettingsState:
    """Flip the row under the cursor.

    Turning off the last enabled column is refused: there is no such thing as
    a table with zero columns, and a dashboard rendering none would look
    broken rather than configured. Every repo *can* be folded — the headers
    stay on screen, so nothing becomes unreachable.
    """
    rows = _rows(state)
    if not rows:
        return state
    name = rows[state.index]
    if state.tab == "fields":
        if name in state.enabled:
            if len(state.enabled) == 1:
                return state
            return replace(state, enabled=state.enabled - {name})
        return replace(state, enabled=state.enabled | {name})
    if name in state.folded:
        return replace(state, folded=state.folded - {name})
    return replace(state, folded=state.folded | {name})


def enabled_names(state: SettingsState) -> tuple[str, ...]:
    """The enabled columns in canonical order.

    Order comes from ``field_names``, never from the order the user clicked:
    the dashboards render in field-spec order and filter by membership, so a
    stored order that reflected clicks would imply a reordering feature that
    does not exist.
    """
    return tuple(n for n in state.field_names if n in state.enabled)


def _window_bounds(index: int, total: int, size: int) -> tuple[int, int]:
    """The ``[start, end)`` slice of ``size`` rows that keeps ``index`` visible.

    Scrolls the minimum amount to bring the cursor into view rather than
    always centering it, and clamps so the window never runs past either end
    of the list.
    """
    if total <= size:
        return 0, total
    start = max(0, index - size + 1)
    start = min(start, total - size)
    return start, start + size


def render_settings(state: SettingsState, *, dynamic: frozenset[str]) -> RenderableType:
    """The overlay as a bordered panel, drawn below the live table.

    ``dynamic`` names the columns whose ``show_if`` can prune them even when
    enabled. Those rows say so: an enabled column that does not appear would
    otherwise read as a bug rather than as the emptiness heuristic doing its
    job.

    Only a fixed window of rows around the cursor is drawn (see
    ``_VISIBLE_ROWS``) — drawing every row unconditionally would let a long
    field vocabulary grow the panel past the terminal height, and Rich crops
    a live render from the bottom, so an unwindowed panel loses its own
    bottom rows first with no way to scroll them back into view.
    """
    tabs = " ".join(
        f"[reverse bold] {label} [/]" if state.tab == tab else f" {label} "
        for tab, label in (("fields", "Fields"), ("repos", "Repos"))
    )
    lines = [tabs, ""]
    rows = _rows(state)
    total = len(rows)
    start, end = _window_bounds(state.index, total, _VISIBLE_ROWS)
    on = state.enabled if state.tab == "fields" else None
    if start > 0:
        lines.append(f"[dim]↑ {start} more[/dim]")
    for i in range(start, end):
        name = rows[i]
        checked = (name in on) if on is not None else (name not in state.folded)
        box_mark = "[bold green]x[/]" if checked else " "
        cursor = "[bold cyan]▸[/] " if i == state.index else "  "
        note = (
            "  [dim](shown only when it applies)[/dim]"
            if state.tab == "fields" and name in dynamic
            else ""
        )
        style = "bold bright_white" if i == state.index else ""
        text = f"[{style}]{name}[/]" if style else name
        lines.append(f"{cursor}[{box_mark}]  {text}{note}")
    if end < total:
        lines.append(f"[dim]↓ {total - end} more[/dim]")
    lines += ["", "[dim]↑/↓ move  ·  Space toggle  ·  Tab switch  ·  Esc close[/dim]"]
    return Panel(
        "\n".join(lines),
        title="[bold]settings[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        width=72,
    )
