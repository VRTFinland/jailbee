"""Raw layer reading and per-path origin resolution.

The editor never reads a merged `Config`: merging destroys the
information about which file a value came from, which is exactly what
every row's origin marker shows (spec 3.3).
"""

from __future__ import annotations

import pytest

from jailbee.config_edit import layers
from jailbee.config_edit.schema import repo_specs
from jailbee.config_writer import DELETE, YamlChange


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_read_layers_tolerates_missing_files(tmp_path):
    """A repo with no config and a host with no global.yaml is a real state.

    `jb new` works in a directory with no config at all, so the editor
    opening there must show defaults rather than crash.
    """
    got = layers.read_layers(tmp_path / "nope.yaml", tmp_path / "gone.yaml")
    assert got.repo_raw == {}
    assert got.global_raw == {}


def test_resolve_reports_default_when_neither_layer_sets_it(tmp_path):
    got = layers.read_layers(tmp_path / "nope.yaml", tmp_path / "gone.yaml")
    origins = layers.resolve(repo_specs(), got)
    assert origins[("gpg", "enabled")].source == "default"
    assert origins[("gpg", "enabled")].value is False


def test_repo_wins_over_global_wins_over_default(tmp_path):
    _write(tmp_path / "global.yaml", "gpg:\n  enabled: true\ndefaults:\n  cpu: 2\n")
    _write(tmp_path / "repo" / "config.yaml", "defaults:\n  cpu: 8\n")
    got = layers.read_layers(tmp_path / "repo" / "config.yaml", tmp_path / "global.yaml")
    origins = layers.resolve(repo_specs(), got)

    assert origins[("defaults", "cpu")] == layers.Origin("repo", 8)
    assert origins[("gpg", "enabled")] == layers.Origin("global", True)
    assert origins[("ssh", "enabled")].source == "default"


def test_an_explicit_null_is_a_set_value_not_an_absent_one(tmp_path):
    """`chrome.url: null` is a deliberate choice, distinct from not setting it.

    Treating None as absent would make the origin marker lie and would
    make reset a no-op on a key the user did set.
    """
    _write(tmp_path / "global.yaml", "chrome:\n  url: null\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")
    origins = layers.resolve(repo_specs(), got)
    assert origins[("chrome", "url")] == layers.Origin("global", None)


def test_lookup_distinguishes_absent_from_none():
    assert layers.lookup({"a": {"b": None}}, ("a", "b")) == (True, None)
    assert layers.lookup({"a": {}}, ("a", "b")) == (False, None)
    assert layers.lookup({}, ("a", "b")) == (False, None)


def test_lookup_does_not_walk_through_a_scalar():
    """A malformed file must not raise from the editor's read path."""
    assert layers.lookup({"gpg": "yes"}, ("gpg", "enabled")) == (False, None)


def test_raw_for_selects_the_open_layer(tmp_path):
    _write(tmp_path / "global.yaml", "defaults:\n  cpu: 2\n")
    _write(tmp_path / "repo.yaml", "defaults:\n  cpu: 8\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")
    assert layers.raw_for(got, "repo") == {"defaults": {"cpu": 8}}
    assert layers.raw_for(got, "global") == {"defaults": {"cpu": 2}}


def _spec(dotted):
    wanted = tuple(dotted.split("."))
    return next(s for s in repo_specs() if s.path == wanted)


def test_github_is_disabled_in_the_repo_layer():
    reason = layers.disabled_reason(_spec("github.api_tokens"), "repo")
    assert reason is not None
    assert "global.yaml" in reason


def test_github_is_editable_in_the_global_layer():
    assert layers.disabled_reason(_spec("github.api_tokens"), "global") is None


def test_ordinary_fields_are_never_disabled():
    assert layers.disabled_reason(_spec("gpg.enabled"), "repo") is None
    assert layers.disabled_reason(_spec("gpg.enabled"), "global") is None


def test_the_opaque_scratch_overlay_is_disabled_in_both_layers():
    """`scratch.config` is free-form: there is no form to render for it."""
    from jailbee.config_edit.schema import global_specs

    spec = next(s for s in global_specs() if s.path == ("scratch", "config"))
    # Test both layers as the name promises
    reason_global = layers.disabled_reason(spec, "global")
    assert reason_global is not None
    assert "by hand" in reason_global
    reason_repo = layers.disabled_reason(spec, "repo")
    assert reason_repo is not None
    assert "by hand" in reason_repo


