"""The editor's pure state machine.

Modelled on `test_dashboard_settings.py`: every transition is a function
from state to state, so the whole interaction model is testable without a
terminal.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

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


def test_a_staged_entry_field_wins_over_the_saved_collection_it_sits_in():
    """Renamed from `..._over_the_staged_collection_it_sits_in`.

    That name described a collision this fixture never set up — only the leaf
    is staged, so there was no staged collection for it to win over, and the
    test proved nothing beyond the saved-layer read below it. The scenario the
    old name claimed is now impossible by construction anyway: `stage` folds a
    leaf into a staged ancestor instead of letting the two coexist, which the
    next test covers.
    """
    origins = {("host_mounts",): Origin("repo", [{"host": "/a"}])}
    state = st.EditorState(
        layer="repo",
        specs=SPECS,
        origins=origins,
        staged={("host_mounts", 0, "host"): "/edited"},
    )

    assert st.effective(state, ("host_mounts", 0, "host")) == "/edited"


def test_staging_a_field_under_a_staged_collection_folds_it_into_the_collection():
    """The invariant: a leaf and a staged ancestor of it never coexist.

    `changes` drops a leaf under a staged ancestor as superseded, so a leaf
    left standing here would be silently discarded at save time while
    `effective` went on showing it. `stage` writes into the collection instead.
    """
    origins = {("host_mounts",): Origin("repo", [{"host": "/a"}])}
    state = st.EditorState(
        layer="repo",
        specs=SPECS,
        origins=origins,
        staged={("host_mounts",): [{"host": "/a"}]},
    )

    got = st.stage(state, ("host_mounts", 0, "host"), "/edited")

    assert ("host_mounts", 0, "host") not in got.staged
    assert got.staged[("host_mounts",)] == [{"host": "/edited"}]
    assert st.effective(got, ("host_mounts", 0, "host")) == "/edited"


def test_folding_a_field_in_does_not_mutate_the_state_it_came_from():
    """`replace` shares staged structures between states; a fold must copy.

    Without the copy, staging an edit would reach back and rewrite the value
    every earlier state holds — including the one the quit confirmation
    compares against.
    """
    origins = {("host_mounts",): Origin("repo", [{"host": "/a"}])}
    before = st.EditorState(
        layer="repo",
        specs=SPECS,
        origins=origins,
        staged={("host_mounts",): [{"host": "/a"}]},
    )

    st.stage(before, ("host_mounts", 0, "host"), "/edited")

    assert before.staged[("host_mounts",)] == [{"host": "/a"}]


def test_resetting_a_field_under_a_staged_collection_removes_it_from_the_entry():
    """`r` inside a staged entry prunes the key rather than staging `UNSET`.

    A staged `UNSET` leaf would sit under a staged ancestor and be dropped as
    superseded, so the reset would appear to do nothing at all.
    """
    origins = {("host_mounts",): Origin("repo", [{"host": "/a", "readonly": True}])}
    state = st.EditorState(
        layer="repo",
        specs=SPECS,
        origins=origins,
        staged={("host_mounts",): [{"host": "/a", "readonly": True}]},
        trail=("host_mounts", 0),
        index=2,
    )
    assert st.current(state).path == ("host_mounts", 0, "readonly")

    got = st.reset_current(state, {"host_mounts": [{"host": "/a", "readonly": True}]})

    assert got.staged[("host_mounts",)] == [{"host": "/a"}]
    assert ("host_mounts", 0, "readonly") not in got.staged
    assert st.entry_origin(got, ("host_mounts", 0, "readonly")) == "default"


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


# -- adding, deleting and reordering entries ------------------------------


def _staged(**kw):
    """A state whose repo layer already holds two host mounts."""
    origins = {("host_mounts",): Origin("repo", [{"host": "/a"}, {"host": "/b"}])}
    return st.EditorState(layer="repo", specs=SPECS, origins=origins, **kw)


SAVED = {"host_mounts": [{"host": "/a"}, {"host": "/b"}]}
"""The layer file `_staged` describes, for the `changes` calls below."""


def test_a_new_list_entry_is_an_empty_mapping_at_the_end():
    """Empty, not defaults written out: a written-out default freezes (spec 4.3)."""
    state, crumb = st.add_entry(_staged(staged={}), COLLECTION)

    assert crumb == 2
    assert state.staged[("host_mounts",)] == [{"host": "/a"}, {"host": "/b"}, {}]


def test_a_new_map_entry_needs_a_key_and_a_list_entry_refuses_one():
    """A map has no next index to invent, so the caller must name the key."""
    origins = {("agents",): Origin("repo", {"claude": {}})}
    spec = replace(COLLECTION, path=("agents",), kind=FieldKind.MODEL_MAP, default={})
    state = st.EditorState(layer="repo", specs=(spec,), origins=origins, staged={})

    got, crumb = st.add_entry(state, spec, "codex")

    assert crumb == "codex"
    assert got.staged[("agents",)] == {"claude": {}, "codex": {}}
    with pytest.raises(ValueError, match="key name"):
        st.add_entry(state, spec)


def test_deleting_an_entry_stages_the_remaining_list():
    state = st.delete_entry(_staged(staged={}), COLLECTION, 0)

    assert state.staged[("host_mounts",)] == [{"host": "/b"}]


def test_deleting_a_crumb_that_addresses_nothing_is_a_no_op():
    """Staging an identical collection would light up `modified` for no edit."""
    assert st.delete_entry(_staged(staged={}), COLLECTION, 7).staged == {}


def test_deleting_a_map_entry_removes_that_key():
    origins = {("agents",): Origin("repo", {"claude": {}, "codex": {}})}
    spec = replace(COLLECTION, path=("agents",), kind=FieldKind.MODEL_MAP, default={})
    state = st.EditorState(layer="repo", specs=(spec,), origins=origins, staged={})

    got = st.delete_entry(state, spec, "codex")

    assert got.staged[("agents",)] == {"claude": {}}
    assert st.delete_entry(state, spec, "gemini").staged == {}


def test_moving_an_entry_swaps_it_with_its_neighbour():
    state = st.move_entry(_staged(staged={}), COLLECTION, 0, 1)

    assert state.staged[("host_mounts",)] == [{"host": "/b"}, {"host": "/a"}]


def test_moving_past_either_end_is_a_no_op():
    state = st.move_entry(_staged(staged={}), COLLECTION, 0, -1)

    assert ("host_mounts",) not in state.staged


def test_a_map_has_no_order_so_moving_one_of_its_entries_does_nothing():
    origins = {("agents",): Origin("repo", {"claude": {}, "codex": {}})}
    spec = replace(COLLECTION, path=("agents",), kind=FieldKind.MODEL_MAP, default={})
    state = st.EditorState(layer="repo", specs=(spec,), origins=origins, staged={})

    assert st.move_entry(state, spec, 0, 1) == state


def test_a_structural_change_carries_a_staged_field_along_with_its_entry():
    """The brief expected the leaf *superseded*; folding it in is strictly better.

    Its index addresses the list that stops existing, so it cannot survive as a
    leaf — but the edit itself can. `_collection_value` replays it onto the old
    list *before* the delete, so it travels with the entry it was made on
    instead of being thrown away (or, worse, landing on whichever entry
    inherited index 1).
    """
    state = _staged(staged={("host_mounts", 1, "host"): "/edited"})

    state = st.delete_entry(state, COLLECTION, 0)

    assert ("host_mounts", 1, "host") not in state.staged
    got = st.changes(state, SAVED)
    assert [c.path for c in got] == [("host_mounts",)]
    assert got[0].value == [{"host": "/edited"}]


def test_filling_in_a_new_entry_after_adding_it_reaches_the_save():
    """add-then-edit. The bug this whole invariant exists to stop.

    `n` stages the whole collection; each field then filled in would be a leaf
    under it, and `changes` would drop every one of them as superseded — the
    save would write the new entry as `{}` and the user's typing would be gone
    with nothing on screen to say so.
    """
    state, crumb = st.add_entry(_staged(staged={}), COLLECTION)
    state = st.stage(state, ("host_mounts", crumb, "host"), "/new")
    state = st.stage(state, ("host_mounts", crumb, "container"), "/in")

    got = st.changes(state, SAVED)

    assert [c.path for c in got] == [("host_mounts",)]
    assert got[0].value == [
        {"host": "/a"},
        {"host": "/b"},
        {"host": "/new", "container": "/in"},
    ]


def test_adding_an_entry_keeps_an_edit_made_before_it():
    """edit-then-add, the mirror case.

    Here the leaf is staged first, so `effective` — which resolves from the
    nearest staged *ancestor* — cannot see it when `add_entry` rebuilds the
    list. `_collection_value` has to replay the staged leaves itself, or the
    earlier edit disappears the moment the user presses `n`.
    """
    state = st.stage(_staged(staged={}), ("host_mounts", 0, "host"), "/edited")
    state, _ = st.add_entry(state, COLLECTION)

    got = st.changes(state, SAVED)

    assert [c.path for c in got] == [("host_mounts",)]
    assert got[0].value == [{"host": "/edited"}, {"host": "/b"}, {}]


def test_a_reset_made_before_a_structural_change_survives_it():
    """`UNSET` under the collection prunes the key rather than being replayed."""
    state = _staged(staged={("host_mounts", 0, "host"): st.UNSET})

    state = st.move_entry(state, COLLECTION, 0, 1)

    assert state.staged[("host_mounts",)] == [{"host": "/b"}, {}]


def test_changes_survives_an_index_and_a_key_at_the_same_depth():
    """What `_sort_key` actually buys: plain `sorted` raises `TypeError` here.

    The brief claimed the numeric test below would raise. It does not — every
    path there has an `int` in the same position, so tuple comparison never
    reaches an int-versus-str pair. Only a *shape clash* under one prefix does,
    which is a hand-broken file (`host_mounts` written as a list in one place
    and keyed in another). That must not blow up the save path, so the case is
    pinned here rather than left to a user to discover.
    """
    state = _staged(staged={("host_mounts", 0, "host"): "/x", ("host_mounts", "note"): "hi"})

    got = st.changes(state, SAVED)

    assert [c.path for c in got] == [("host_mounts", 0, "host"), ("host_mounts", "note")]


def test_changes_orders_indices_numerically_not_as_text():
    """`host_mounts.10` follows `host_mounts.2`, so a save is reproducible.

    Plain `sorted` would pass this too (see the test above); it is pinned
    because `_sort_key` keeps `int` segments as ints, and a stringifying
    variant of it — the obvious way to dodge the `TypeError` — would order
    these 0, 10, 2.
    """
    state = _staged(
        staged={
            ("host_mounts", 0, "host"): "/x",
            ("host_mounts", 10, "host"): "/y",
            ("host_mounts", 2, "host"): "/z",
        }
    )
    layer_raw = {"host_mounts": [{"host": "/a"}] * 11}

    got = st.changes(state, layer_raw)

    assert [c.path[1] for c in got] == [0, 2, 10]


def test_changes_drops_a_leaf_that_somehow_shares_staged_with_an_ancestor():
    """`_superseded`'s belt-and-braces net, reached only by hand-built state.

    No transition can produce this pair any more — `stage` folds and
    `_stage_collection` prunes — so the state is constructed directly. The net
    stays because the failure it prevents is a wrong write, not a crash.
    """
    state = _staged(
        staged={
            ("host_mounts",): [{"host": "/kept"}],
            ("host_mounts", 4, "host"): "/stale",
        }
    )

    assert [c.path for c in st.changes(state, SAVED)] == [("host_mounts",)]


def test_entries_reports_map_keys_in_order():
    origins = {("agents",): Origin("repo", {"claude": {}, "codex": {}})}
    spec = replace(COLLECTION, path=("agents",), kind=FieldKind.MODEL_MAP, default={})
    state = st.EditorState(layer="repo", specs=(spec,), origins=origins, staged={})

    assert st.entries(state, spec) == ("claude", "codex")


def test_move_counts_a_collections_rows_with_entries():
    """Step 5 of the brief, already shipped: `move` asks `entries`, not `specs`.

    A collection screen draws no `FieldSpec` rows at all, so a row count taken
    from `view.specs` would pin the cursor to row 0 on every list in the
    editor.
    """
    state = st.enter_crumb(_staged(staged={}), "host_mounts")

    assert st.screen(state).specs == ()
    assert st.move(state, 99).index == 1


# -- a pending reset of a collection, and editing inside one --------------


def test_typing_into_an_entry_cancels_a_pending_reset_of_its_collection():
    """The reviewer's sequence, through the public API only.

    Search `host_mounts` → `r` (stages `UNSET` at the collection) → walk into
    the entry list, which still renders because `effective` falls through an
    `UNSET` to what the layers say → type into a field.

    While `_staged_ancestor` skipped `UNSET`, that left the leaf and the
    `UNSET` collection in `staged` together: the screen showed the typed value
    and `changes` emitted the delete alone, discarding it. `app.reset` has no
    collection guard, so it was reachable in the shipped UI.

    The ruling: editing inside a collection *materialises* it — the pending
    reset is dropped and the collection the user is looking at is staged with
    the edit in it. The reset is invisible at that depth, so the edit is the
    more recent and more specific instruction.
    """
    state = st.set_query(_staged(staged={}), "host_mounts")
    assert st.current(state) is COLLECTION

    state = st.reset_current(state, SAVED)
    assert state.staged == {("host_mounts",): st.UNSET}

    state = st.enter_crumb(st.enter_crumb(state, "host_mounts"), 0)
    state = st.stage(state, ("host_mounts", 0, "host"), "/typed")

    assert st.effective(state, ("host_mounts", 0, "host")) == "/typed"
    assert st.changes(state, SAVED) == (
        YamlChange(("host_mounts",), [{"host": "/typed"}, {"host": "/b"}]),
    )


def test_resetting_an_entry_field_cancels_a_pending_reset_of_its_collection():
    """The same ruling for `r` rather than typing: it is still an edit *inside*.

    A leaf `UNSET` under a collection `UNSET` would be superseded exactly like
    the typed value, so the inner reset would appear to do nothing.
    """
    origins = {("host_mounts",): Origin("repo", [{"host": "/a", "readonly": True}])}
    saved = {"host_mounts": [{"host": "/a", "readonly": True}]}
    state = st.EditorState(
        layer="repo",
        specs=SPECS,
        origins=origins,
        staged={("host_mounts",): st.UNSET},
        trail=("host_mounts", 0),
        index=2,
    )
    assert st.current(state).path == ("host_mounts", 0, "readonly")

    got = st.reset_current(state, saved)

    assert got.staged == {("host_mounts",): [{"host": "/a"}]}
    assert st.changes(got, saved) == (YamlChange(("host_mounts",), [{"host": "/a"}]),)


def test_a_pending_reset_of_a_collection_is_cancelled_by_adding_an_entry_too():
    """`n` materialises the same way `stage` does — one rule, not two."""
    state = _staged(staged={("host_mounts",): st.UNSET})

    got, crumb = st.add_entry(state, COLLECTION)

    assert crumb == 2
    assert got.staged == {("host_mounts",): [{"host": "/a"}, {"host": "/b"}, {}]}


def test_resetting_a_collection_and_leaving_it_alone_still_deletes_it():
    """The cancellation is not a refusal: an untouched pending reset still saves."""
    state = st.set_query(_staged(staged={}), "host_mounts")

    state = st.reset_current(state, SAVED)

    assert st.changes(state, SAVED) == (YamlChange(("host_mounts",), DELETE),)


def test_staging_where_the_ancestor_has_no_room_raises_instead_of_losing_it():
    """`_plant` failing must be loud. Falling back to the bare leaf would
    rebuild the forbidden pair and drop the edit at save time in silence.

    Reachable here with a list too short for the index — the index addresses an
    entry that is not there, and inventing one would shift the rest.
    """
    state = _staged(staged={("host_mounts",): []})

    with pytest.raises(ValueError, match=r"host_mounts\.0\.host"):
        st.stage(state, ("host_mounts", 0, "host"), "/x")


def test_staging_under_a_scalar_ancestor_raises_too():
    """`_materialised` returns `None` for a value that is neither list nor map."""
    state = _staged(staged={("host_mounts",): "not a collection"})

    with pytest.raises(ValueError, match="host_mounts"):
        st.stage(state, ("host_mounts", 0, "host"), "/x")


def test_resetting_under_a_broken_ancestor_is_a_no_op_not_a_raise():
    """Asymmetric with `stage` on purpose: `r` is a keystroke on whatever the
    cursor is on, and a hand-broken file must not crash the TUI. Nothing is
    staged, so the invariant holds either way."""
    state = st.EditorState(
        layer="repo",
        specs=SPECS,
        origins={("host_mounts",): Origin("repo", [{"host": "/a"}])},
        staged={("host_mounts",): "not a collection"},
        trail=("host_mounts", 0),
        index=2,
    )

    assert st.reset_current(state, SAVED) == state
