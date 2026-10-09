"""The TUI dashboard's settings overlay: which columns show, which repos fold.

Pure data, separate from ``dashboard.tui.app`` because this is a
self-contained concern: the rows each tab lists and the toggle that flips
one. Drawing, the cursor and the current tab belong to the settings widget
(``tui.native.SettingsBox``). Nothing here touches the terminal, the
database or ``lifecycle``: the field vocabulary is passed in, so the overlay
can be tested without building a container list.

Every transition returns a new ``SettingsState``. The run loop owns the
current one and writes it through to ``view_prefs`` on each change — there is
no OK/Cancel, because the live table is on screen behind the panel and
shows the effect immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

Tab = Literal["fields", "repos", "visibility"]

TABS: tuple[tuple[Tab, str], ...] = (
    ("fields", "Fields"),
    ("repos", "Repos"),
    ("visibility", "Visibility"),
)
# The Visibility tab's first row. NUL can never start a repo prefix.
SHOW_EMPTY = "\x00show-empty"
# The row's identity and the frozen column: always on, always first.
LOCKED_FIELD = "name"

# The cursor row's style for every TUI dashboard surface: container rows, repo
# headings, action menus and this overlay. It lives here, the lowest module that
# draws a cursor, so every dashboard module can import it. A background band, so
# it reads as "the selected row" and never as a heading's resting colour (cyan,
# yellow); the mouse hover is only an underline (``dashboard.hit.HOVER_STYLE``),
# so the two never look alike. Container rows and headings carry no other cursor marker.
CURSOR_STYLE = "bold on grey30"


@dataclass(frozen=True)
class SettingsState:
    """An open settings overlay (the tab and cursor are the settings widget's).

    ``field_names`` is the full column vocabulary in canonical order;
    ``enabled`` is the column order, ``name`` first
    (:data:`LOCKED_FIELD`). ``repo_prefixes`` is every group the user can
    reach — those on screen plus any folded prefix that is not currently
    present, so a repo whose containers are gone can still be unfolded.
    ``visibility_repo_prefixes`` likewise retains every prefix available for
    visibility choices, including currently hidden or empty repositories.
    """

    field_names: tuple[str, ...]
    enabled: tuple[str, ...]
    repo_prefixes: tuple[str, ...]
    folded: frozenset[str]
    visibility_repo_prefixes: tuple[str, ...]
    show_empty_repos: bool
    hidden_repos: frozenset[str]


def open_settings(
    *,
    field_names: tuple[str, ...],
    enabled: tuple[str, ...],
    repo_prefixes: tuple[str, ...],
    folded: frozenset[str],
    visibility_repo_prefixes: tuple[str, ...] = (),
    show_empty_repos: bool = True,
    hidden_repos: frozenset[str] = frozenset(),
) -> SettingsState:
    """A fresh overlay."""
    if not field_names:
        raise ValueError("settings overlay needs at least one field name")
    return SettingsState(
        field_names=field_names,
        enabled=enabled,
        repo_prefixes=repo_prefixes,
        folded=folded,
        visibility_repo_prefixes=visibility_repo_prefixes,
        show_empty_repos=show_empty_repos,
        hidden_repos=hidden_repos,
    )


@dataclass(frozen=True)
class SettingRow:
    key: str
    label: str
    checked: bool


def setting_rows(state: SettingsState, tab: Tab) -> tuple[SettingRow, ...]:
    """One tab's rows: fields shown, repos unfolded, repos visible (after "Show empty repos")."""
    if tab == "fields":
        enabled = [n for n in state.enabled if n in state.field_names]
        disabled = sorted(n for n in state.field_names if n not in state.enabled)
        return tuple(
            SettingRow(n, f"{n} (always first)" if n == LOCKED_FIELD else n, n in state.enabled)
            for n in (*enabled, *disabled)
        )
    if tab == "repos":
        return tuple(SettingRow(p, p, p not in state.folded) for p in state.repo_prefixes)
    return (
        SettingRow(SHOW_EMPTY, "Show empty repos", state.show_empty_repos),
        *(SettingRow(p, p, p not in state.hidden_repos) for p in state.visibility_repo_prefixes),
    )


def toggle_setting(state: SettingsState, tab: Tab, key: str) -> SettingsState:
    """Flip one row; ``state`` unchanged for an unknown key or the last enabled column.

    Turning off the last enabled column is refused: there is no such thing as
    a table with zero columns, and a dashboard rendering none would look
    broken rather than configured. Every repo *can* be folded — the headers
    stay on screen, so nothing becomes unreachable.
    """
    if tab == "fields":
        if key not in state.field_names or key == LOCKED_FIELD:
            return state
        if key in state.enabled:
            if len(state.enabled) == 1:
                return state
            return replace(state, enabled=tuple(n for n in state.enabled if n != key))
        return replace(state, enabled=(*state.enabled, key))
    if tab == "repos":
        if key not in state.repo_prefixes:
            return state
        return replace(state, folded=state.folded ^ {key})
    if key == SHOW_EMPTY:
        return replace(state, show_empty_repos=not state.show_empty_repos)
    if key not in state.visibility_repo_prefixes:
        return state
    return replace(state, hidden_repos=state.hidden_repos ^ {key})


def next_tab(tab: Tab) -> Tab:
    """The tab after ``tab``, wrapping round."""
    order = [t for t, _ in TABS]
    return order[(order.index(tab) + 1) % len(order)]


def move_field(state: SettingsState, key: str, step: int) -> SettingsState:
    """Move an enabled field ``step`` places; unchanged past ``name`` or the enabled block."""
    if key == LOCKED_FIELD or key not in state.enabled:
        return state
    order = list(state.enabled)
    index = order.index(key)
    target = index + step
    floor = 1 if order and order[0] == LOCKED_FIELD else 0
    if not floor <= target < len(order):
        return state
    order.insert(target, order.pop(index))
    return replace(state, enabled=tuple(order))


def enabled_names(state: SettingsState) -> tuple[str, ...]:
    """The enabled columns in the user's order."""
    return state.enabled
