"""Generic table + JSON output for list-style commands.

Each command provides a list of :class:`FieldSpec` entries describing
its columns. :func:`emit` then renders the rows as either a Rich
table or a JSON document, honouring an optional ``--fields`` filter
and per-field dynamic visibility.

Conventions:

* ``FieldSpec.name`` is the canonical key — what users pass to
  ``--fields`` and what appears as a key in JSON output. Use
  ``snake_case`` and keep it stable across releases.
* ``cell`` may return Rich markup; ``json`` must return a
  JSON-serialisable value (use ``None`` for missing data).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import typer
from rich.console import Console
from rich.table import Table

Justify = Literal["default", "left", "center", "right", "full"]


@dataclass(frozen=True)
class FieldSpec[T]:
    name: str
    header: str
    cell: Callable[[T], str]
    json: Callable[[T], Any]
    justify: Justify = "left"
    default_table: bool = True
    default_json: bool = True
    # Whether the column belongs to a *dashboard*'s default set, as opposed
    # to a one-shot table's. ``None`` — the usual case — means "same as
    # ``default_table``". Set it explicitly only for a column whose value
    # differs between the two: a live monitor can justify a column that
    # a one-shot listing cannot (MEM changes every refresh; IP is worth a
    # glance in a view you leave open), and the reverse is equally possible.
    # Read it through :func:`shows_by_default_in_dashboard`, never directly.
    default_dashboard: bool | None = None
    show_if: Callable[[Sequence[T]], bool] | None = None
    # Optional per-column aggregator rendered as a footer row in table
    # mode only. If any visible field defines a footer, a separator row
    # and the footer row are appended below the data rows. JSON output
    # ignores footers — callers can aggregate the raw values themselves.
    footer: Callable[[Sequence[T]], str] | None = None


def shows_by_default_in_dashboard[T](field: FieldSpec[T]) -> bool:
    """Whether ``field`` is in a dashboard's default column set.

    The single reader of :attr:`FieldSpec.default_dashboard`, which falls
    back to :attr:`FieldSpec.default_table` when unset. Dashboards filter
    with this instead of ``default_table`` directly so that a column can be
    off by default in ``jailbee ls`` while staying on in the live views.
    """
    if field.default_dashboard is None:
        return field.default_table
    return field.default_dashboard


def _parse_fields[T](
    spec_str: str | None,
    all_fields: Sequence[FieldSpec[T]],
    fmt: str,
) -> list[FieldSpec[T]]:
    by_name = {f.name: f for f in all_fields}
    if spec_str is None:
        if fmt == "table":
            return [f for f in all_fields if f.default_table]
        return [f for f in all_fields if f.default_json]
    requested = [s.strip() for s in spec_str.split(",") if s.strip()]
    if not requested:
        raise typer.BadParameter("--fields must list at least one field name")
    out: list[FieldSpec[T]] = []
    for name in requested:
        if name not in by_name:
            allowed = ", ".join(sorted(by_name))
            raise typer.BadParameter(f"unknown field {name!r}; allowed: {allowed}")
        out.append(by_name[name])
    return out


def apply_column_config[T](
    all_fields: Sequence[FieldSpec[T]],
    *,
    fields: list[str] | None,
    hide: Sequence[str],
) -> list[FieldSpec[T]]:
    """Narrow a field list to the user's remembered column preference.

    ``fields`` is an explicit ordered selection: the result is exactly those
    fields, in that order, each forced ``default_table=True`` (and
    ``default_dashboard=True``, so the same request is honoured in the live
    views) so an off-by-default column the user asked for actually shows,
    and its ``show_if`` cleared to ``None``. Naming a column is a request for that
    exact column: the format default doesn't apply (handled by forcing
    ``default_table``) and neither does the emptiness heuristic (handled by
    clearing ``show_if``) — an explicitly named column renders even when no
    row would otherwise justify it. This is the one place that rule lives;
    every caller that narrows via ``fields`` (``jailbee ls``'s configured column
    list, the dashboard's) inherits it for free, rather than each
    reimplementing "explicit beats show_if" itself.

    ``hide`` is subtractive and applies only when ``fields`` is None: the
    named fields stay in the list — so ``--fields`` can still reach them —
    but drop out of the default table *and* out of the dashboard default,
    so hiding a column hides it wherever the block applies. ``show_if`` is untouched for the
    ``hide`` branch: the built-in default set is not an explicit request for
    any one column, so the emptiness heuristic still prunes it.

    Note this is orthogonal to ``emit()``'s own ``fields: str | None``
    parameter (an on-the-fly ``--fields`` flag): that mechanism already
    skips ``show_if`` for its own explicit selection and is untouched here —
    see ``emit()``'s docstring. The two never compound: callers pass this
    function's ``fields`` only when the CLI flag was *not* given (see
    ``cli.py``'s ``ls`` command).

    Unknown names are ignored rather than raising: a rendering path is the
    wrong place to fail. For the repo layer that is the only safety net —
    `Config.validate_runtime` (surfaced by `jailbee config validate`) reports a
    typo there, but nothing sanitizes it before it reaches here. The global
    layer no longer relies on this tolerance the same way: a typo'd name in
    `global.yaml` is dropped by `global_config.load_global_config` before
    the block gets anywhere near this function (`jailbee config validate`
    reports that one too, via `global_config.global_config_issues`). Never
    mutates the input specs.
    """
    by_name = {f.name: f for f in all_fields}
    if fields is not None:
        return [
            replace(by_name[n], default_table=True, default_dashboard=True, show_if=None)
            for n in fields
            if n in by_name
        ]
    hidden = set(hide)
    return [
        replace(f, default_table=False, default_dashboard=False) if f.name in hidden else f
        for f in all_fields
    ]


def emit[T](
    rows: Sequence[T],
    all_fields: Sequence[FieldSpec[T]],
    *,
    fmt: str,
    fields: str | None,
    console: Console,
    title: str | None = None,
    empty_message: str | None = None,
    sub_rows: Callable[[T], list[dict[str, str]]] | None = None,
) -> None:
    """Render ``rows`` as either a Rich table or JSON.

    ``fmt`` must be ``"table"`` or ``"json"``. ``fields``, if given, is
    a comma-separated list of :attr:`FieldSpec.name` values that
    overrides the format-specific defaults and disables ``show_if``.
    """
    if fmt not in ("table", "json"):
        raise typer.BadParameter(f"unknown format {fmt!r}; allowed: table, json")

    selected = _parse_fields(fields, all_fields, fmt)

    if fmt == "json":
        payload = [{f.name: f.json(r) for f in selected} for r in rows]
        # Use print() so the output is unstyled and pipeable, regardless
        # of the Rich console's terminal detection.
        print(json.dumps(payload, indent=2, default=str))
        return

    if fields is None:
        visible = [f for f in selected if f.show_if is None or f.show_if(rows)]
    else:
        visible = list(selected)

    has_footer = bool(rows) and any(f.footer is not None for f in visible)
    table = Table(title=title, show_footer=has_footer) if title else Table(show_footer=has_footer)
    for f in visible:
        footer_text = f.footer(rows) if (has_footer and f.footer) else ""
        table.add_column(f.header, justify=f.justify, footer=footer_text)
    for r in rows:
        table.add_row(*(f.cell(r) for f in visible))
        if sub_rows is not None:
            for sr in sub_rows(r):
                table.add_row(*(sr.get(f.name, "") for f in visible))
    console.print(table)
    if not rows and empty_message:
        console.print(empty_message)
