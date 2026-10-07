"""Dashboard-only cell and header presentation over canonical ls fields."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from rich.markup import escape

from jailbee.lifecycle import format_duration_short

if TYPE_CHECKING:
    from jailbee.lifecycle import ContainerInfo
    from jailbee.table_format import FieldSpec


_HEADER_LABELS = {
    "state": "ST",
    "network": "NET",
    "created": "AGE",
    "full_name": "FULL",
    "memory_limit": "LIMIT",
    "loose_until": "UNTIL",
    "agent_compact": "AI",
    "issues": "ISS",
}
_STATE_GLYPHS = {"Running": "▶", "Stopped": "■", "Frozen": "Ⅱ"}
_MEM_SEPARATOR_RE = re.compile(r"\s*/\s*")


def dashboard_header(field: FieldSpec[ContainerInfo]) -> str:
    """Compact selected dashboard labels without changing canonical specs."""
    return _HEADER_LABELS.get(field.name, field.header)


def _age(created_at: datetime | None, now: datetime) -> str:
    if created_at is None:
        return "—"
    seconds = max(0, int((now - created_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _network(container: ContainerInfo, now: datetime) -> str:
    if container.network != "loose":
        return "S" if container.network == "strict" else escape(container.network or "-")
    if container.loose_until is None:
        return "L ∞"
    compact = format_duration_short(container.loose_until - now).replace(" ", "")
    return f"L {compact}"


def dashboard_cell(field: FieldSpec[ContainerInfo], container: ContainerInfo, now: datetime) -> str:
    """Return a dashboard-specific value, keeping unknown text safely renderable."""
    value = field.cell(container)
    if field.name == "state":
        return _STATE_GLYPHS.get(container.state, escape(container.state))
    if field.name == "network":
        return _network(container, now)
    if field.name == "created":
        return _age(container.created_at, now)
    if field.name == "mem":
        return _MEM_SEPARATOR_RE.sub("/", value)
    return value
