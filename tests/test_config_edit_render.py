"""What the editor draws, without a terminal.

Every function under test is `state -> fragments`, the same split
`dashboard_settings.render_settings` uses on the Rich side. The assertions read
the flattened text, so they survive a restyling.
"""

from __future__ import annotations

from jailbee.config import HostMount
from jailbee.config_edit import state as st
from jailbee.config_edit.layers import Origin, read_layers, resolve
from jailbee.config_edit.render import (
    body_pane,
    collection_pane,
    edit_block,
    field_pane,
    footer,
    help_pane,
    section_pane,
    title_bar,
)
from jailbee.config_edit.schema import FieldKind, FieldSpec, repo_specs


def _text(fragments) -> str:
    return "".join(chunk for _style, chunk, *_rest in fragments)


def _spec(dotted, kind=FieldKind.BOOL, default=False, advanced=False, **kwargs):
    path = tuple(dotted.split("."))
    return FieldSpec(
        path=path,
        label=path[-1],
        kind=kind,
        description=f"what {dotted} does",
        default=default,
        advanced=advanced,
        **kwargs,
    )


SPECS = (
    _spec("gpg.enabled"),
    _spec("gpg.agent_forward", advanced=True),
    _spec("ssh.enabled"),
    _spec("egress_allow", kind=FieldKind.STR_LIST, default=[]),
    _spec("host_mounts", kind=FieldKind.MODEL_LIST, default=[], item_model=HostMount),
    _spec("github.enabled"),
)


def _layers(tmp_path, repo_text="", global_text=""):
    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    glob = tmp_path / "global.yaml"
    repo.parent.mkdir(parents=True, exist_ok=True)
    if repo_text:
        repo.write_text(repo_text)
    if global_text:
        glob.write_text(global_text)
    return read_layers(repo, glob)


def _state(layer="repo", origins=None, **kwargs):
    base = st.open_editor(
        layer=layer,
        specs=SPECS,
        origins=origins or {s.path: Origin("default", s.default) for s in SPECS},
    )
    return st.EditorState(**{**base.__dict__, **kwargs})


_HOST_MOUNTS_SPEC = next(s for s in SPECS if s.path == ("host_mounts",))


def _collection_state(entries, layer="repo", **kwargs):
    """A state on the `host_mounts` collection screen.

    `entries` is the collection's own current (unstaged) contents — what
    `state.origins` reports it holds, independent of whatever a `LayerSet`
    passed alongside this state says (that is only ever consulted for the
    *inherited* block, through `layers.inherited_entries`).
    """
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("host_mounts",)] = Origin(layer, entries)
    return _state(layer=layer, origins=origins, trail=("host_mounts",), **kwargs)


def _entry_state(entry, layer="repo", **kwargs):
    """A state on entry `0` of `host_mounts`, `entry` as its own unstaged value."""
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("host_mounts",)] = Origin(layer, [entry])
    return _state(layer=layer, origins=origins, trail=("host_mounts", 0), **kwargs)


def test_the_fixture_schema_yields_the_same_screen_shapes_the_real_one_does():
    """`host_mounts` here carries a real `item_model`, as `repo_specs()` does.

    Without it `state.screen` never takes its collection branch and every test
    in this module would be asserting against a state shape the real schema can
    no longer produce — passing on a fiction. This test is the guard on the
    fixture itself, not on any renderer: what a collection screen *draws* is
    `collection_pane`'s, which does not exist yet.
    """
    assert st.screen(_state(trail=("host_mounts",))).kind == "collection"
    assert st.screen(_state(trail=("gpg",))).kind == "fields"


def test_section_pane_lists_every_top_level_key_once():
    pane = section_pane(_state())
    tokens = _text(pane.fragments).split()
    names = [tok for tok in tokens if tok not in {"▸", "·"}]
    assert names == ["gpg", "ssh", "egress_allow", "host_mounts", "github"]
    assert pane.cursor_row == 0


def test_field_pane_shows_the_saved_value_and_its_origin(tmp_path):
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("gpg", "enabled")] = Origin("global", True)
    pane = field_pane(_state(trail=("gpg",), origins=origins), _layers(tmp_path))
    text = _text(pane.fragments)
    assert "enabled" in text
    assert "true" in text
    assert "(global)" in text


