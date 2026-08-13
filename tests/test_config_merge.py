"""Tests for deep_merge — pure helper used by the layered config loader.

Rules:
  * scalars: overlay overrides base; None clears
  * lists:   overlay appended; [] resets to empty
  * dicts:   recursive deep_merge per key
"""

from jailbee.config import deep_merge


def test_empty_inputs():
    assert deep_merge({}, {}) == {}


def test_scalar_override():
    assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_scalar_none_clears():
    assert deep_merge({"a": 1}, {"a": None}) == {"a": None}


def test_scalar_added_by_overlay():
    assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_list_append():
    assert deep_merge({"xs": [1, 2]}, {"xs": [3]}) == {"xs": [1, 2, 3]}


def test_empty_list_resets():
    assert deep_merge({"xs": [1, 2]}, {"xs": []}) == {"xs": []}


def test_list_added_by_overlay():
    assert deep_merge({"a": 1}, {"xs": [1]}) == {"a": 1, "xs": [1]}


def test_dict_recursive_merge():
    base = {"d": {"a": 1, "b": 2}}
    overlay = {"d": {"b": 3, "c": 4}}
    assert deep_merge(base, overlay) == {"d": {"a": 1, "b": 3, "c": 4}}


def test_nested_list_inside_dict_appends():
    base = {"defaults": {"egress_allow": ["a"]}}
    overlay = {"defaults": {"egress_allow": ["b"]}}
    assert deep_merge(base, overlay) == {"defaults": {"egress_allow": ["a", "b"]}}


def test_type_mismatch_overlay_wins():
    # Base has a list, overlay has a scalar of a different shape.
    # This is a "user shape change" — overlay wins outright.
    assert deep_merge({"x": [1, 2]}, {"x": "scalar"}) == {"x": "scalar"}


def test_map_of_models_new_keys_added():
    base = {"optional_mounts": {"m2": {"host": "~/.m2"}}}
    overlay = {"optional_mounts": {"aws": {"host": "~/.aws"}}}
    expected = {
        "optional_mounts": {
            "m2": {"host": "~/.m2"},
            "aws": {"host": "~/.aws"},
        }
    }
    assert deep_merge(base, overlay) == expected


def test_map_of_models_same_key_deep_merges():
    base = {"optional_mounts": {"m2": {"host": "~/.m2", "readonly": True}}}
    overlay = {"optional_mounts": {"m2": {"readonly": False}}}
    expected = {
        "optional_mounts": {
            "m2": {"host": "~/.m2", "readonly": False},
        }
    }
    assert deep_merge(base, overlay) == expected


def test_does_not_mutate_inputs():
    base = {"xs": [1], "d": {"a": 1}}
    overlay = {"xs": [2], "d": {"a": 2}}
    deep_merge(base, overlay)
    assert base == {"xs": [1], "d": {"a": 1}}
    assert overlay == {"xs": [2], "d": {"a": 2}}
