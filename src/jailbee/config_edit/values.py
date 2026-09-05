"""Text in, typed value out — the editor's whole editing vocabulary.

Every field is edited as a line (or a block) of text, so each kind needs one
function to seed the editor and one to read the result back. Both are pure, so
what the editor accepts is testable without a terminal.

Two spelling rules hold across the module:

* **Booleans are words.** `true/yes/on` and `false/no/off`, never `1`/`0`.
  `LooseAutoRevert.after` is `str | int`, so a `SCALAR_UNION` has to read a
  bare number *as a number*; one rule for the whole module beats a per-kind
  exception, and a `BOOL` field typing `1` gets a clear error rather than a
  guess.
* **`CHOICE`'s choices are authoritative, `SCALAR_UNION`'s are a hint**
  (spec 10.3). An off-list `CHOICE` is rejected; a `SCALAR_UNION` keeps free
  text legal, because its open arm is the whole point.

Nothing here validates against the model — that is `layers.validate`'s job at
save time, running the real loader. These functions only decide what the typed
text *means*.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from jailbee.config_edit.schema import FieldKind

if TYPE_CHECKING:
    from jailbee.config_edit.schema import FieldSpec

_TRUE = frozenset({"true", "yes", "on"})
_FALSE = frozenset({"false", "no", "off"})
_INT_RE = re.compile(r"[+-]?[0-9]+")

_MISSING = "—"
"""What an unset value looks like in the field list.

An em dash rather than an empty cell: a blank would be indistinguishable from
a rendering bug, and from the empty string, which is a legitimate value.
"""


def _parse_bool(raw: str) -> bool | None:
    low = raw.casefold()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return None


def format_value(spec: FieldSpec, value: object) -> str:
    """One-line display text for `value` under `spec`.

    A secret field reports its size, never its content (spec 3.4): this string
    is painted on a terminal that may be shared, recorded or scrolled back.
    Collections report their size too — a row is one line, and the drill-down
    screen is where the entries belong.
    """
    if spec.secret:
        count = len(value) if isinstance(value, dict) else (0 if value is None else 1)
        return f"{count} entr{'y' if count == 1 else 'ies'} (hidden)"
    if value is None:
        return _MISSING
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return f"[{len(value)}]" if value else "[]"
    if isinstance(value, dict):
        return f"{{{len(value)}}}" if value else "{}"
    return str(value)


def to_text(spec: FieldSpec, value: object) -> str:
    """The text the modal editor opens with for a scalar field.

    `None` seeds an empty line rather than the word "None": committing the
    line unchanged must leave the field unset, not store a string.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def list_to_text(value: object) -> str:
    """One entry per line — the seed for a `STR_LIST` editor."""
    if not isinstance(value, (list, tuple)):
        return ""
    return "".join(f"{entry}\n" for entry in value)


def map_to_text(value: object) -> str:
    """`key = value` per line — the seed for a `STR_MAP`/`BOOL_MAP` editor."""
    if not isinstance(value, dict):
        return ""
    out = []
    for key, entry in value.items():
        if entry is None:
            out.append(f"{key} = null\n")
        elif isinstance(entry, bool):
            out.append(f"{key} = {'true' if entry else 'false'}\n")
        else:
            out.append(f"{key} = {entry}\n")
    return "".join(out)


def parse_value(spec: FieldSpec, text: str) -> tuple[object, str | None]:
    """`(value, error)` for a scalar field. Exactly one half is `None`."""
    raw = text.strip()
    if raw == "":
        if spec.optional:
            return None, None
        return None, f"`{spec.label}` cannot be empty."
    if spec.kind is FieldKind.BOOL:
        parsed = _parse_bool(raw)
        if parsed is None:
            return None, "Expected true or false."
        return parsed, None
    if spec.kind is FieldKind.INT:
        try:
            return int(raw), None
        except ValueError:
            return None, f"Expected a whole number, got {raw!r}."
    if spec.kind is FieldKind.CHOICE:
        for choice in spec.choices:
            if raw == str(choice):
                return choice, None
        allowed = ", ".join(str(c) for c in spec.choices)
        return None, f"Expected one of: {allowed}."
    if spec.kind is FieldKind.SCALAR_UNION:
        return _parse_union(spec, raw), None
    return raw, None


def _parse_union(spec: FieldSpec, raw: str) -> object:
    """The arm `raw` belongs to, preferring a declared one over free text.

    `str | int` must store 300 as an int: the model would accept `"300"`, but
    the file would then disagree with every hand-written example and with what
    `render_documented` emits for the same value.
    """
    if any(isinstance(choice, bool) for choice in spec.choices):
        parsed = _parse_bool(raw)
        if parsed is not None:
            return parsed
    for choice in spec.choices:
        if not isinstance(choice, bool) and raw == str(choice):
            return choice
    if _INT_RE.fullmatch(raw):
        return int(raw)
    return raw


def parse_list(spec: FieldSpec, text: str) -> tuple[list[str] | None, str | None]:
    """`(entries, error)` for a `STR_LIST`, one entry per non-blank line.

    `egress_allow` gets its real parser run per row (spec 4.3), because a
    malformed entry there is only caught at ACL-build time otherwise — long
    after the save that introduced it.
    """
    entries = [line.strip() for line in text.splitlines() if line.strip()]
    if spec.path == ("egress_allow",):
        from jailbee.egress import parse_egress_entry

        for lineno, entry in enumerate(entries, start=1):
            try:
                parse_egress_entry(entry)
            except ValueError as exc:
                return None, f"Line {lineno}: {exc}"
    return entries, None


def parse_map(spec: FieldSpec, text: str) -> tuple[dict[str, object] | None, str | None]:
    """`(mapping, error)` for a `STR_MAP`/`BOOL_MAP`, one `key = value` per line.

    `null` is spelled out rather than inferred from an empty right-hand side:
    `claude_credentials.repos` uses an explicit YAML null to opt one repo out
    of every credential group, and the empty string is a different, legitimate
    value. Guessing between them would silently change what a config means.
    """
    out: dict[str, object] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        entry = line.strip()
        if not entry:
            continue
        if "=" not in entry:
            return None, f"Line {lineno}: expected `key = value`, got {entry!r}."
        key, _, value = entry.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            return None, f"Line {lineno}: the key is empty."
        if key in out:
            return None, f"Line {lineno}: duplicate key {key!r}."
        if spec.kind is FieldKind.BOOL_MAP:
            parsed = _parse_bool(value)
            if parsed is None:
                return None, f"Line {lineno}: expected true or false, got {value!r}."
            out[key] = parsed
        elif value.casefold() == "null":
            out[key] = None
        else:
            out[key] = value
    return out, None