def test_the_default_view_hides_advanced_fields_until_a_is_pressed(tmp_path):
    layers = _layers(tmp_path)
    assert "agent_forward" not in _text(field_pane(_state(trail=("gpg",)), layers).fragments)
    shown = field_pane(_state(trail=("gpg",), show_all=True), layers)
    assert "agent_forward" in _text(shown.fragments)


def test_a_staged_edit_is_marked_and_says_what_will_happen(tmp_path):
    layers = _layers(tmp_path, repo_text="gpg:\n  enabled: false\n")
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("gpg", "enabled")] = Origin("repo", False)
    state = st.stage(_state(trail=("gpg",), origins=origins), ("gpg", "enabled"), True)
    text = _text(field_pane(state, layers).fragments)
    before, arrow, after = text.partition("→")
    assert arrow, "no staged edit was marked at all"
    # The value column must still show what is saved (false), not what the
    # edit will make it (true) — the arrow is what carries the pending
    # change, not the main column. A regression to `effective()` would show
    # "true" on both sides of the arrow.
    assert "false" in before
    assert "true" in after
    assert "(repo)" in before  # the origin still describes the file, not the edit


def test_a_staged_reset_says_reset_rather_than_the_old_value(tmp_path):
    layers = _layers(tmp_path, repo_text="gpg:\n  enabled: true\n")
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("gpg", "enabled")] = Origin("repo", True)
    state = st.reset_current(_state(trail=("gpg",), origins=origins), layers.repo_raw)
    assert "→ reset" in _text(field_pane(state, layers).fragments)


def test_a_no_op_edit_is_not_marked(tmp_path):
    """The marker and the `modified` counter come from the same change list."""
    layers = _layers(tmp_path, repo_text="gpg:\n  enabled: true\n")
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("gpg", "enabled")] = Origin("repo", True)
    state = st.stage(_state(trail=("gpg",), origins=origins), ("gpg", "enabled"), True)
    assert "→" not in _text(field_pane(state, layers).fragments)
    assert "modified: 0" in _text(title_bar(state, layers))


def test_search_rows_are_named_by_their_full_path(tmp_path):
    state = _state(query="enabled")
    text = _text(field_pane(state, _layers(tmp_path)).fragments)
    assert "gpg.enabled" in text
    assert "ssh.enabled" in text


def test_title_bar_names_the_layer_the_file_and_the_pending_count(tmp_path):
    layers = _layers(tmp_path)
    text = _text(title_bar(_state(), layers))
    assert "repo" in text
    assert str(layers.repo_path) in text
    assert "modified: 0" in text


def test_help_pane_carries_the_description_and_the_default(tmp_path):
    state = _state(trail=("gpg",))
    text = _text(help_pane(state, _layers(tmp_path)))
    assert "gpg.enabled" in text
    assert "what gpg.enabled does" in text
    assert "Default" in text


def test_help_pane_shows_inherited_list_context_for_an_appending_key(tmp_path):
    """A repo `egress_allow` adds to the global one; the user must see that."""
    layers = _layers(
        tmp_path,
        repo_text="egress_allow:\n  - repo.example\n",
        global_text="egress_allow:\n  - global.example\n",
    )
    rows = [s for s in SPECS]
    state = st.EditorState(
        layer="repo",
        specs=tuple(rows),
        origins={s.path: Origin("default", s.default) for s in rows},
        staged={},
        trail=("egress_allow",),
    )
    text = _text(help_pane(state, layers))
    assert "global.example" in text


def _egress_state(staged):
    """A repo-layer state on the `egress_allow` section with `staged` applied."""
    rows = list(SPECS)
    return st.EditorState(
        layer="repo",
        specs=tuple(rows),
        origins={s.path: Origin("default", s.default) for s in rows},
        staged=staged,
        trail=("egress_allow",),
    )


