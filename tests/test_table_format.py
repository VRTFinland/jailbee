"""Tests for the generic table/JSON output helper."""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO

import pytest
import typer
from rich.console import Console

from jailbee import table_format
from jailbee.table_format import FieldSpec, emit


@dataclass
class Row:
    name: str
    age: int
    extra: str | None = None


FIELDS: list[FieldSpec[Row]] = [
    FieldSpec(name="name", header="NAME", cell=lambda r: r.name, json=lambda r: r.name),
    FieldSpec(
        name="age",
        header="AGE",
        cell=lambda r: str(r.age),
        json=lambda r: r.age,
        justify="right",
    ),
    FieldSpec(
        name="extra",
        header="EXTRA",
        cell=lambda r: r.extra or "—",
        json=lambda r: r.extra,
        default_table=False,
        default_json=False,
    ),
    FieldSpec(
        name="dyn",
        header="DYN",
        cell=lambda r: "x",
        json=lambda r: "x",
        default_json=False,
        show_if=lambda rows: any(r.age > 30 for r in rows),
    ),
    FieldSpec(
        name="nested",
        header="NESTED",
        cell=lambda r: f"a={r.age}",
        json=lambda r: {"age": r.age, "name": r.name},
        default_table=False,
        default_json=False,
    ),
]


def _capture() -> Console:
    return Console(file=StringIO(), force_terminal=False, width=200, no_color=True)


def test_json_output_default_fields(capsys: pytest.CaptureFixture[str]) -> None:
    rows = [Row("a", 10), Row("b", 25)]
    emit(rows, FIELDS, fmt="json", fields=None, console=_capture())
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data == [
        {"name": "a", "age": 10},
        {"name": "b", "age": 25},
    ]


def test_json_output_selected_fields(capsys: pytest.CaptureFixture[str]) -> None:
    rows = [Row("a", 10, extra="x")]
    emit(rows, FIELDS, fmt="json", fields="name,extra,nested", console=_capture())
    data = json.loads(capsys.readouterr().out)
    assert data == [{"name": "a", "extra": "x", "nested": {"age": 10, "name": "a"}}]


def test_json_output_empty_list(capsys: pytest.CaptureFixture[str]) -> None:
    emit([], FIELDS, fmt="json", fields=None, console=_capture())
    assert json.loads(capsys.readouterr().out) == []


def test_table_output_default_fields() -> None:
    console = _capture()
    assert isinstance(console.file, StringIO)
    rows = [Row("a", 10), Row("b", 25)]
    emit(rows, FIELDS, fmt="table", fields=None, console=console)
    out = console.file.getvalue()
    assert "NAME" in out
    assert "AGE" in out
    assert "EXTRA" not in out  # default_table=False
    assert "DYN" not in out  # show_if false for ages <= 30


def test_table_show_if_includes_column_when_predicate_true() -> None:
    console = _capture()
    assert isinstance(console.file, StringIO)
    rows = [Row("a", 50)]
    emit(rows, FIELDS, fmt="table", fields=None, console=console)
    out = console.file.getvalue()
    assert "DYN" in out


def test_table_explicit_fields_override_show_if() -> None:
    console = _capture()
    assert isinstance(console.file, StringIO)
    rows = [Row("a", 10)]
    emit(rows, FIELDS, fmt="table", fields="name,dyn", console=console)
    out = console.file.getvalue()
    assert "DYN" in out


def test_table_explicit_fields_includes_non_default() -> None:
    console = _capture()
    assert isinstance(console.file, StringIO)
    rows = [Row("a", 10, extra="hi")]
    emit(rows, FIELDS, fmt="table", fields="name,extra", console=console)
    out = console.file.getvalue()
    assert "EXTRA" in out
    assert "hi" in out
    assert "AGE" not in out


def test_unknown_field_raises_bad_parameter() -> None:
    with pytest.raises(typer.BadParameter) as ei:
        emit([], FIELDS, fmt="table", fields="name,bogus", console=_capture())
    msg = str(ei.value)
    assert "bogus" in msg
    assert "name" in msg  # lists allowed fields


def test_empty_fields_string_raises() -> None:
    with pytest.raises(typer.BadParameter):
        emit([], FIELDS, fmt="table", fields="  ,  ", console=_capture())


def test_unknown_format_raises() -> None:
    with pytest.raises(typer.BadParameter):
        emit([], FIELDS, fmt="yaml", fields=None, console=_capture())


def test_empty_message_printed_for_table_only() -> None:
    console = _capture()
    assert isinstance(console.file, StringIO)
    emit(
        [],
        FIELDS,
        fmt="table",
        fields=None,
        console=console,
        empty_message="(none)",
    )
    assert "(none)" in console.file.getvalue()


