"""The TUI dashboard's settings overlay: which columns show, which repos fold.

A pure state machine plus a renderer, separate from ``dashboard.tui.loop``
because this is a self-contained concern. Nothing
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

from jailbee.dashboard import hit as dhit

if TYPE_CHECKING:
    from rich.console import RenderableType

Tab = Literal["fields", "repos", "visibility"]

# The cursor row's text style for every TUI dashboard surface: container
# rows, repo headings, action menus and this overlay. It lives here, the
# lowest module that draws a cursor, so every dashboard module can import it. It must
# stay distinct from the headings' resting colours (cyan, yellow). Container
# rows and headings carry no other cursor marker.
CURSOR_STYLE = "bold magenta"

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
    ``visibility_repo_prefixes`` likewise retains every prefix available for
    visibility choices, including currently hidden or empty repositories.
    """

    tab: Tab
    field_names: tuple[str, ...]
    enabled: frozenset[str]
    repo_prefixes: tuple[str, ...]
    folded: frozenset[str]
    visibility_repo_prefixes: tuple[str, ...]
    show_empty_repos: bool
    hidden_repos: frozenset[str]
    index: int = 0


def open_settings(
    *,
    field_names: tuple[str, ...],
    enabled: frozenset[str],
    repo_prefixes: tuple[str, ...],
    folded: frozenset[str],
    visibility_repo_prefixes: tuple[str, ...] = (),
    show_empty_repos: bool = True,
    hidden_repos: frozenset[str] = frozenset(),
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
        visibility_repo_prefixes=visibility_repo_prefixes,
        show_empty_repos=show_empty_repos,
        hidden_repos=hidden_repos,
    )


def _rows(state: SettingsState) -> tuple[str, ...]:
    """The current tab's list."""
    if state.tab == "fields":
        return state.field_names
    if state.tab == "repos":
        return state.repo_prefixes
    return state.visibility_repo_prefixes


def _row_count(state: SettingsState) -> int:
    """Number of selectable rows on the current tab."""
    return len(_rows(state)) + (1 if state.tab == "visibility" else 0)


def move_settings(state: SettingsState, delta: int) -> SettingsState:
    """Move the cursor by ``delta`` within the current tab, clamped."""
    last = max(0, _row_count(state) - 1)
    return replace(state, index=max(0, min(last, state.index + delta)))


def switch_tab(state: SettingsState) -> SettingsState:
    """Cycle Fields, Repos and Visibility, resetting the cursor.

    The tab lists differ in length, so carrying the index across could leave
    the cursor past the end of the shorter one.
    """
    if state.tab == "fields":
        return replace(state, tab="repos", index=0)
    if state.tab == "repos":
        return replace(state, tab="visibility", index=0)
    return replace(state, tab="fields", index=0)


def toggle_current(state: SettingsState) -> SettingsState:
    """Flip the row under the cursor.

    Turning off the last enabled column is refused: there is no such thing as
    a table with zero columns, and a dashboard rendering none would look
    broken rather than configured. Every repo *can* be folded — the headers
    stay on screen, so nothing becomes unreachable.
    """
    if state.tab == "fields":
        rows = _rows(state)
        if not rows:
            return state
        name = rows[state.index]
        if name in state.enabled:
            if len(state.enabled) == 1:
                return state
            return replace(state, enabled=state.enabled - {name})
        return replace(state, enabled=state.enabled | {name})
    if state.tab == "repos":
        rows = _rows(state)
        if not rows:
            return state
        name = rows[state.index]
        if name in state.folded:
            return replace(state, folded=state.folded - {name})
        return replace(state, folded=state.folded | {name})
    if state.index == 0:
        return replace(state, show_empty_repos=not state.show_empty_repos)
    name = state.visibility_repo_prefixes[state.index - 1]
    if name in state.hidden_repos:
        return replace(state, hidden_repos=state.hidden_repos - {name})
    return replace(state, hidden_repos=state.hidden_repos | {name})


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
        dhit.hit_markup(
            f"[reverse bold] {label} [/]" if state.tab == tab else f" {label} ",
            "tab",
            tab,
        )
        for tab, label in (
            ("fields", "Fields"),
            ("repos", "Repos"),
            ("visibility", "Visibility"),
        )
    )
    lines = [tabs, ""]
    rows = _rows(state)
    total = _row_count(state)
    start, end = _window_bounds(state.index, total, _VISIBLE_ROWS)
    if start > 0:
        lines.append(f"[dim]↑ {start} more[/dim]")
    for i in range(start, end):
        if state.tab == "visibility" and i == 0:
            name = "Show empty repos"
            checked = state.show_empty_repos
        else:
            row_index = i - 1 if state.tab == "visibility" else i
            name = rows[row_index]
            if state.tab == "fields":
                checked = name in state.enabled
            elif state.tab == "repos":
                checked = name not in state.folded
            else:
                checked = name not in state.hidden_repos
        box_mark = "[bold green]x[/]" if checked else " "
        cursor = "[bold cyan]▸[/] " if i == state.index else "  "
        note = (
            "  [dim](shown only when it applies)[/dim]"
            if state.tab == "fields" and name in dynamic
            else ""
        )
        style = CURSOR_STYLE if i == state.index else ""
        text = f"[{style}]{name}[/]" if style else name
        line = f"{cursor}[{box_mark}]  {text}{note}"
        lines.append(dhit.hit_markup(line, "setting", i))
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
