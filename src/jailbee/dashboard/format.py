"""Dashboard-only cell and header presentation over canonical ls fields."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from rich.markup import escape

from jailbee import background
from jailbee.lifecycle import DOING_MAX_NAMES, agent_compact_cell, format_duration_short

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
    "target_diff": "DIFF",
    "local_diff": "L DIFF",
    "git_status": "GIT",
}
_STATE_GLYPHS = {"Running": "▶", "Stopped": "■", "Frozen": "Ⅱ"}
_MEM_SEPARATOR_RE = re.compile(r"\s*/\s*")

RECENT_IDLE = timedelta(minutes=30)
"""An idle agent younger than this reads as "just finished" in AI."""


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


def _network(container: ContainerInfo) -> str:
    if container.network == "strict":
        return "●"
    if container.network == "loose":
        return "○"
    return escape(container.network or "-")


def card_network(container: ContainerInfo, now: datetime) -> str:
    """Network label for cards, where the loose TTL remains useful inline."""
    mode = _network(container)
    if container.network != "loose":
        return mode
    if container.loose_until is None:
        return f"{mode} ∞"
    compact = format_duration_short(container.loose_until - now).replace(" ", "")
    return f"{mode} {compact}"


def dashboard_cell(field: FieldSpec[ContainerInfo], container: ContainerInfo, now: datetime) -> str:
    """Return a dashboard-specific value, keeping unknown text safely renderable."""
    value = field.cell(container)
    if field.name == "state":
        return _STATE_GLYPHS.get(container.state, escape(container.state))
    if field.name == "network":
        return _network(container)
    if field.name == "agent_compact":
        return agent_compact_cell(container.agent_status, now, recent_idle=RECENT_IDLE)
    if field.name == "created":
        return _age(container.created_at, now)
    if field.name == "base":
        base = escape(container.base_branch or "—")
        tracking = (
            container.base_branch
            and container.git_status
            and container.git_status.base_source == "tracking"
        )
        return f"{base} ↗" if tracking else base
    if field.name == "mode":
        return {"clone": "cln", "mount": "mnt"}.get(container.mode, escape(container.mode))
    if field.name == "job":
        if container.job_phase is None:
            return ""
        label = background.job_label_or_empty(
            container.job_phase, container.job_pid, kind=container.job_kind
        )
        phase, suffix = (
            (label[: -len(background.DEAD_SUFFIX)], background.DEAD_SUFFIX)
            if label.endswith(background.DEAD_SUFFIX)
            else (label, "")
        )
        if phase.startswith("autostart:"):
            phase = "auto:" + phase[len("autostart:") :]
        else:
            phase = {
                "starting": "start",
                "creating": "create",
                "cloning": "clone",
                "stopping": "stop",
                "deleting": "delete",
                "destroying": "destroy",
            }.get(phase, phase)
        dead = (
            background.clearable(container.job_phase, container.job_pid)
            if container.job_pid is not None
            else container.job_phase in background.TERMINAL_PHASES
        )
        colour = "red" if dead else "yellow"
        return f"[{colour}]{escape(phase + suffix)}[/{colour}]"
    if field.name == "doing" and container.activity:
        names = [
            escape(p.comm) if p.count == 1 else f"{escape(p.comm)}×{p.count}"  # noqa: RUF001 - intentional multiplication sign
            for p in container.activity[:DOING_MAX_NAMES]
        ]
        hidden = len(container.activity) - len(names)
        if hidden:
            names.append(f"[dim]+{hidden}[/dim]")
        return ",".join(names)
    if field.name in ("wt", "target_diff", "local_diff") and container.git_status is not None:
        if getattr(container.git_status, field.name) == "clean":
            return "[dim]✓[/dim]"
    if field.name == "ttl":
        return value.replace(" ", "")
    if field.name == "mem":
        return _MEM_SEPARATOR_RE.sub("/", value)
    return value
