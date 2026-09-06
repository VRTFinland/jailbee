"""What the editor draws, without a terminal.

Every function under test is `state -> fragments`, the same split
`dashboard_settings.render_settings` uses on the Rich side. The assertions read
the flattened text, so they survive a restyling.
"""

from __future__ import annotations

from dataclasses import replace

import yaml

from jailbee.config import HostMount
from jailbee.config_edit import state as st
from jailbee.config_edit.layers import raw_for, read_layers, resolve
from jailbee.config_edit.render import (
    _SECRET_MASK,
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


def _state(layer_set, layer="repo", specs=SPECS, **kwargs):
    """A state over `layer_set` — the same one the renderer under test is given.

    `origins` and `layer_raw` both come from it (`layers.resolve` /
    `layers.raw_for`), so the merged view a row shows and the open layer a
    collection screen edits can never describe two different files. That is
    why `layer_set` is required rather than defaulted: the fixture this
    replaced took a hand-built `origins` and let the caller hand the renderer
    an unrelated `LayerSet` alongside it, and advertised the decoupling as a
    feature. No test could then pair a collection with `Origin("global", ...)`,
    which is exactly the state a repo config with no key of its own produces —
    and the collection screen drew global's entries as its own rows all the way
    to a release.
    """
    base = st.open_editor(
        layer=layer,
        specs=specs,
        origins=resolve(specs, layer_set),
        layer_raw=raw_for(layer_set, layer),
    )
    return replace(base, **kwargs) if kwargs else base


_HOST_MOUNTS_SPEC = next(s for s in SPECS if s.path == ("host_mounts",))


def _mounts(entries):
    """`host_mounts:` YAML for `entries`, for `_layers`'s `repo_text`/`global_text`."""
    return yaml.safe_dump({"host_mounts": entries}, sort_keys=False)


def _collection_state(layer_set, layer="repo", **kwargs):
    """A state on the `host_mounts` collection screen over `layer_set`.

    The collection's contents are whatever `layer_set` holds for the open
    layer — there is no separate `entries` argument, because there is no
    honest way for one to exist: what a collection screen lists is the open
    layer's own value at that path and nothing else (spec 11.2).
    """
    return _state(layer_set, layer=layer, trail=("host_mounts",), **kwargs)


def _entry_state(layer_set, layer="repo", **kwargs):
    """A state on entry `0` of `host_mounts`, over `layer_set`."""
    return _state(layer_set, layer=layer, trail=("host_mounts", 0), **kwargs)


def test_the_fixture_schema_yields_the_same_screen_shapes_the_real_one_does(tmp_path):
    """`host_mounts` here carries a real `item_model`, as `repo_specs()` does.

    Without it `state.screen` never takes its collection branch and every test
    in this module would be asserting against a state shape the real schema can
    no longer produce — passing on a fiction. This test is the guard on the
    fixture itself, not on any renderer: what a collection screen *draws* is
    `collection_pane`'s, which does not exist yet.
    """
    layers = _layers(tmp_path)
    assert st.screen(_state(layers, trail=("host_mounts",))).kind == "collection"
    assert st.screen(_state(layers, trail=("gpg",))).kind == "fields"


def test_section_pane_lists_every_top_level_key_once(tmp_path):
    pane = section_pane(_state(_layers(tmp_path)))
    tokens = _text(pane.fragments).split()
    names = [tok for tok in tokens if tok not in {"▸", "·"}]
    assert names == ["gpg", "ssh", "egress_allow", "host_mounts", "github"]
    assert pane.cursor_row == 0


def test_field_pane_shows_the_saved_value_and_its_origin(tmp_path):
    layers = _layers(tmp_path, global_text="gpg:\n  enabled: true\n")
    pane = field_pane(_state(layers, trail=("gpg",)))
    text = _text(pane.fragments)
    assert "enabled" in text
    assert "true" in text
    assert "(global)" in text


def test_the_default_view_hides_advanced_fields_until_a_is_pressed(tmp_path):
    layers = _layers(tmp_path)
    assert "agent_forward" not in _text(field_pane(_state(layers, trail=("gpg",))).fragments)
    shown = field_pane(_state(layers, trail=("gpg",), show_all=True))
    assert "agent_forward" in _text(shown.fragments)


def test_a_staged_edit_is_marked_and_says_what_will_happen(tmp_path):
    layers = _layers(tmp_path, repo_text="gpg:\n  enabled: false\n")
    state = st.stage(_state(layers, trail=("gpg",)), ("gpg", "enabled"), True)
    text = _text(field_pane(state).fragments)
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
    state = st.reset_current(_state(layers, trail=("gpg",)))
    assert "→ reset" in _text(field_pane(state).fragments)


def test_a_no_op_edit_is_not_marked(tmp_path):
    """The marker and the `modified` counter come from the same change list."""
    layers = _layers(tmp_path, repo_text="gpg:\n  enabled: true\n")
    state = st.stage(_state(layers, trail=("gpg",)), ("gpg", "enabled"), True)
    assert "→" not in _text(field_pane(state).fragments)
    assert "modified: 0" in _text(title_bar(state, layers))


def test_search_rows_are_named_by_their_full_path(tmp_path):
    state = _state(_layers(tmp_path), query="enabled")
    text = _text(field_pane(state).fragments)
    assert "gpg.enabled" in text
    assert "ssh.enabled" in text


def test_title_bar_names_the_layer_the_file_and_the_pending_count(tmp_path):
    layers = _layers(tmp_path)
    text = _text(title_bar(_state(layers), layers))
    assert "repo" in text
    assert str(layers.repo_path) in text
    assert "modified: 0" in text


def test_help_pane_carries_the_description_and_the_default(tmp_path):
    layers = _layers(tmp_path)
    text = _text(help_pane(_state(layers, trail=("gpg",)), layers))
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
    text = _text(help_pane(_state(layers, trail=("egress_allow",)), layers))
    assert "global.example" in text


_API_TOKENS_SPEC = _spec("github.api_tokens", kind=FieldKind.STR_MAP, secret=True)


def _secret_map_layers(tmp_path, entries):
    """`global.yaml` holding `entries` under `github.api_tokens`.

    Global, because `github` is one of `GLOBAL_ONLY_KEYS`: a repo config
    carrying it is rejected by the loader, so the map's entries can only ever
    belong to the global layer.
    """
    return _layers(
        tmp_path, global_text=yaml.safe_dump({"github": {"api_tokens": entries}}, sort_keys=False)
    )


def _secret_map_state(layer_set, layer="global"):
    """A state on `github.api_tokens`'s own collection screen over `layer_set`.

    A secret map has no `item_model`, so this — trail pointing at the map
    itself — is as deep as the trail can go (`state.screen`'s guard).
    """
    return _state(
        layer_set, layer=layer, specs=(*SPECS, _API_TOKENS_SPEC), trail=_API_TOKENS_SPEC.path
    )


def _egress_state(layer_set, staged):
    """A repo-layer state on the `egress_allow` section with `staged` applied."""
    return _state(layer_set, staged=staged, trail=("egress_allow",))


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
    text = _text(help_pane(_egress_state(layers, {("egress_allow",): []}), layers))
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
    text = _text(help_pane(_egress_state(layers, {("egress_allow",): ["other.example"]}), layers))
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
    text = _text(help_pane(_egress_state(layers, {("egress_allow",): st.UNSET}), layers))
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
    layers = _layers(tmp_path, repo_text=_mounts([{"host": "/a", "container": "/data"}]))
    state = _collection_state(layers)

    got = collection_pane(state, layers)

    text = _text(got.fragments)
    assert "[0]" in text
    assert "/a" in text


def test_collection_pane_shows_inherited_entries_above_and_marks_them(tmp_path):
    """Repo-layer lists append to the global one; the global entries cannot be
    removed here (spec 11.2), so they must not look like rows the cursor owns.
    """
    layer_set = _layers(
        tmp_path,
        repo_text=_mounts([{"host": "/mine"}]),
        global_text="host_mounts:\n  - host: /inherited\n",
    )
    state = _collection_state(layer_set)

    text = _text(collection_pane(state, layer_set).fragments)

    assert text.index("/inherited") < text.index("/mine")
    assert "inherited from global" in text.casefold()


def test_collection_pane_says_so_when_the_collection_is_empty(tmp_path):
    layers = _layers(tmp_path)
    state = _collection_state(layers)

    text = _text(collection_pane(state, layers).fragments)

    assert "press `n`" in text


def test_field_pane_marks_an_entry_field_that_is_not_set(tmp_path):
    state = _entry_state(_layers(tmp_path, repo_text=_mounts([{"host": "/a"}])))

    text = _text(field_pane(state).fragments)

    assert "(set)" in text  # host
    assert "(default)" in text  # readonly


def test_help_pane_matches_the_row_for_an_unedited_entry_field(tmp_path):
    """`host_mounts.0.host`, never touched this session: the row reads
    `/home/dev/data (set)` because the entry as loaded carries the key.

    `help_pane` used to answer through `state.origins`, which holds no
    entry-level keys at all (spec 11.4) — so it fell back to `spec.default`
    and reported `Now: — (default)` under a row that said the opposite.
    """
    layers = _layers(tmp_path, repo_text=_mounts([{"host": "/home/dev/data"}]))
    state = _entry_state(layers)  # index 0 is "host"

    text = _text(help_pane(state, layers))

    assert "Now: /home/dev/data (set)" in text


def test_help_pane_matches_the_row_for_a_toggled_entry_field(tmp_path):
    """`host_mounts.0.readonly`, toggled with Space: the row reads
    `true (set)`. `help_pane` used to read the item model's static default
    (`readonly`'s is `false`) instead of the staged value, and labelled it
    `(default)` even though the row right above it said `(set)`.

    The "Default:" half must still name the model's own default (`false`),
    not the entry's staged value — only "Now:" and the origin follow the row.
    """
    layers = _layers(tmp_path, repo_text=_mounts([{"host": "/a"}]))
    state = _entry_state(layers, index=2)  # index 2 is "readonly"
    state = st.stage(state, (*state.trail, "readonly"), True)

    text = _text(help_pane(state, layers))

    assert "Default: false" in text
    assert "Now: true (set)" in text
    assert "(default)" not in text


def test_title_bar_shows_the_trail_once_inside_a_collection(tmp_path):
    layers = _layers(tmp_path, repo_text=_mounts([{"host": "/a"}]))
    state = _entry_state(layers)

    text = _text(title_bar(state, layers))

    assert "host_mounts ▸ 0" in text


def test_body_pane_dispatches_to_the_collection_pane_on_a_collection_screen(tmp_path):
    """`app.py` calls only `body_pane`; this pins that it actually reaches
    `collection_pane` rather than falling through to the (empty) field pane.
    """
    layers = _layers(tmp_path, repo_text=_mounts([{"host": "/a"}]))
    state = _collection_state(layers)

    text = _text(body_pane(state, layers).fragments)

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
    layers = _layers(tmp_path, repo_text=_mounts([{"host": "/a"}]))
    sections = (
        "host_mounts",
        "host_devices",
        "host_ports",
        "optional_mounts",
        "agents",
        "shared_caches",
    )
    for section in sections:
        state = _state(layers, specs=specs, trail=(section,))
        assert st.screen(state).kind == "collection", section
        text = _text(body_pane(state, layers).fragments)
        assert "press `a`" not in text, section
        if section == "host_mounts":
            assert "[0]" in text, section
            assert "host=/a" in text, section
        else:
            # Every other collection is absent from this repo layer, so the
            # screen lists nothing — `shared_caches` included, whose one
            # built-in default belongs to no layer and so is not this layer's
            # to edit (spec 4.3: a written-out default freezes).
            assert "press `n`" in text, section


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
    layers = _layers(tmp_path, repo_text="host_mounts:\n  - host: /old\n    container: /old\n")
    state = _collection_state(layers)
    state, crumb = st.add_entry(state, _HOST_MOUNTS_SPEC)
    state = st.enter_crumb(state, crumb)
    entry_specs = st.screen(state).specs
    host_spec = next(s for s in entry_specs if s.path[-1] == "host")
    state = st.stage(state, host_spec.path, "/new")

    pane = field_pane(state)
    staged_lines = [text for style, text in pane.fragments if style == "class:staged"]

    host_line = next(line for line in staged_lines if "host" in line)
    assert "●" in host_line
    assert "/new" in host_line
    # The untouched sibling field of the same (staged) entry must not be
    # marked — only `host` actually changed.
    container_line = next(text for _style, text in pane.fragments if "container" in text)
    assert "●" not in container_line


def test_edit_block_refuses_a_secret():
    """A scalar secret has no drill-down screen to make it safe — unlike
    `github.api_tokens` (a secret *map*), which is a drill-down since Task 9
    and is no longer refused here (see `test_a_secret_maps_keys_are_listed...`
    and `test_edit_block_still_refuses_a_secret_that_is_not_a_map`)."""
    spec = _spec("some.token", kind=FieldKind.STR, secret=True)
    reason = edit_block(spec, "global")
    assert reason is not None
    assert "0600" in reason


def test_edit_block_still_refuses_a_secret_that_is_not_a_map():
    """The screen is what makes a secret map safe; a scalar secret has none."""
    spec = _spec("some.token", FieldKind.STR, secret=True)

    assert edit_block(spec, "global") is not None


def test_edit_block_lets_a_secret_map_through():
    """The mirror of the refusal above: `github.api_tokens` is a `STR_MAP`
    with `secret=True`, and it is the map shape — not the secret flag alone —
    that now makes it editable."""
    spec = _spec("github.api_tokens", kind=FieldKind.STR_MAP, secret=True)
    assert edit_block(spec, "global") is None


def test_a_secret_maps_keys_are_listed_and_its_values_are_not(tmp_path):
    """The whole point of Task 9's screen: keys are listable, tokens are not
    — not even a masked stand-in sized to the real value."""
    layers = _secret_map_layers(tmp_path, {"gisgro": "ghp_realtoken", "personal": "ghp_other"})
    state = _secret_map_state(layers)

    text = "".join(t for _, t in collection_pane(state, layers).fragments)

    assert "gisgro" in text
    assert "personal" in text
    assert "ghp_realtoken" not in text
    assert "ghp_other" not in text
    assert "••••" in text


def test_a_secret_maps_mask_is_fixed_not_sized_to_the_value(tmp_path):
    """`assert "••••" in text` alone would still pass for a mask sized to the
    value (`"•" * len(value)`) — the length of a token is information too, so
    the mask must be a fixed string, and two entries with wildly different
    token lengths must render byte-identical value columns.
    """
    layers = _secret_map_layers(tmp_path, {"short": "a", "long": "a" * 97})
    state = _secret_map_state(layers)

    pane = collection_pane(state, layers)
    rows = [t for _, t in pane.fragments if "short" in t or "long" in t]

    assert len(rows) == 2
    # `rsplit` (not `split`) on the *last* double space: the cursor glyph
    # itself pads the row with a leading double space on a non-cursor line
    # ("  long  ••••••\n"), so splitting from the front would grab the label
    # instead of the mask for that row.
    masks = [row.rsplit("  ", 1)[1] for row in rows]
    assert masks[0] == masks[1] == f"{_SECRET_MASK}\n"


def test_footer_names_every_action_the_editor_offers(tmp_path):
    """The footer text itself, not the bindings — `_bindings` lives in
    `app.py` and nothing here reads it. What this pins is that no action
    quietly drops out of the one line the user is told to read.
    """
    text = _text(footer(_state(_layers(tmp_path))))
    for key in ("search", "toggle", "edit", "reset", "show all", "save", "quit"):
        assert key in text


def test_footer_names_the_collection_actions_on_a_collection_screen(tmp_path):
    """`footer` now takes the state, so it can offer different keys for a
    collection screen (`n`/`x`/`J`/`K`) instead of the section/field ones,
    which make no sense there (there is nothing to search or toggle)."""
    text = _text(footer(_collection_state(_layers(tmp_path))))
    for key in ("new", "delete", "move", "open", "save", "quit"):
        assert key in text


# -- a repo layer that inherits a whole collection from global -------------


_INHERITED_TEXT = "host_mounts:\n  - host: /g1\n  - host: /g2\n"


def test_collection_pane_draws_the_inherited_entries_once_and_only_dimmed(tmp_path):
    """The Critical: `entries()` used to report global's list as this layer's.

    With no `host_mounts:` in the repo config, `layers.resolve` marks the path
    `Origin("global", <global's list>)` — and `entries()` read that through
    `effective`, so `/g1` and `/g2` were drawn *twice*: dimmed under "Inherited
    from global", then again as cursor-addressable rows that `Enter`, `x` and
    `J`/`K` all acted on. This pins the pane's own docstring: the inherited
    block is read-only and the editable block is empty.
    """
    layers = _layers(tmp_path, global_text=_INHERITED_TEXT)
    state = _collection_state(layers)

    pane = collection_pane(state, layers)
    text = _text(pane.fragments)

    assert text.count("/g1") == 1
    assert text.count("/g2") == 1
    assert "press `n`" in text  # nothing here is this layer's to edit
    addressable = [chunk for style, chunk, *_ in pane.fragments if style != "class:dim"]
    assert addressable == []


def test_help_pane_on_a_collection_screen_describes_the_collection(tmp_path):
    """MINOR 3: it used to print "Pick a section, or press `/` to search every
    field" — `current()` is `None` on a collection screen — while the reader was
    standing inside `host_mounts`. The same contradiction the entry screen had.

    The count it adds is the *open layer's*, which is the one thing "Now:"
    cannot say: "Now:" reads through to global here, so without this line the
    pane would claim 2 entries over a screen listing none.
    """
    layers = _layers(tmp_path, global_text=_INHERITED_TEXT)

    text = _text(help_pane(_collection_state(layers), layers))

    assert "Pick a section" not in text
    assert "host_mounts" in text
    assert "what host_mounts does" in text
    assert "In this layer: 0 entries" in text


def test_help_pane_on_a_collection_counts_this_layers_own_entries(tmp_path):
    """The singular, and a layer that does own its entries."""
    layers = _layers(tmp_path, repo_text=_mounts([{"host": "/mine"}]))

    text = _text(help_pane(_collection_state(layers), layers))

    assert "In this layer: 1 entry" in text
