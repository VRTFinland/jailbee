"""The editor's pure state machine.

Modelled on `test_dashboard_settings.py`: every transition is a function
from state to state, so the whole interaction model is testable without a
terminal.
"""

from __future__ import annotations

from jailbee.config_edit import state as st
from jailbee.config_edit.layers import Origin
from jailbee.config_edit.schema import FieldKind, FieldSpec
from jailbee.config_writer import DELETE, YamlChange


def _spec(dotted, kind=FieldKind.BOOL, default=False, description="help", advanced=True):
    path = tuple(dotted.split("."))
    return FieldSpec(
        path=path,
        label=path[-1],
        kind=kind,
        description=description,
        default=default,
        advanced=advanced,
    )


# A miniature schema. `ssh.enabled` is the one curated field, so the
# advanced filter has something to keep and something to hide. These are
# invented specs, not real config paths: the state machine must not care
# what `BASIC_FIELDS` happens to contain today.
SPECS = (
    _spec("container_prefix", FieldKind.STR, ""),
    _spec("gpg.enabled"),
    _spec("ssh.enabled", advanced=False),
    _spec("ssh.seed_from_host"),
    _spec("chrome.url", FieldKind.STR, None, description="the landing page"),
)


def _open():
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    return st.open_editor(layer="repo", specs=SPECS, origins=origins)


def test_sections_are_the_top_level_keys_in_declaration_order():
    """A leaf at the top level is its own section, so nothing is unreachable."""
    assert st.sections(_open()) == ("container_prefix", "gpg", "ssh", "chrome")


def test_a_fresh_editor_starts_on_the_section_list():
    got = _open()
    assert got.section is None
    assert got.index == 0


def test_entering_a_section_lists_its_fields():
    got = st.toggle_show_all(st.enter_section(_open(), "ssh"))
    assert [s.label for s in st.visible_specs(got)] == ["enabled", "seed_from_host"]
    assert got.index == 0


def test_leaving_a_section_returns_to_the_section_list():
    got = st.leave_section(st.enter_section(_open(), "ssh"))
    assert got.section is None


def test_move_is_clamped_at_both_ends():
    got = st.toggle_show_all(st.enter_section(_open(), "ssh"))
    assert st.move(got, -1).index == 0
    assert st.move(got, 99).index == 1


def test_move_is_clamped_against_the_section_list_too():
    """The cursor is shared between the two panes, so both need clamping."""
    assert st.move(_open(), 99).index == len(st.sections(_open())) - 1


def test_entering_a_section_resets_the_cursor():
    """Sections differ in length; a carried index could land past the end."""
    got = st.move(st.toggle_show_all(st.enter_section(_open(), "ssh")), 1)
    assert got.index == 1
    assert st.enter_section(got, "gpg").index == 0


def test_current_is_none_on_the_section_list():
    assert st.current(_open()) is None
    assert st.current(st.enter_section(_open(), "ssh")).path == ("ssh", "enabled")


def test_the_default_view_hides_advanced_fields():
    """Only curated fields show until `a`. `ssh.enabled` is the curated one."""
    got = st.enter_section(_open(), "ssh")
    assert [s.label for s in st.visible_specs(got)] == ["enabled"]
    assert len(st.visible_specs(st.toggle_show_all(got))) == 2


def test_a_section_whose_fields_are_all_advanced_shows_empty_until_show_all():
    got = st.enter_section(_open(), "gpg")
    assert st.visible_specs(got) == ()
    assert st.current(got) is None
    assert len(st.visible_specs(st.toggle_show_all(got))) == 1


def test_search_matches_labels_paths_and_descriptions():
    got = st.set_query(_open(), "landing")
    assert [s.path for s in st.visible_specs(got)] == [("chrome", "url")]

    got = st.set_query(_open(), "seed")
    assert [s.path for s in st.visible_specs(got)] == [("ssh", "seed_from_host")]

    got = st.set_query(_open(), "GPG.")
    assert [s.path for s in st.visible_specs(got)] == [("gpg", "enabled")]


def test_search_ignores_the_advanced_filter():
    """Spec 4.3: filtering search results would hide what is being looked for."""
    got = _open()
    assert got.show_all is False
    assert st.visible_specs(st.set_query(got, "seed_from_host"))


