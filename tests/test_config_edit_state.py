"""The editor's pure state machine.

Modelled on `test_dashboard_settings.py`: every transition is a function
from state to state, so the whole interaction model is testable without a
terminal.
"""

from __future__ import annotations

from jailbee.config import AutostartStep, HostMount
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
COLLECTION = FieldSpec(
    path=("host_mounts",),
    label="host_mounts",
    kind=FieldKind.MODEL_LIST,
    description="bind mounts",
    default=[],
    item_model=HostMount,
    advanced=False,
)
"""A *top-level* collection: its section name and its path are the same crumb,
so one `enter_crumb` opens the collection itself rather than a one-row field
list."""

NESTED = FieldSpec(
    path=("autostart", "on_create"),
    label="on_create",
    kind=FieldKind.MODEL_LIST,
    description="steps run once",
    default=[],
    item_model=AutostartStep,
    advanced=False,
)
"""A collection under a real section, so reaching it takes two crumbs."""

SPECS = (
    _spec("container_prefix", FieldKind.STR, ""),
    _spec("gpg.enabled"),
    _spec("ssh.enabled", advanced=False),
    _spec("ssh.seed_from_host"),
    _spec("chrome.url", FieldKind.STR, None, description="the landing page"),
    COLLECTION,
    NESTED,
)


def _open():
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    return st.open_editor(layer="repo", specs=SPECS, origins=origins)


def test_sections_are_the_top_level_keys_in_declaration_order():
    """A leaf at the top level is its own section, so nothing is unreachable."""
    assert st.sections(_open()) == (
        "container_prefix",
        "gpg",
        "ssh",
        "chrome",
        "host_mounts",
        "autostart",
    )


def test_a_fresh_editor_starts_on_the_section_list():
    got = _open()
    assert got.trail == ()
    assert got.section is None
    assert got.index == 0


def test_entering_a_section_lists_its_fields():
    got = st.toggle_show_all(st.enter_crumb(_open(), "ssh"))
    assert [s.label for s in st.visible_specs(got)] == ["enabled", "seed_from_host"]
    assert got.index == 0


def test_leaving_a_section_returns_to_the_section_list():
    got = st.leave_crumb(st.enter_crumb(_open(), "ssh"))
    assert got.trail == ()
    assert got.section is None


def test_move_is_clamped_at_both_ends():
    got = st.toggle_show_all(st.enter_crumb(_open(), "ssh"))
    assert st.move(got, -1).index == 0
    assert st.move(got, 99).index == 1


def test_move_is_clamped_against_the_section_list_too():
    """The cursor is shared between the two panes, so both need clamping."""
    assert st.move(_open(), 99).index == len(st.sections(_open())) - 1


def test_entering_a_section_resets_the_cursor():
    """Sections differ in length; a carried index could land past the end."""
    got = st.move(st.toggle_show_all(st.enter_crumb(_open(), "ssh")), 1)
    assert got.index == 1
    assert st.enter_crumb(got, "gpg").index == 0


def test_current_is_none_on_the_section_list():
    assert st.current(_open()) is None
    assert st.current(st.enter_crumb(_open(), "ssh")).path == ("ssh", "enabled")


def test_the_default_view_hides_advanced_fields():
    """Only curated fields show until `a`. `ssh.enabled` is the curated one."""
    got = st.enter_crumb(_open(), "ssh")
    assert [s.label for s in st.visible_specs(got)] == ["enabled"]
    assert len(st.visible_specs(st.toggle_show_all(got))) == 2


def test_a_section_whose_fields_are_all_advanced_shows_empty_until_show_all():
    got = st.enter_crumb(_open(), "gpg")
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
    got = st.set_query(st.enter_crumb(_open(), "gpg"), "enabled")
    assert {s.path for s in st.visible_specs(got)} == {
        ("gpg", "enabled"),
        ("ssh", "enabled"),
    }


def test_a_new_query_resets_the_cursor():
    got = st.move(st.set_query(_open(), "enabled"), 1)
    assert st.set_query(got, "seed").index == 0


def test_clearing_the_query_restores_the_section_list():
    """`/` leaves the trail behind, so Esc lands on the section list.

    Search spans the top-level specs only, and once it can be run from inside
    an entry form the trail it came from may name a screen the results cannot
    describe (spec 11.3 rule 3) — so `set_query` clears it and clearing the
    query returns to the top rather than to the section it started in.
    """
    got = st.set_query(st.enter_crumb(_open(), "ssh"), "chrome")
    assert got.trail == ()
    assert [s.path for s in st.visible_specs(got)] == [("chrome", "url")]

    cleared = st.set_query(got, "")
    assert cleared.section is None
    assert st.screen(cleared).kind == "sections"
    assert st.visible_specs(cleared) == ()


