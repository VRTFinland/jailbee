"""Tests for the TUI dashboard's settings overlay data: rows, toggles, tabs."""

from __future__ import annotations

import pytest

import jailbee.dashboard.settings as ds

FIELDS = ("name", "state", "network", "pr", "ip")
REPOS = ("alpha", "beta")
VISIBILITY_REPOS = ("alpha", "beta", "gamma")


def _state(**over):  # type: ignore[no-untyped-def]
    kwargs = dict(
        field_names=FIELDS,
        enabled=frozenset({"name", "state"}),
        repo_prefixes=REPOS,
        folded=frozenset({"beta"}),
        visibility_repo_prefixes=VISIBILITY_REPOS,
        show_empty_repos=True,
        hidden_repos=frozenset(),
    )
    kwargs.update(over)
    return ds.open_settings(**kwargs)


def test_setting_rows_per_tab():
    state = _state(hidden_repos=frozenset({"alpha"}), visibility_repo_prefixes=REPOS)
    assert ds.setting_rows(state, "fields") == (
        ds.SettingRow("name", "name", True),
        ds.SettingRow("state", "state", True),
        ds.SettingRow("network", "network", False),
        ds.SettingRow("pr", "pr", False),
        ds.SettingRow("ip", "ip", False),
    )
    assert ds.setting_rows(state, "repos") == (
        ds.SettingRow("alpha", "alpha", True),
        ds.SettingRow("beta", "beta", False),
    )
    assert ds.setting_rows(state, "visibility") == (
        ds.SettingRow(ds.SHOW_EMPTY, "Show empty repos", True),
        ds.SettingRow("alpha", "alpha", False),
        ds.SettingRow("beta", "beta", True),
    )


def test_toggle_setting_flips_one_row_per_tab():
    state = _state(hidden_repos=frozenset({"alpha"}))
    assert ds.toggle_setting(state, "fields", "pr").enabled == {"name", "state", "pr"}
    assert ds.toggle_setting(state, "repos", "beta").folded == frozenset()
    assert ds.toggle_setting(state, "visibility", ds.SHOW_EMPTY).show_empty_repos is False
    assert ds.toggle_setting(state, "visibility", "alpha").hidden_repos == frozenset()


def test_the_last_column_cannot_be_turned_off():
    state = _state(enabled=frozenset({"name"}))
    assert ds.toggle_setting(state, "fields", "name") == state


def test_an_unknown_key_changes_nothing():
    state = _state()
    assert ds.toggle_setting(state, "fields", "nope") == state
    assert ds.toggle_setting(state, "repos", ds.SHOW_EMPTY) == state
    assert ds.toggle_setting(state, "visibility", "nope") == state


def test_next_tab_cycles():
    assert [ds.next_tab(t) for t, _ in ds.TABS] == ["repos", "visibility", "fields"]


def test_show_empty_can_never_be_a_repo_prefix():
    assert ds.SHOW_EMPTY.startswith("\x00")


def test_toggle_flips_the_field_both_ways():
    state = _state()
    flipped = ds.toggle_setting(state, "fields", "name")
    assert "name" not in flipped.enabled
    assert "name" in ds.toggle_setting(flipped, "fields", "name").enabled


def test_toggle_flips_the_repo_and_leaves_the_others():
    flipped = ds.toggle_setting(_state(), "repos", "alpha")  # alpha was unfolded
    assert flipped.folded == frozenset({"alpha", "beta"})


def test_visibility_global_toggle_only_changes_show_empty_repos():
    """A missing first-row branch would toggle a made-up repo instead."""
    state = _state(hidden_repos=frozenset({"beta"}))
    toggled = ds.toggle_setting(state, "visibility", ds.SHOW_EMPTY)
    assert not toggled.show_empty_repos
    assert toggled.hidden_repos == frozenset({"beta"})


def test_visibility_repo_toggle_only_changes_the_selected_hidden_prefix():
    """A prefix toggle must not alter the independent global empty setting."""
    toggled = ds.toggle_setting(_state(show_empty_repos=False), "visibility", "alpha")
    assert toggled.show_empty_repos is False
    assert toggled.hidden_repos == frozenset({"alpha"})


def test_visibility_toggles_do_not_change_folded_repos():
    """Visibility and folding remain distinct preference families."""
    toggled = ds.toggle_setting(_state(), "visibility", "alpha")
    assert toggled.folded == frozenset({"beta"})


def test_visibility_keeps_hidden_repos_listed_when_empty_repos_are_off():
    """Turning off empty groups must not remove their restoration control."""
    state = _state(show_empty_repos=False, hidden_repos=frozenset({"gamma"}))
    assert ds.SettingRow("gamma", "gamma", False) in ds.setting_rows(state, "visibility")


def test_visibility_rows_follow_the_checkbox_polarity():
    """Checked means shown: a hidden repo and a disabled "show empty" are unchecked."""
    state = _state(show_empty_repos=False, hidden_repos=frozenset({"beta"}))
    checked = {row.label: row.checked for row in ds.setting_rows(state, "visibility")}
    assert checked == {
        "Show empty repos": False,
        "alpha": True,
        "beta": False,
        "gamma": True,
    }


def test_repos_rows_follow_the_checkbox_polarity():
    """Folded means unchecked; checking only that both names appear would pass inverted."""
    checked = {row.label: row.checked for row in ds.setting_rows(_state(), "repos")}
    assert checked == {"alpha": True, "beta": False}


def test_enabled_names_is_canonical_order_not_toggle_order():
    """Stored order must not depend on the order the user happened to click,
    because rendering order comes from the field-spec list either way."""
    state = _state(enabled=frozenset({"name"}))
    state = ds.toggle_setting(state, "fields", "ip")
    state = ds.toggle_setting(state, "fields", "state")
    assert ds.enabled_names(state) == ("name", "state", "ip")


def test_open_settings_rejects_an_empty_field_vocabulary():
    """A guard against a caller that resolved its field list wrongly: an
    empty overlay is indistinguishable from a broken one."""
    with pytest.raises(ValueError):
        ds.open_settings(
            field_names=(), enabled=frozenset(), repo_prefixes=REPOS, folded=frozenset()
        )