def test_repo_layer_shows_the_global_list_entries_it_will_append_to(tmp_path):
    """deep_merge appends lists, so the global entries are not being replaced."""
    _write(tmp_path / "global.yaml", "egress_allow:\n  - a.example\n  - b.example\n")
    _write(tmp_path / "repo.yaml", "egress_allow:\n  - c.example\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")

    assert layers.inherited_entries(_spec("egress_allow"), got, "repo") == (
        "a.example",
        "b.example",
    )


def test_the_global_layer_inherits_nothing(tmp_path):
    _write(tmp_path / "global.yaml", "egress_allow:\n  - a.example\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")
    assert layers.inherited_entries(_spec("egress_allow"), got, "global") == ()


def test_non_list_fields_inherit_nothing(tmp_path):
    """Scalars override rather than append; showing context would be a lie."""
    _write(tmp_path / "global.yaml", "defaults:\n  cpu: 2\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")
    assert layers.inherited_entries(_spec("defaults.cpu"), got, "repo") == ()


def test_an_explicit_null_in_the_repo_layer_is_also_a_set_value(tmp_path):
    """`chrome.url: null` in the repo layer is the twin of the global test.

    This closes a gap: `test_an_explicit_null_is_a_set_value_not_an_absent_one`
    only covers the global layer. Both branches of `resolve()` are
    structurally identical, so a mutation breaking only the repo one would
    pass the suite without this twin.
    """
    _write(tmp_path / "repo.yaml", "chrome:\n  url: null\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")
    origins = layers.resolve(repo_specs(), got)
    assert origins[("chrome", "url")] == layers.Origin("repo", None)


def test_an_explicit_empty_list_in_the_repo_layer_resets_not_appends(tmp_path):
    """An explicit empty list in the repo layer resets, discarding global entries.

    `deep_merge` treats `egress_allow: []` in the repo config as a reset
    that discards the global list entirely, not as an append that would add
    to it. So `inherited_entries` returns `()` for a repo layer holding an
    explicit empty list, even if the global layer has entries — nothing is
    inherited when the list is explicitly reset.
    """
    _write(tmp_path / "global.yaml", "egress_allow:\n  - a.example\n  - b.example\n")
    _write(tmp_path / "repo.yaml", "egress_allow: []\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")

    assert layers.inherited_entries(_spec("egress_allow"), got, "repo") == ()


def test_host_level_list_paths_inherit_nothing(tmp_path):
    """`ls`/`dashboard` never reach `deep_merge`, so nothing is appended.

    `_split_host_keys` routes the `_HOST_LEVEL_KEYS` out of the
    Config overlay before the merge runs, and `Config._effective_columns`
    merges the two blocks field-by-field with `model_copy(update=...)` —
    a repo `hide` *replaces* the global one. Reporting the global entries
    as inherited context here would tell the user they are appending to a
    list that in fact gets thrown away.
    """
    _write(tmp_path / "global.yaml", "ls:\n  hide:\n    - ip\n")
    _write(tmp_path / "repo.yaml", "ls:\n  hide:\n    - branch\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")

    assert layers.inherited_entries(_spec("ls.hide"), got, "repo") == ()


def test_an_explicit_null_in_the_repo_layer_discards_the_global_list(tmp_path):
    """`null` is a reset too — it just takes `deep_merge`'s other branch.

    Only two lists hit the append rule. A `None` overlay falls through to
    the scalar/type-mismatch branch where the overlay simply wins, so the
    global entries are gone; the same is true of any non-list value a
    hand-edited file might hold there.
    """
    _write(tmp_path / "global.yaml", "egress_allow:\n  - a.example\n")
    _write(tmp_path / "repo.yaml", "egress_allow: null\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")

    assert layers.inherited_entries(_spec("egress_allow"), got, "repo") == ()


def test_a_non_list_in_the_repo_layer_discards_the_global_list(tmp_path):
    """A hand-broken scalar where a list belongs also wins outright."""
    _write(tmp_path / "global.yaml", "egress_allow:\n  - a.example\n")
    _write(tmp_path / "repo.yaml", "egress_allow: a.example\n")
    got = layers.read_layers(tmp_path / "repo.yaml", tmp_path / "global.yaml")

    assert layers.inherited_entries(_spec("egress_allow"), got, "repo") == ()


