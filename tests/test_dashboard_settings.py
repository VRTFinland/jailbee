"""Tests for the TUI dashboard's settings overlay state machine."""

from __future__ import annotations

import pytest
from rich.console import Console

FIELDS = ("name", "state", "network", "pr", "ip")
REPOS = ("alpha", "beta")


def _state(**over):
    from jailbee.dashboard_settings import open_settings

    kwargs = dict(
        field_names=FIELDS,
        enabled=frozenset({"name", "state"}),
        repo_prefixes=REPOS,
        folded=frozenset({"beta"}),
    )
    kwargs.update(over)
    return open_settings(**kwargs)


def test_opens_on_the_fields_tab_at_the_top():
    state = _state()
    assert state.tab == "fields"
    assert state.index == 0


def test_move_clamps_within_the_current_tabs_list():
    from jailbee.dashboard_settings import move_settings

    state = _state()
    assert move_settings(state, -1).index == 0  # clamp at top
    assert move_settings(state, 1).index == 1
    bottom = _state()
    for _ in range(len(FIELDS) + 3):
        bottom = move_settings(bottom, 1)
    assert bottom.index == len(FIELDS) - 1  # clamp at bottom, not past it


def test_switching_tab_resets_the_cursor():
    """The two lists have different lengths, so carrying an index across
    would let the cursor land past the end of the shorter one."""
    from jailbee.dashboard_settings import move_settings, switch_tab

    state = move_settings(_state(), 3)
    assert state.index == 3
    switched = switch_tab(state)
    assert switched.tab == "repos"
    assert switched.index == 0
    assert switch_tab(switched).tab == "fields"


def test_toggle_flips_the_field_under_the_cursor():
    from jailbee.dashboard_settings import toggle_current

    state = _state()  # cursor on "name", which is enabled
    flipped = toggle_current(state)
    assert "name" not in flipped.enabled
    assert "name" in toggle_current(flipped).enabled


def test_toggle_flips_the_repo_under_the_cursor():
    from jailbee.dashboard_settings import switch_tab, toggle_current

    state = switch_tab(_state())  # repos tab, cursor on "alpha", unfolded
    flipped = toggle_current(state)
    assert "alpha" in flipped.folded
    assert "beta" in flipped.folded  # untouched


def test_enabled_names_is_canonical_order_not_toggle_order():
    """Stored order must not depend on the order the user happened to click,
    because rendering order comes from the field-spec list either way."""
    from jailbee.dashboard_settings import enabled_names, move_settings, toggle_current

    state = _state(enabled=frozenset({"name"}))
    state = toggle_current(move_settings(state, 4))  # enable "ip"
    state = toggle_current(move_settings(state, -3))  # enable "state"
    assert enabled_names(state) == ("name", "state", "ip")


def test_the_last_enabled_field_cannot_be_turned_off():
    """There is no such thing as a table with zero columns, and a dashboard
    that rendered none would look broken rather than configured."""
    from jailbee.dashboard_settings import toggle_current

    state = _state(enabled=frozenset({"name"}))  # cursor on the only one
    assert toggle_current(state).enabled == frozenset({"name"})


def test_render_marks_state_and_flags_dynamic_columns():
    from jailbee.dashboard_settings import render_settings

    console = Console(width=90, no_color=True)
    with console.capture() as cap:
        console.print(render_settings(_state(), dynamic=frozenset({"pr"})))
    out = cap.get()

    assert "name" in out and "state" in out
    assert "only when it applies" in out  # the `pr` row explains its pruning
    assert "Fields" in out and "Repos" in out  # both tabs are discoverable


def test_render_repos_tab_shows_folded_state():
    """Repos tab shows folded state: unchecked ([ ]) if folded, checked ([x]) if not.

    A checkbox semantically reads as "shown", so folded means unchecked. The
    fixture has beta folded and alpha unfolded, so the test must assert that
    beta's marker is [ ] and alpha's is [x] — just checking that both names
    appear in the output would pass under inverted polarity."""
    from jailbee.dashboard_settings import render_settings, switch_tab

    console = Console(width=90, no_color=True)
    with console.capture() as cap:
        console.print(render_settings(switch_tab(_state()), dynamic=frozenset()))
    out = cap.get()

    lines = out.split("\n")
    alpha_line = next(line for line in lines if " alpha " in line)
    beta_line = next(line for line in lines if " beta " in line)

    # alpha is not folded → checkbox is [x]
    assert "[x]  alpha" in alpha_line
    # beta is folded → checkbox is [ ]
    assert "[ ]  beta" in beta_line


def test_render_windows_a_long_field_vocabulary():
    """A vocabulary longer than the visible window must not draw every row —
    that's exactly the unbounded-height bug the whole-branch review found:
    the overlay drew all 22+ fields below the live table and Rich's
    bottom-cropping ate the overlay first. This fails against the old
    unwindowed renderer, which prints every one of the 40 names."""
    from jailbee.dashboard_settings import render_settings

    many_fields = tuple(f"field{i}" for i in range(40))
    state = _state(field_names=many_fields, enabled=frozenset({"field0"}))
    console = Console(width=90, no_color=True)
    with console.capture() as cap:
        console.print(render_settings(state, dynamic=frozenset()))
    out = cap.get()

    shown = sum(1 for name in many_fields if name in out)
    assert shown < len(many_fields)


def test_render_keeps_the_cursor_row_visible_near_the_end_of_a_long_list():
    """The row under the cursor must always be in the rendered output. This
    is the one that fails today: with the cursor near the bottom of a long
    list, the unwindowed renderer still draws every row (so it would
    trivially pass), but a windowed renderer that doesn't scroll to follow
    the cursor would clip exactly this row."""
    from jailbee.dashboard_settings import move_settings, render_settings

    many_fields = tuple(f"field{i}" for i in range(40))
    state = _state(field_names=many_fields, enabled=frozenset({"field0"}))
    state = move_settings(state, 37)  # cursor near the end of the list
    assert state.index == 37

    console = Console(width=90, no_color=True)
    with console.capture() as cap:
        console.print(render_settings(state, dynamic=frozenset()))
    out = cap.get()

    assert "field37" in out


def test_render_shows_continuation_markers_only_when_something_is_hidden():
    from jailbee.dashboard_settings import move_settings, render_settings

    many_fields = tuple(f"field{i}" for i in range(40))
    state = _state(field_names=many_fields, enabled=frozenset({"field0"}))

    console = Console(width=90, no_color=True)
    with console.capture() as cap:
        console.print(render_settings(state, dynamic=frozenset()))
    top_lines = cap.get().split("\n")
    assert not any("more" in line and "↑" in line for line in top_lines)
    assert any("more" in line and "↓" in line for line in top_lines)

    bottom = move_settings(state, 39)
    with console.capture() as cap:
        console.print(render_settings(bottom, dynamic=frozenset()))
    bottom_lines = cap.get().split("\n")
    assert any("more" in line and "↑" in line for line in bottom_lines)
    assert not any("more" in line and "↓" in line for line in bottom_lines)


def test_open_settings_rejects_an_empty_field_vocabulary():
    """A guard against a caller that resolved its field list wrongly: an
    empty overlay is indistinguishable from a broken one."""
    from jailbee.dashboard_settings import open_settings

    with pytest.raises(ValueError):
        open_settings(field_names=(), enabled=frozenset(), repo_prefixes=REPOS, folded=frozenset())
