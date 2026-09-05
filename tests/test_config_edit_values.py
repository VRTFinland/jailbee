"""Text <-> typed value, per field kind.

Pure functions, so the whole editing vocabulary is testable without a
terminal.
"""

from __future__ import annotations

import pytest

from jailbee.config_edit.schema import FieldKind, FieldSpec
from jailbee.config_edit.values import (
    format_value,
    parse_list,
    parse_map,
    parse_value,
    to_text,
)


def _spec(kind, *, choices=(), optional=False, secret=False, label="field"):
    return FieldSpec(
        path=(label,),
        label=label,
        kind=kind,
        description="help",
        default=None,
        choices=choices,
        optional=optional,
        secret=secret,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (True, "true"),
        (False, "false"),
        ("idea", "idea"),
        (42, "42"),
        ([], "[]"),
        (["a", "b"], "[2]"),
        ({}, "{}"),
        ({"a": 1}, "{1}"),
    ],
)
def test_format_value_is_one_line_per_shape(value, expected):
    assert format_value(_spec(FieldKind.STR), value) == expected


def test_format_value_never_prints_a_secret():
    spec = _spec(FieldKind.STR_MAP, secret=True)
    rendered = format_value(spec, {"github.com": "ghp_realtoken"})
    assert "ghp_realtoken" not in rendered
    assert "1 entry (hidden)" == rendered


def test_to_text_seeds_the_editor_with_an_editable_spelling():
    assert to_text(_spec(FieldKind.BOOL), True) == "true"
    assert to_text(_spec(FieldKind.STR), None) == ""
    assert to_text(_spec(FieldKind.INT), 8) == "8"


def test_parse_value_reads_booleans_as_words_only():
    spec = _spec(FieldKind.BOOL)
    assert parse_value(spec, "true") == (True, None)
    assert parse_value(spec, " YES ") == (True, None)
    assert parse_value(spec, "off") == (False, None)
    value, error = parse_value(spec, "1")
    assert value is None
    assert "true or false" in error


def test_parse_value_rejects_a_non_number_for_an_int():
    value, error = parse_value(_spec(FieldKind.INT), "eight")
    assert value is None
    assert "whole number" in error
    assert parse_value(_spec(FieldKind.INT), "8") == (8, None)


def test_parse_value_enforces_a_closed_choice_list():
    spec = _spec(FieldKind.CHOICE, choices=("idea", "pycharm"))
    assert parse_value(spec, "pycharm") == ("pycharm", None)
    value, error = parse_value(spec, "vscode")
    assert value is None
    assert "idea, pycharm" in error


def test_parse_value_keeps_free_text_legal_for_a_scalar_union():
    """`LooseAutoRevert.after` is `str | int`: "5m" and 300 are both real."""
    spec = _spec(FieldKind.SCALAR_UNION, choices=())
    assert parse_value(spec, "5m") == ("5m", None)
    assert parse_value(spec, "300") == (300, None)


def test_parse_value_prefers_a_boolean_arm_when_the_union_has_one():
    spec = _spec(FieldKind.SCALAR_UNION, choices=("auto", True, False))
    assert parse_value(spec, "auto") == ("auto", None)
    assert parse_value(spec, "true") == (True, None)
    assert parse_value(spec, "yes") == (True, None)


def test_parse_value_empty_text_clears_an_optional_field_and_fails_otherwise():
    assert parse_value(_spec(FieldKind.STR, optional=True), "  ") == (None, None)
    value, error = parse_value(_spec(FieldKind.STR), "")
    assert value is None
    assert "cannot be empty" in error


def test_parse_list_drops_blank_lines_and_keeps_order():
    spec = _spec(FieldKind.STR_LIST, label="egress_allow")
    assert parse_list(spec, "a.example\n\n  b.example  \n") == (["a.example", "b.example"], None)
    assert parse_list(spec, "") == ([], None)


def test_parse_list_validates_every_egress_entry():
    """`egress_allow` has a real parser; a bad row must not reach the file."""
    spec = _spec(FieldKind.STR_LIST, label="egress_allow")
    value, error = parse_list(spec, "good.example\nnot a host\n")
    assert value is None
    assert "Line 2" in error


def test_parse_map_reads_key_equals_value_lines():
    spec = _spec(FieldKind.STR_MAP)
    assert parse_map(spec, "a = 1\nb=2\n") == ({"a": "1", "b": "2"}, None)


def test_parse_map_spells_null_explicitly():
    spec = _spec(FieldKind.STR_MAP)
    assert parse_map(spec, "solo = null\n") == ({"solo": None}, None)
    assert parse_map(spec, "solo =\n") == ({"solo": ""}, None)


def test_parse_map_reports_the_offending_line():
    spec = _spec(FieldKind.STR_MAP)
    for text, needle in (
        ("nope\n", "key = value"),
        ("a=1\na=2\n", "duplicate"),
        (" = 1\n", "empty"),
    ):
        value, error = parse_map(spec, text)
        assert value is None
        assert needle in error
        assert "Line" in error


def test_parse_map_parses_booleans_for_a_bool_map():
    spec = _spec(FieldKind.BOOL_MAP, label="pooled_caches")
    assert parse_map(spec, "npm = true\nuv = false\n") == ({"npm": True, "uv": False}, None)
    value, error = parse_map(spec, "npm = maybe\n")
    assert value is None
    assert "true or false" in error