def test_effective_prefers_a_staged_value_over_the_resolved_origin():
    got = st.stage(_open(), ("gpg", "enabled"), True)
    assert st.effective(got, ("gpg", "enabled")) is True
    assert st.effective(got, ("ssh", "enabled")) is False


def test_effective_reads_an_entry_field_out_of_the_saved_collection():
    origins = {
        ("host_mounts",): Origin("repo", [{"host": "/a", "readonly": True}]),
    }
    state = st.EditorState(layer="repo", specs=SPECS, origins=origins, staged={})

    assert st.effective(state, ("host_mounts", 0, "readonly")) is True
    assert st.effective(state, ("host_mounts", 0, "container")) is None


def test_a_staged_whole_collection_wins_over_the_saved_one_for_entry_reads():
    origins = {("host_mounts",): Origin("repo", [{"host": "/old"}])}
    state = st.EditorState(
        layer="repo",
        specs=SPECS,
        origins=origins,
        staged={("host_mounts",): [{"host": "/new"}]},
    )

    assert st.effective(state, ("host_mounts", 0, "host")) == "/new"


def test_a_staged_entry_field_wins_over_the_staged_collection_it_sits_in():
    origins = {("host_mounts",): Origin("repo", [{"host": "/a"}])}
    state = st.EditorState(
        layer="repo",
        specs=SPECS,
        origins=origins,
        staged={("host_mounts", 0, "host"): "/edited"},
    )

    assert st.effective(state, ("host_mounts", 0, "host")) == "/edited"


def test_entry_origin_says_set_only_when_the_key_is_in_the_entry():
    origins = {("host_mounts",): Origin("repo", [{"host": "/a"}])}
    state = st.EditorState(layer="repo", specs=SPECS, origins=origins, staged={})

    assert st.entry_origin(state, ("host_mounts", 0, "host")) == "set"
    assert st.entry_origin(state, ("host_mounts", 0, "readonly")) == "default"


def test_toggle_flips_the_bool_under_the_cursor():
    got = st.enter_crumb(_open(), "gpg")
    got = st.toggle_show_all(got)
    got = st.toggle_current(got)
    assert st.effective(got, ("gpg", "enabled")) is True
    assert st.effective(st.toggle_current(got), ("gpg", "enabled")) is False


def test_toggle_is_a_no_op_on_a_non_bool():
    got = st.toggle_show_all(st.enter_crumb(_open(), "chrome"))
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
    got = st.toggle_show_all(st.enter_crumb(_open(), "gpg"))
    got = st.reset_current(got, raw)
    assert st.changes(got, raw) == (YamlChange(("gpg", "enabled"), DELETE),)


def test_reset_on_an_inherited_key_stages_nothing():
    """The key is not in this layer, so deleting it would be a no-op diff."""
    raw: dict[str, object] = {}
    got = st.toggle_show_all(st.enter_crumb(_open(), "gpg"))
    got = st.reset_current(got, raw)
    assert st.changes(got, raw) == ()


def test_reset_discards_a_staged_edit_to_the_same_field():
    raw: dict[str, object] = {}
    got = st.stage(_open(), ("gpg", "enabled"), True)
    got = st.toggle_show_all(st.enter_crumb(got, "gpg"))
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


def test_a_top_level_collection_is_its_own_screen_one_step_in():
    """`host_mounts` is a section of one whose single row *is* the collection.

    The trail is the config path, so `("host_mounts",)` already names the
    collection spec — there is no one-row field list to step through first.
    """
    state = st.enter_crumb(_open(), "host_mounts")

    got = st.screen(state)

    assert got.kind == "collection"
    assert got.collection is COLLECTION


def test_a_nested_collection_takes_two_steps():
    """`autostart` is a real section: its rows are `on_create` and `on_start`."""
    state = st.enter_crumb(_open(), "autostart")
    assert st.screen(state).kind == "fields"

    state = st.enter_crumb(state, "on_create")

    assert st.screen(state).kind == "collection"
    assert state.trail == ("autostart", "on_create")


def test_entering_an_entry_lists_the_item_model_fields_at_full_paths():
    state = st.enter_crumb(_open(), "host_mounts")

    state = st.enter_crumb(state, 0)

    got = st.screen(state)
    assert got.kind == "entry"
    assert [s.path for s in got.specs] == [
        ("host_mounts", 0, "host"),
        ("host_mounts", 0, "container"),
        ("host_mounts", 0, "readonly"),
    ]