def test_empty_message_not_printed_for_json(capsys: pytest.CaptureFixture[str]) -> None:
    emit([], FIELDS, fmt="json", fields=None, console=_capture(), empty_message="(none)")
    out = capsys.readouterr().out
    assert "(none)" not in out
    assert json.loads(out) == []


_FIELDS_WITH_FOOTER: list[FieldSpec[Row]] = [
    FieldSpec(
        name="name",
        header="NAME",
        cell=lambda r: r.name,
        json=lambda r: r.name,
        footer=lambda _rows: "TOTAL",
    ),
    FieldSpec(
        name="age",
        header="AGE",
        cell=lambda r: str(r.age),
        json=lambda r: r.age,
        footer=lambda rs: str(sum(r.age for r in rs)),
    ),
]


def test_footer_row_rendered_in_table_mode() -> None:
    console = _capture()
    assert isinstance(console.file, StringIO)
    emit(
        [Row("a", 10), Row("b", 25)],
        _FIELDS_WITH_FOOTER,
        fmt="table",
        fields=None,
        console=console,
    )
    out = console.file.getvalue()
    assert "TOTAL" in out
    assert "35" in out


def test_footer_omitted_in_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    emit(
        [Row("a", 10), Row("b", 25)],
        _FIELDS_WITH_FOOTER,
        fmt="json",
        fields=None,
        console=_capture(),
    )
    data = json.loads(capsys.readouterr().out)
    assert data == [{"name": "a", "age": 10}, {"name": "b", "age": 25}]


def test_footer_skipped_for_empty_rows() -> None:
    console = _capture()
    assert isinstance(console.file, StringIO)
    emit([], _FIELDS_WITH_FOOTER, fmt="table", fields=None, console=console)
    assert "TOTAL" not in console.file.getvalue()


def _specs() -> list[FieldSpec[dict[str, str]]]:
    return [
        FieldSpec(name="name", header="NAME", cell=lambda r: r["name"], json=lambda r: r["name"]),
        FieldSpec(name="wt", header="WT", cell=lambda r: r["wt"], json=lambda r: r["wt"]),
    ]


def test_emit_renders_sub_rows_in_table() -> None:
    console = Console(record=True, width=80)
    rows = [{"name": "feat-x", "wt": "clean"}]
    table_format.emit(
        rows,
        _specs(),
        fmt="table",
        fields=None,
        console=console,
        sub_rows=lambda r: [{"name": "  └ deps/libfoo", "wt": "+3 -0"}],
    )
    text = console.export_text()
    assert "feat-x" in text
    assert "└ deps/libfoo" in text
    assert "+3 -0" in text


def test_emit_sub_rows_ignored_in_json(capsys: pytest.CaptureFixture[str]) -> None:
    console = Console(record=True, width=80)
    rows: list[dict[str, str]] = [{"name": "feat-x", "wt": "clean"}]
    table_format.emit(
        rows,
        _specs(),
        fmt="json",
        fields=None,
        console=console,
        sub_rows=lambda r: [{"name": "  └ deps/libfoo", "wt": "+3 -0"}],
    )
    out = capsys.readouterr().out
    assert "libfoo" not in out  # sub_rows never affect JSON


def _spec(name: str, *, default_table: bool = True):
    from jailbee.table_format import FieldSpec

    return FieldSpec(
        name=name,
        header=name.upper(),
        cell=lambda r: str(r),
        json=lambda r: r,
        default_table=default_table,
    )


def test_apply_column_config_is_a_noop_when_unset():
    from jailbee.table_format import apply_column_config

    all_fields = [_spec("a"), _spec("b", default_table=False)]

    out = apply_column_config(all_fields, fields=None, hide=[])

    assert [f.name for f in out] == ["a", "b"]
    assert [f.default_table for f in out] == [True, False]


def test_apply_column_config_explicit_fields_win_and_keep_their_order():
    from jailbee.table_format import apply_column_config

    all_fields = [_spec("a"), _spec("b", default_table=False), _spec("c")]

    out = apply_column_config(all_fields, fields=["c", "b"], hide=["c"])

    # `hide` is ignored when `fields` is set, and an off-by-default field
    # named explicitly becomes visible.
    assert [f.name for f in out] == ["c", "b"]
    assert all(f.default_table for f in out)


def test_apply_column_config_hide_only_affects_the_default_set():
    from jailbee.table_format import apply_column_config

    all_fields = [_spec("a"), _spec("b")]

    out = apply_column_config(all_fields, fields=None, hide=["b"])

    # The field is still present — so `--fields b` can still ask for it —
    # but no longer part of the default table.
    assert [f.name for f in out] == ["a", "b"]
    assert [f.default_table for f in out] == [True, False]


def test_apply_column_config_ignores_an_unknown_name():
    """validate_runtime reports typos; the renderer must not crash on one."""
    from jailbee.table_format import apply_column_config

    out = apply_column_config([_spec("a")], fields=["a", "nosuch"], hide=[])

    assert [f.name for f in out] == ["a"]
