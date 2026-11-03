"""jq-compatible inbox rendering."""

import json
import re
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal


def clean_c0c1(s: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", s)


def clean_c1(s: str) -> str:
    return re.sub(r"[\x80-\x9f]", "?", s)


class SaturatedNumber:
    """A jq decimal outside its supported exponent range."""

    def __init__(self, text: str) -> None:
        self.text = text


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
    if isinstance(v, SaturatedNumber):
        return v.text
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


NAME_CAP = 64
SUBJECT_CAP = 120
COMMAND_CAP = 160
STATE_CAP = 24
TABLE_SUBJECT = 40
TABLE_COMMAND = 40
STATUS_COLUMNS = (
    "NAME",
    "STATE",
    "AGENT",
    "ACTIVE",
    "CPU",
    "MEM",
    "HEAD",
    "DIRTY",
    "LONGEST",
    "LAST-MSG",
)


def _num(value: object) -> float | None:
    """A finite real number, or None (bools, strings, NaN, inf, saturated all unknown)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: object, cap: int) -> str:
    """A child-supplied string, sanitised and capped; anything else is unknown."""
    if not isinstance(value, str):
        return "?"
    return clean_c0c1(value[:cap]) or "?"


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def format_age(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    return f"{total // 3600}h{total % 3600 // 60}m"


def _age(stamp: object, now: float) -> str:
    value = _num(stamp)
    return "?" if value is None else format_age(now - value)


def _memory(value: object) -> str:
    number = _num(value)
    if number is None or number < 0:
        return "?"
    if number < 1024**3:
        return f"{number / 1024**2:.0f}MiB"
    return f"{number / 1024**3:.1f}GiB"


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _status_row(child: object, now: float, from_child: Mapping[str, float]) -> list[str]:
    item = _mapping(child) or {}
    raw_name = item.get("name")
    name = _text(raw_name, NAME_CAP)
    if item.get("done") is True:
        state = "done"
    elif item.get("running") is True:
        state = "running"
    elif item.get("running") is False:
        state = "stopped"
    else:
        state = "?"
    status = _mapping(item.get("status")) or {}
    agent = _mapping(status.get("agent"))
    alive = agent.get("alive") if agent is not None else None
    if agent is not None and alive is True:
        agent_cell = "alive:" + _text(agent.get("state"), STATE_CAP)
    elif agent is not None and alive is False:
        agent_cell = "dead"
    else:
        agent_cell = "?"
    cpu = _num(status.get("cpu_percent"))
    cpu_cell = "?" if cpu is None or cpu < 0 else f"{cpu:.0f}%"
    head = _mapping(status.get("head"))
    if head is None:
        head_cell = "?"
    else:
        sha = _text(head.get("sha"), 40)[:7]
        subject = _shorten(_text(head.get("subject"), SUBJECT_CAP), TABLE_SUBJECT)
        head_cell = f"{sha} {_age(head.get('time'), now)} {subject}"
    dirty = status.get("dirty")
    dirty_cell = "yes" if dirty is True else "no" if dirty is False else "?"
    if "longest_command" not in status or (
        status["longest_command"] is not None and _mapping(status["longest_command"]) is None
    ):
        longest_cell = "?"
    else:
        longest = _mapping(status["longest_command"])
        if longest is None:
            longest_cell = "-"
        else:
            seconds = _num(longest.get("seconds"))
            command = _shorten(_text(longest.get("command"), COMMAND_CAP), TABLE_COMMAND)
            longest_cell = f"{command} {'?' if seconds is None else format_age(seconds)}"
    sent = from_child.get(raw_name) if isinstance(raw_name, str) else None
    last_msg = f"↑{_age(sent, now)} ↓{_age(status.get('last_to_child_at'), now)}"
    return [
        name,
        state,
        agent_cell,
        _age(status.get("activity_at"), now),
        cpu_cell,
        _memory(status.get("memory_bytes")),
        head_cell,
        dirty_cell,
        longest_cell,
        last_msg,
    ]


def render_status(
    children: Sequence[object], *, now: float, from_child: Mapping[str, float]
) -> str:
    """The `inbox status` table. Pure; tolerates any shape of the published items."""
    rows = [list(STATUS_COLUMNS)] + [_status_row(c, now, from_child) for c in children]
    widths = [max(len(row[i]) for row in rows) for i in range(len(STATUS_COLUMNS) - 1)]
    lines = []
    for row in rows:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])] + [row[-1]]
        lines.append("  ".join(cells))
    return "\n".join(lines)