def test_help_pane_warns_when_a_staged_empty_list_would_discard_the_inherited_entries(tmp_path):
    """A staged `[]` is `deep_merge`'s explicit reset, so saving it empties the
    allowlist — the inherited-context sentence would state the exact inverse.

    `inherited_entries` answers against the layers as saved (spec 10.1 option
    b), which is right for an origin marker and wrong for a claim about what
    the save will do. This is the one place the two must not be the same.
    """
    layers = _layers(
        tmp_path,
        repo_text="egress_allow:\n  - repo.example\n",
        global_text="egress_allow:\n  - global.example\n",
    )
    text = _text(help_pane(_egress_state({("egress_allow",): []}), layers))
    assert "global.example" not in text
    assert "added to these" not in text
    assert "discards" in text
    assert "`r`" in text


def test_help_pane_keeps_the_inherited_entries_for_a_staged_non_empty_list(tmp_path):
    """A non-empty repo list still appends, so the context stays true."""
    layers = _layers(
        tmp_path,
        repo_text="egress_allow:\n  - repo.example\n",
        global_text="egress_allow:\n  - global.example\n",
    )
    text = _text(help_pane(_egress_state({("egress_allow",): ["other.example"]}), layers))
    assert "global.example" in text
    assert "discards" not in text


def test_help_pane_keeps_the_inherited_entries_for_a_staged_reset(tmp_path):
    """`r` deletes the repo key, so the global entries are inherited whole —
    the opposite of a discard, and the key the warning itself points at.
    """
    layers = _layers(
        tmp_path,
        repo_text="egress_allow:\n  - repo.example\n",
        global_text="egress_allow:\n  - global.example\n",
    )
    text = _text(help_pane(_egress_state({("egress_allow",): st.UNSET}), layers))
    assert "global.example" in text
    assert "discards" not in text


def test_edit_block_names_a_global_only_key():
    reason = edit_block(_spec("github.enabled"), "repo")
    assert reason is not None
    assert "global.yaml" in reason
    assert edit_block(_spec("github.enabled"), "global") is None


def test_edit_block_lets_a_collection_through():
    """A collection of models now has its own drill-down screen
    (`collection_pane`/`body_pane`), so `edit_block` no longer refuses it —
    replaces `test_edit_block_refuses_a_model_collection_for_now`.
    """
    spec = _spec("host_mounts", kind=FieldKind.MODEL_LIST, item_model=HostMount)
    assert edit_block(spec, "repo") is None


def test_collection_pane_lists_entries_with_a_one_line_summary(tmp_path):
    state = _collection_state([{"host": "/a", "container": "/data"}])

    got = collection_pane(state, _layers(tmp_path))

    text = _text(got.fragments)
    assert "[0]" in text
    assert "/a" in text


def test_collection_pane_shows_inherited_entries_above_and_marks_them(tmp_path):
    """Repo-layer lists append to the global one; the global entries cannot be
    removed here (spec 11.2), so they must not look like rows the cursor owns.
    """
    state = _collection_state([{"host": "/mine"}], layer="repo")
    layer_set = _layers(tmp_path, global_text="host_mounts:\n  - host: /inherited\n")

    text = _text(collection_pane(state, layer_set).fragments)

    assert text.index("/inherited") < text.index("/mine")
    assert "inherited from global" in text.casefold()


def test_collection_pane_says_so_when_the_collection_is_empty(tmp_path):
    state = _collection_state([])

    text = _text(collection_pane(state, _layers(tmp_path)).fragments)

    assert "press `n`" in text


def test_field_pane_marks_an_entry_field_that_is_not_set(tmp_path):
    state = _entry_state({"host": "/a"})

    text = _text(field_pane(state, _layers(tmp_path)).fragments)

    assert "(set)" in text  # host
    assert "(default)" in text  # readonly


def test_title_bar_shows_the_trail_once_inside_a_collection(tmp_path):
    state = _entry_state({"host": "/a"})

    text = _text(title_bar(state, _layers(tmp_path)))

    assert "host_mounts ▸ 0" in text


def test_body_pane_dispatches_to_the_collection_pane_on_a_collection_screen(tmp_path):
    """`app.py` calls only `body_pane`; this pins that it actually reaches
    `collection_pane` rather than falling through to the (empty) field pane.
    """
    state = _collection_state([{"host": "/a"}])

    text = _text(body_pane(state, _layers(tmp_path)).fragments)

    assert "[0]" in text
    assert "/a" in text


