"""Dashboard-only cell and header presentation over canonical ls fields."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from rich.markup import escape

from jailbee import background
from jailbee.lifecycle import (
    DOING_MAX_NAMES,
    agent_compact_cell,
    format_duration_short,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.lifecycle import ContainerInfo
    from jailbee.table_format import FieldSpec


_HEADER_LABELS = {
    "state": "ST",
    "network": "LOOSE",
    "mounts": "MOUNT",
    "created": "AGE",
    "full_name": "FULL",
    "memory_limit": "LIMIT",
    "mem_used": "USED",
    "loose_until": "UNTIL",
    "agent_compact": "AI",
    "issues": "ISS",
    "target_diff": "DIFF",
    "local_diff": "L DIFF",
    "git_status": "GIT",
}
_STATE_GLYPHS = {"Running": "▶", "Stopped": "■", "Frozen": "Ⅱ"}


def transition_glyph(container: ContainerInfo) -> str | None:
    """The state a live job is taking ``container`` to, as a yellow glyph; None if none.

    Incus reports Running for the whole of a clean shutdown and Stopped until
    a boot's `incus start` returns, so the state alone lags the action by as
    long as the action takes. A live job row is what knows better.
    """
    if container.job_phase is None or container.job_pid is None:
        return None
    if background.clearable(container.job_phase, container.job_pid):
        return None
    if container.job_phase == background.PHASE_STOPPING:
        return "[yellow]■[/yellow]"
    if (
        container.job_kind == background.JOB_BOOT
        and container.job_phase == background.PHASE_STARTING
    ):
        return "[yellow]▶[/yellow]"
    return None


def autostart_failed(container: ContainerInfo) -> bool:
    """Whether the container's autostart run ended in failure (or its worker died)."""
    if container.job_kind != background.JOB_AUTOSTART or container.job_phase is None:
        return False
    if container.job_pid is None:
        return container.job_phase in background.TERMINAL_PHASES
    return background.clearable(container.job_phase, container.job_pid)


def state_label(state: str) -> str:
    """The state as the details panel words it: glyph and name, escaped."""
    glyph = _STATE_GLYPHS.get(state)
    return f"{glyph} {escape(state)}" if glyph else escape(state)


_MEM_SEPARATOR_RE = re.compile(r"\s*/\s*")

RECENT_IDLE = timedelta(minutes=30)
"""An idle agent with a real event younger than this is highlighted in AI."""

# One flag for "on, and for how long": LOOSE and MOUNT both lead with it.
_ON_MARK = "[red]●[/red]"


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


def _loose(container: ContainerInfo, now: datetime) -> str:
    """LOOSE: empty when strict, a red ● and the remaining TTL when loose."""
    if container.network == "strict":
        return ""
    if container.network != "loose":
        return escape(container.network or "-")
    if container.loose_until is None:
        return f"{_ON_MARK} ∞"
    compact = format_duration_short(container.loose_until - now).replace(" ", "")
    return f"{_ON_MARK} {compact}"


def card_network(container: ContainerInfo, now: datetime) -> str:
    """Network label for cards: the table's LOOSE cell."""
    return _loose(container, now)


_MOUNT_OFF = "dim"
_MOUNT_FOREVER = "bold red"
# Time left, coloured by how long the credential stays exposed: the first
# bucket whose limit exceeds the remainder wins. Anything longer is orange.
_MOUNT_BUCKETS = (
    (timedelta(minutes=15), "bold cyan"),
    (timedelta(hours=1), "bold green"),
    (timedelta(hours=4), "bold yellow"),
)
_MOUNT_LONG = "bold dark_orange"


def _mount_kinds(container: ContainerInfo) -> tuple[str, ...]:
    """The repo's configured kinds in config order, then attached kinds it lacks."""
    extra = tuple(k for k in container.optional_mounts if k not in container.configured_mounts)
    return container.configured_mounts + extra


def _mount_letters(kinds: Sequence[str]) -> list[str]:
    """One upper-case letter per kind: its first letter not already taken by an earlier one."""
    taken: set[str] = set()
    letters: list[str] = []
    for kind in kinds:
        pool = [ch.upper() for ch in kind if ch.isalnum()] or ["?"]
        letter = next((ch for ch in pool if ch not in taken), pool[0])
        taken.add(letter)
        letters.append(letter)
    return letters


def _mount_style(container: ContainerInfo, kind: str, now: datetime) -> str:
    """Rich style of one kind: dim when off, else by the time it has left."""
    if kind not in container.optional_mounts:
        return _MOUNT_OFF
    until = container.mount_until.get(kind)
    if until is None:
        return _MOUNT_FOREVER
    left = until - now
    return next((style for limit, style in _MOUNT_BUCKETS if left < limit), _MOUNT_LONG)


def _mounts(container: ContainerInfo, now: datetime) -> str:
    """One letter per mount kind, dim when off, else coloured by the time left."""
    kinds = _mount_kinds(container)
    cells = []
    for kind, letter in zip(kinds, _mount_letters(kinds), strict=True):
        style = _mount_style(container, kind, now)
        cells.append(f"[{style}]{letter}[/{style}]")
    return "".join(cells)


def mount_names_markup(container: ContainerInfo, now: datetime) -> str:
    """Every mount kind by name in the MOUNT colours; attached ones carry their time left."""
    parts = []
    for kind in _mount_kinds(container):
        style = _mount_style(container, kind, now)
        if kind in container.optional_mounts:
            until = container.mount_until.get(kind)
            rest = "∞" if until is None else format_duration_short(until - now).replace(" ", "")
            label = f"{escape(kind)} {rest}"
        else:
            label = escape(kind)
        parts.append(f"[{style}]{label}[/{style}]")
    return ", ".join(parts)


def doing_cell(container: ContainerInfo) -> str:
    """The busy processes as compact markup, ``""`` when there are none."""
    if not container.activity:
        return ""
    names = [
        escape(p.comm) if p.count == 1 else f"{escape(p.comm)}×{p.count}"  # noqa: RUF001 - intentional multiplication sign
        for p in container.activity[:DOING_MAX_NAMES]
    ]
    hidden = len(container.activity) - len(names)
    if hidden:
        names.append(f"[dim]+{hidden}[/dim]")
    return ",".join(names)


def dashboard_cell(field: FieldSpec[ContainerInfo], container: ContainerInfo, now: datetime) -> str:
    """Return a dashboard-specific value, keeping unknown text safely renderable."""
    value = field.cell(container)
    if field.name == "state":
        return transition_glyph(container) or _STATE_GLYPHS.get(
            container.state, escape(container.state)
        )
    if field.name == "network":
        return _loose(container, now)
    if field.name == "mounts":
        return _mounts(container, now)
    if field.name == "role":
        if value == "child!":
            return "[red]child![/red]"
        if value == "orch":
            return "[bold]orch[/bold]"
        return value
    if field.name == "pr":
        # The outbox count lives in OUTBOX; `jailbee ls` keeps it in PR.
        if container.pr_number is None:
            return ""
        return f"#{container.pr_number}" if container.pr_author else f"#{container.pr_number}↓"
    if field.name == "agent_compact":
        return agent_compact_cell(
            container.agent_status, now, recent_idle=RECENT_IDLE, activity_age=True
        )
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
        return doing_cell(container)
    if field.name in ("wt", "target_diff", "local_diff") and container.git_status is not None:
        if getattr(container.git_status, field.name) == "clean":
            return "[dim]✓[/dim]"
    if field.name == "ttl":
        return value.replace(" ", "")
    if field.name == "mem":
        return _MEM_SEPARATOR_RE.sub("/", value)
    return value