def test_apply_changes_does_not_mutate_the_input():
    raw = {"defaults": {"cpu": 2}}
    out = layers.apply_changes(raw, [YamlChange(("defaults", "cpu"), 8)])
    assert out == {"defaults": {"cpu": 8}}
    assert raw == {"defaults": {"cpu": 2}}


def test_apply_changes_creates_missing_parents_and_deletes():
    out = layers.apply_changes({}, [YamlChange(("gpg", "enabled"), True)])
    assert out == {"gpg": {"enabled": True}}
    assert layers.apply_changes(out, [YamlChange(("gpg", "enabled"), DELETE)]) == {"gpg": {}}


@pytest.fixture
def opened(tmp_path, monkeypatch, mocker):
    """A `LayerSet` over an isolated global.yaml, with git detection stubbed.

    `validate` runs the real loader, which calls `detect_default_branch`
    and `detect_upstream_remote` against a tmp dir that is not a git
    repo; unpatched they shell out. `XDG_CONFIG_HOME` is what makes
    `default_global_config_path()` — which the loader calls itself —
    point at the same file this fixture wrote.
    """
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.loader.detect_upstream_remote", return_value="origin")

    def _open(global_text=""):
        from jailbee.global_config import default_global_config_path

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        global_path = default_global_config_path()
        global_path.parent.mkdir(parents=True, exist_ok=True)
        global_path.write_text(global_text)
        return layers.read_layers(tmp_path / "repo" / ".jailbee" / "config.yaml", global_path)

    return _open


def test_validate_accepts_a_good_repo_change(opened):
    got = opened()
    assert layers.validate(got, "repo", [YamlChange(("defaults", "cpu"), 8)]) is None


def test_validate_rejects_a_bad_value_and_names_the_field(opened):
    got = opened()
    error = layers.validate(got, "repo", [YamlChange(("defaults", "cpu"), "lots")])
    assert error is not None
    assert "cpu" in error


def test_validate_checks_a_staged_global_layer_without_writing_it(opened):
    """The whole reason load_config_from_layers exists."""
    got = opened("defaults:\n  cpu: 2\n")

    error = layers.validate(got, "global", [YamlChange(("defaults", "cpu"), "lots")])

    assert error is not None
    assert got.global_path.read_text() == "defaults:\n  cpu: 2\n", "nothing was written"


def test_validate_catches_a_cross_field_rule_not_just_the_schema(opened):
    """`github.enabled` with no tokens is a loader rule, not a pydantic one.

    Validating with `Config.model_validate` alone would let it through and
    the next CLI command would fail instead.
    """
    got = opened()

    error = layers.validate(got, "global", [YamlChange(("github", "enabled"), True)])

    assert error is not None
    assert "api_tokens" in error


def test_validate_rejects_a_malformed_host_level_block(opened):
    """`docker_registry_mirror` is host-level: the loader never sees it.

    `load_config_from_layers` splits the `_HOST_LEVEL_KEYS` off and uses
    them for one thing only, so without a `validate_global_raw` pass the
    editor would happily write a `global.yaml` that every later
    `_load_unsanitized` rejects outright.
    """
    got = opened()

    error = layers.validate(
        got, "global", [YamlChange(("docker_registry_mirror", "port"), "not-a-number")]
    )

    assert error is not None
    assert "docker_registry_mirror" in error


def test_validate_rejects_a_malformed_column_block(opened):
    """`ls.hide` must be a list; `_load_unsanitized` will not recover it."""
    got = opened()

    error = layers.validate(got, "global", [YamlChange(("ls", "hide"), "not-a-list")])

    assert error is not None
    assert "ls" in error


def test_validate_rejects_a_malformed_scratch_block(opened):
    got = opened()

    error = layers.validate(got, "global", [YamlChange(("scratch", "enabled"), "maybe")])

    assert error is not None
    assert "scratch" in error


def test_validate_accepts_a_good_host_level_change(opened):
    """The host-level pass must not reject what the CLI would accept."""
    got = opened()

    assert layers.validate(got, "global", [YamlChange(("ls", "hide"), ["ip"])]) is None


def test_validate_leaves_the_in_memory_layers_untouched(opened):
    """A rejected save must not corrupt the editor's live view of the file."""
    got = opened("defaults:\n  cpu: 2\n")

    layers.validate(got, "global", [YamlChange(("defaults", "cpu"), "lots")])

    assert got.global_raw == {"defaults": {"cpu": 2}}
