"""Raw layer reading and per-path origin resolution.

The editor never reads a merged `Config`: merging destroys the
information about which file a value came from, which is exactly what
every row's origin marker shows (spec 3.3).
"""

from __future__ import annotations

from jailbee.config_edit import layers
from jailbee.config_edit.schema import repo_specs


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
