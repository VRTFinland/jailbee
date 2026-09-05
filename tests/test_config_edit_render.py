"""What the editor draws, without a terminal.

Every function under test is `state -> fragments`, the same split
`dashboard_settings.render_settings` uses on the Rich side. The assertions read
the flattened text, so they survive a restyling.
"""

from __future__ import annotations

from jailbee.config_edit import state as st
from jailbee.config_edit.layers import Origin, read_layers
from jailbee.config_edit.render import (
    edit_block,
    field_pane,
    footer,
    help_pane,
    section_pane,
    title_bar,
)
from jailbee.config_edit.schema import FieldKind, FieldSpec


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
    _spec("host_mounts", kind=FieldKind.MODEL_LIST, default=[]),
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


def test_section_pane_lists_every_top_level_key_once():
    pane = section_pane(_state())
    tokens = _text(pane.fragments).split()
    names = [tok for tok in tokens if tok not in {"▸", "·"}]
    assert names == ["gpg", "ssh", "egress_allow", "host_mounts", "github"]
    assert pane.cursor_row == 0


def test_field_pane_shows_the_saved_value_and_its_origin(tmp_path):
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("gpg", "enabled")] = Origin("global", True)
    pane = field_pane(_state(section="gpg", origins=origins), _layers(tmp_path))
    text = _text(pane.fragments)
    assert "enabled" in text
    assert "true" in text
    assert "(global)" in text


def test_the_default_view_hides_advanced_fields_until_a_is_pressed(tmp_path):
    layers = _layers(tmp_path)
    assert "agent_forward" not in _text(field_pane(_state(section="gpg"), layers).fragments)
    shown = field_pane(_state(section="gpg", show_all=True), layers)
    assert "agent_forward" in _text(shown.fragments)


def test_a_staged_edit_is_marked_and_says_what_will_happen(tmp_path):
    layers = _layers(tmp_path, repo_text="gpg:\n  enabled: false\n")
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("gpg", "enabled")] = Origin("repo", False)
    state = st.stage(_state(section="gpg", origins=origins), ("gpg", "enabled"), True)
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
    state = st.reset_current(_state(section="gpg", origins=origins), layers.repo_raw)
    assert "→ reset" in _text(field_pane(state, layers).fragments)


def test_a_no_op_edit_is_not_marked(tmp_path):
    """The marker and the `modified` counter come from the same change list."""
    layers = _layers(tmp_path, repo_text="gpg:\n  enabled: true\n")
    origins = {s.path: Origin("default", s.default) for s in SPECS}
    origins[("gpg", "enabled")] = Origin("repo", True)
    state = st.stage(_state(section="gpg", origins=origins), ("gpg", "enabled"), True)
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
    state = _state(section="gpg")
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
        section="egress_allow",
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
        section="egress_allow",
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


def test_edit_block_refuses_a_model_collection_for_now():
    reason = edit_block(_spec("host_mounts", kind=FieldKind.MODEL_LIST), "repo")
    assert reason is not None
    assert "by hand" in reason


def test_edit_block_refuses_a_secret():
    spec = _spec("github.api_tokens", kind=FieldKind.STR_MAP, secret=True)
    reason = edit_block(spec, "global")
    assert reason is not None
    assert "0600" in reason


def test_footer_names_the_keys_the_bindings_actually_have():
    text = _text(footer())
    for key in ("search", "toggle", "edit", "reset", "show all", "save", "quit"):
        assert key in text
