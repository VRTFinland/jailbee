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

# The cursor row's text style for every TUI dashboard surface: container
# rows, repo headings, action menus and this overlay. It lives here, the
# lowest module that draws a cursor, so every dashboard module can import it. It must
# stay distinct from the headings' resting colours (cyan, yellow). Container
# rows and headings carry no other cursor marker.
CURSOR_STYLE = "bold magenta"


@dataclass(frozen=True)
class SettingsState:
    """An open settings overlay (the tab and cursor are the settings widget's).

    ``field_names`` is the full column vocabulary in canonical order;
    ``enabled`` is a set because stored order is not significant (see
    :func:`enabled_names`). ``repo_prefixes`` is every group the user can
    reach — those on screen plus any folded prefix that is not currently
    present, so a repo whose containers are gone can still be unfolded.
    ``visibility_repo_prefixes`` likewise retains every prefix available for
    visibility choices, including currently hidden or empty repositories.
    """

    field_names: tuple[str, ...]
    enabled: frozenset[str]
    repo_prefixes: tuple[str, ...]
    folded: frozenset[str]
    visibility_repo_prefixes: tuple[str, ...]
    show_empty_repos: bool
    hidden_repos: frozenset[str]


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
        return tuple(SettingRow(n, n, n in state.enabled) for n in state.field_names)
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
        if key not in state.field_names:
            return state
        if key in state.enabled:
            return (
                state if len(state.enabled) == 1 else replace(state, enabled=state.enabled - {key})
            )
        return replace(state, enabled=state.enabled | {key})
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


def enabled_names(state: SettingsState) -> tuple[str, ...]:
    """The enabled columns in canonical order.

    Order comes from ``field_names``, never from the order the user clicked:
    the dashboards render in field-spec order and filter by membership, so a
    stored order that reflected clicks would imply a reordering feature that
    does not exist.
    """
    return tuple(n for n in state.field_names if n in state.enabled)