def test_an_entry_screen_names_the_collection_it_belongs_to():
    """`app.py` validates an entry against `collection.item_model` and writes it
    back at `entry_path`, both without re-deriving the trail."""
    state = _open()
    for crumb in ("host_mounts", 0):
        state = st.enter_crumb(state, crumb)

    got = st.screen(state)

    assert got.collection is COLLECTION
    assert got.entry_path == ("host_mounts", 0)


def test_an_entry_form_is_not_subject_to_the_show_all_filter():
    """`screen`'s entry branch returns its rows unfiltered — `a` changes nothing.

    Named for what it actually verifies. It does *not* prove `rebase` clears
    `advanced` (spec 11.3 rule 2): because this branch never consults
    `show_all`, the count would still be 3 if `rebase` forgot the flag. That
    rule is pinned where it is decided —
    `test_config_edit_schema.py::test_rebase_prefixes_every_path_and_clears_the_advanced_filter`.
    The two guards are independent and each needs its own test; asserting rule 2
    from here would only fail once *both* were broken.
    """
    state = _open()
    for crumb in ("host_mounts", 0):
        state = st.enter_crumb(state, crumb)

    assert state.show_all is False
    assert len(st.visible_specs(state)) == 3
    assert len(st.visible_specs(st.toggle_show_all(state))) == 3


def test_leaving_an_entry_returns_to_the_collection():
    state = _open()
    for crumb in ("host_mounts", 0):
        state = st.enter_crumb(state, crumb)

    state = st.leave_crumb(state)

    assert st.screen(state).kind == "collection"
    assert state.trail == ("host_mounts",)


def test_section_still_reports_the_open_top_level_key_at_any_depth():
    """`render.section_pane` and `title_bar` read it; they must keep working."""
    state = _open()
    for crumb in ("host_mounts", 0):
        state = st.enter_crumb(state, crumb)

    assert state.section == "host_mounts"


def test_search_clears_the_trail_and_spans_top_level_specs_only():
    """An entry field must not be reachable from a query it cannot describe."""
    state = _open()
    for crumb in ("host_mounts", 0):
        state = st.enter_crumb(state, crumb)

    state = st.set_query(state, "readonly")

    assert state.trail == ()
    assert [s.path for s in st.visible_specs(state)] == []


def test_entries_are_the_crumbs_of_a_collections_own_items():
    """List indices, map keys, and nothing at all for a value that is neither."""
    state = _open()
    assert st.entries(state, COLLECTION) == ()

    two = st.stage(state, ("host_mounts",), [{"host": "/a"}, {"host": "/b"}])
    assert st.entries(two, COLLECTION) == (0, 1)

    as_map = st.stage(state, ("host_mounts",), {"src": {"host": "/a"}})
    assert st.entries(as_map, COLLECTION) == ("src",)

    broken = st.stage(state, ("host_mounts",), "not a collection")
    assert st.entries(broken, COLLECTION) == ()


def test_move_is_clamped_against_a_collections_entry_list():
    """The cursor is shared by every screen, so the entry list needs clamping
    too — without a row count of its own it would be stuck on row 0."""
    state = st.enter_crumb(_open(), "host_mounts")
    state = st.stage(state, ("host_mounts",), [{"host": "/a"}, {"host": "/b"}])

    assert st.move(state, 99).index == 1
    assert st.move(state, -1).index == 0


def test_an_entry_of_a_nested_collection_takes_three_crumbs():
    """The two shapes composed: a section, its collection, then one entry.

    This is where `screen`'s `i += 2` would show an off-by-one — the section
    crumb is consumed singly and the collection/entry pair together, so a walk
    that advanced by the wrong amount would still resolve the two-crumb case
    and fail only here.
    """
    state = _open()
    for crumb in ("autostart", "on_create", 0):
        state = st.enter_crumb(state, crumb)

    got = st.screen(state)

    assert got.kind == "entry"
    assert got.collection is NESTED
    assert got.entry_path == ("autostart", "on_create", 0)
    assert [s.path for s in got.specs] == [
        ("autostart", "on_create", 0, "name"),
        ("autostart", "on_create", 0, "run"),
        ("autostart", "on_create", 0, "network"),
        ("autostart", "on_create", 0, "mounts"),
        ("autostart", "on_create", 0, "env"),
        ("autostart", "on_create", 0, "working_dir"),
        ("autostart", "on_create", 0, "background"),
        ("autostart", "on_create", 0, "timeout"),
        ("autostart", "on_create", 0, "continue_on_error"),
    ]