def test_search_spans_every_section():
    got = st.set_query(st.enter_section(_open(), "gpg"), "enabled")
    assert {s.path for s in st.visible_specs(got)} == {
        ("gpg", "enabled"),
        ("ssh", "enabled"),
    }


def test_a_new_query_resets_the_cursor():
    got = st.move(st.set_query(_open(), "enabled"), 1)
    assert st.set_query(got, "seed").index == 0


def test_clearing_the_query_restores_the_section_view():
    """The section survives a search, so `/` then Esc lands where it started."""
    got = st.set_query(st.enter_section(_open(), "ssh"), "chrome")
    assert [s.path for s in st.visible_specs(got)] == [("chrome", "url")]

    cleared = st.set_query(got, "")
    assert cleared.section == "ssh"
    assert [s.label for s in st.visible_specs(cleared)] == ["enabled"]


def test_effective_prefers_a_staged_value_over_the_resolved_origin():
    got = st.stage(_open(), ("gpg", "enabled"), True)
    assert st.effective(got, ("gpg", "enabled")) is True
    assert st.effective(got, ("ssh", "enabled")) is False


def test_toggle_flips_the_bool_under_the_cursor():
    got = st.enter_section(_open(), "gpg")
    got = st.toggle_show_all(got)
    got = st.toggle_current(got)
    assert st.effective(got, ("gpg", "enabled")) is True
    assert st.effective(st.toggle_current(got), ("gpg", "enabled")) is False


def test_toggle_is_a_no_op_on_a_non_bool():
    got = st.toggle_show_all(st.enter_section(_open(), "chrome"))
    assert st.toggle_current(got) == got


def test_changes_drops_a_staged_value_equal_to_what_the_file_holds():
    """The property patch_yaml's byte-identical no-op depends on.

    Toggling a value and toggling it back must produce no diff at all.
    """
    raw = {"gpg": {"enabled": False}}
    got = st.stage(_open(), ("gpg", "enabled"), False)
    assert st.changes(got, raw) == ()
    assert st.is_dirty(got, raw) is False


def test_changes_emits_a_real_edit():
    raw = {"gpg": {"enabled": False}}
    got = st.stage(_open(), ("gpg", "enabled"), True)
    assert st.changes(got, raw) == (YamlChange(("gpg", "enabled"), True),)
    assert st.is_dirty(got, raw) is True


def test_reset_deletes_the_key_from_this_layer():
    """Spec 4.3: reset deletes, it does not write the default out.

    A written-out default freezes at today's value; an inherited one keeps
    following jailbee's own.
    """
    raw = {"gpg": {"enabled": True}}
    got = st.toggle_show_all(st.enter_section(_open(), "gpg"))
    got = st.reset_current(got, raw)
    assert st.changes(got, raw) == (YamlChange(("gpg", "enabled"), DELETE),)


def test_reset_on_an_inherited_key_stages_nothing():
    """The key is not in this layer, so deleting it would be a no-op diff."""
    raw: dict[str, object] = {}
    got = st.toggle_show_all(st.enter_section(_open(), "gpg"))
    got = st.reset_current(got, raw)
    assert st.changes(got, raw) == ()


def test_reset_discards_a_staged_edit_to_the_same_field():
    raw: dict[str, object] = {}
    got = st.stage(_open(), ("gpg", "enabled"), True)
    got = st.toggle_show_all(st.enter_section(got, "gpg"))
    assert st.changes(st.reset_current(got, raw), raw) == ()


def test_changes_are_ordered_by_path_so_a_save_is_reproducible():
    raw: dict[str, object] = {}
    got = st.stage(_open(), ("ssh", "enabled"), True)
    got = st.stage(got, ("gpg", "enabled"), True)
    assert [c.path for c in st.changes(got, raw)] == [
        ("gpg", "enabled"),
        ("ssh", "enabled"),
    ]


def test_an_explicit_null_is_a_real_change_not_a_reset():
    """`chrome.url: null` must reach the file as null, not as a deletion."""
    raw: dict[str, object] = {}
    got = st.stage(_open(), ("chrome", "url"), None)
    assert st.changes(got, raw) == (YamlChange(("chrome", "url"), None),)