def test_body_pane_draws_every_real_collection_section_the_bare_field_pane_used_to_blank(
    tmp_path,
):
    """The live regression this task fixes.

    Since Task 3, `state.screen` resolves `host_mounts`, `host_devices`,
    `host_ports`, `optional_mounts`, `shared_caches` and `agents` (all six of
    them, on the real schema) to a `collection` screen — but nothing drew
    one, so `field_pane` rendered its own *fields*-screen empty note instead:
    "nothing in the basic set — press `a` to show all", with `a` doing
    nothing because there were no fields to show in the first place, basic or
    otherwise. Verified here against `schema.repo_specs()`, not the
    module's own small fixture above, which is the only way to be sure the
    real schema still classifies every one of them the same way.
    """
    specs = repo_specs()
    layers = _layers(tmp_path)
    origins = resolve(specs, layers)
    # `shared_caches` alone ships a non-empty default (one `ssh` entry, from
    # `_default_shared_caches`) — the other five default to `[]`/`{}`.
    empty_by_default = ("host_mounts", "host_devices", "host_ports", "optional_mounts", "agents")
    for section in (*empty_by_default, "shared_caches"):
        state = st.EditorState(
            layer="repo", specs=specs, origins=origins, staged={}, trail=(section,)
        )
        assert st.screen(state).kind == "collection", section
        text = _text(body_pane(state, layers).fragments)
        assert "press `a`" not in text, section
        if section in empty_by_default:
            assert "press `n`" in text, section
        else:
            assert "[0]" in text, section


def test_field_pane_marks_an_entry_field_edited_under_an_already_staged_collection(tmp_path):
    """Task 5's invariant (a leaf and its own staged ancestor never coexist in
    `state.staged`) means that once the whole collection is staged — here, by
    `add_entry` (`n`) — a field edited on one of its entries is folded *into*
    that staged collection rather than getting a staged key of its own.
    `_pending`'s `spec.path in pending` reads `changes()`'s own paths, which
    are folded the same way, so it can never mark such a row: without
    `_entry_pending` comparing against the saved layer directly, this test
    fails with neither row painted `class:staged` at all.
    """
    layers = _layers(
        tmp_path, repo_text="host_mounts:\n  - host: /old\n    container: /old\n"
    )
    state = _collection_state([{"host": "/old", "container": "/old"}])
    state, crumb = st.add_entry(state, _HOST_MOUNTS_SPEC)
    state = st.enter_crumb(state, crumb)
    entry_specs = st.screen(state).specs
    host_spec = next(s for s in entry_specs if s.path[-1] == "host")
    state = st.stage(state, host_spec.path, "/new")

    pane = field_pane(state, layers)
    staged_lines = [text for style, text in pane.fragments if style == "class:staged"]

    host_line = next(line for line in staged_lines if "host" in line)
    assert "●" in host_line
    assert "/new" in host_line
    # The untouched sibling field of the same (staged) entry must not be
    # marked — only `host` actually changed.
    container_line = next(text for _style, text in pane.fragments if "container" in text)
    assert "●" not in container_line


def test_edit_block_refuses_a_secret():
    spec = _spec("github.api_tokens", kind=FieldKind.STR_MAP, secret=True)
    reason = edit_block(spec, "global")
    assert reason is not None
    assert "0600" in reason


def test_footer_names_every_action_the_editor_offers():
    """The footer text itself, not the bindings — `_bindings` lives in
    `app.py` and nothing here reads it. What this pins is that no action
    quietly drops out of the one line the user is told to read.
    """
    text = _text(footer(_state()))
    for key in ("search", "toggle", "edit", "reset", "show all", "save", "quit"):
        assert key in text


def test_footer_names_the_collection_actions_on_a_collection_screen():
    """`footer` now takes the state, so it can offer different keys for a
    collection screen (`n`/`x`/`J`/`K`) instead of the section/field ones,
    which make no sense there (there is nothing to search or toggle)."""
    text = _text(footer(_collection_state([])))
    for key in ("new", "delete", "move", "open", "save", "quit"):
        assert key in text
