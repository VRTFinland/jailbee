"""jq-compatible inbox rendering."""

import json
import re
from collections.abc import Sequence
from decimal import Decimal


def clean_c0c1(s: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", s)


def clean_c1(s: str) -> str:
    return re.sub(r"[\x80-\x9f]", "?", s)


def _number(value: float | Decimal) -> str:
    text = str(value)
    if isinstance(value, Decimal) and value.is_finite():
        decimal_exponent = value.as_tuple().exponent
        if value.adjusted() < -6 or (isinstance(decimal_exponent, int) and decimal_exponent > 0):
            text = format(value, "E")
    if "e" not in text.lower():
        return text
    number = Decimal(text)
    if number.is_finite() and -6 <= number.adjusted() < 0:
        return format(number, "f")
    coefficient, exponent = re.split("[eE]", text)
    exponent_value = int(exponent)
    if exponent_value == 0:
        return coefficient
    return f"{coefficient}E{exponent_value:+d}"


def _json(v: object, indent: int | None = None, level: int = 0) -> str:
    if isinstance(v, (float, Decimal)):
        return _number(v)
    if isinstance(v, (list, dict)):
        items = list(v.items()) if isinstance(v, dict) else list(enumerate(v))
        if not items:
            return "{}" if isinstance(v, dict) else "[]"
        parts = []
        for key, value in items:
            prefix = (
                _json(key) + (": " if indent is not None else ":") if isinstance(v, dict) else ""
            )
            parts.append(prefix + _json(value, indent, level + 1))
        opening, closing = ("{", "}") if isinstance(v, dict) else ("[", "]")
        if indent is None:
            return opening + ",".join(parts) + closing
        pad = " " * (indent * (level + 1))
        return (
            opening
            + "\n"
            + pad
            + (",\n" + pad).join(parts)
            + "\n"
            + " " * (indent * level)
            + closing
        )
    return json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f")


def jq_tostring(v: object) -> str:
    if isinstance(v, str):
        return v
    return _json(v)


def tsv(cells: Sequence[str]) -> str:
    return "\t".join(
        s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
        for s in cells
    )


def pretty(v: object) -> str:
    return _json(v, indent=2)


def _alternative(*values: object) -> object:
    return next((v for v in values if v is not None and v is not False), None)


def ls_line(doc: dict[str, object]) -> str:
    kind = doc.get("type")
    text = jq_tostring(_alternative(doc.get("body"), doc.get("detail"), ""))[:60]
    if kind == "message":
        sha = doc.get("imported_sha")
        if sha is not None and sha is not False:
            if not isinstance(sha, str):
                raise ValueError("invalid imported sha")
            text += f" [imported {sha[:12]}]"
        if doc.get("import_error") is not None and doc.get("import_error") is not False:
            text += " [import failed]"
        reason = doc.get("on_done_reason")
        if reason is not None and reason is not False:
            text += f" [kept: {jq_tostring(reason)[:60]}]"
    cells = [
        doc.get("id"),
        kind,
        doc.get("ref" if kind == "status" else "from"),
        doc.get("state") if kind == "status" else _alternative(doc.get("kind"), ""),
        text,
    ]
    return tsv([clean_c0c1("-" if v is None else jq_tostring(v)) for v in cells])
