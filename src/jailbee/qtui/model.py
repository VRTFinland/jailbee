"""Pure transforms from jailbee's container data to plain display values.

Framework-free (no PySide6, though Rich — a shared, non-GUI dependency — is
used to strip markup from ``FieldSpec.cell`` output). Cell text comes from
``FieldSpec.cell`` (the same human-readable rendering the TUI table uses),
with any Rich markup tags stripped, rather than ``FieldSpec.json`` (whose
value may be structured data, e.g. ``outbox``'s json is a ``{"pr", "issues"}``
dict — not something we want to stringify straight into a cell).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.text import Text

from jailbee.agent_activity import describe
from jailbee.background import DEAD_SUFFIX, job_label_or_empty
from jailbee.dashboard import format as dashboard_format
from jailbee.git_status import IN_PROGRESS_CELL_LABELS

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup
    from jailbee.lifecycle import ContainerInfo
    from jailbee.table_format import FieldSpec

# Fallback for the rare case a cell value confuses Rich's markup parser.
_MARKUP_TAG_RE = re.compile(r"\[/?[^\]]*\]")


def _strip_markup(raw: str) -> str:
    """Plain text for a (possibly Rich-markup) cell string."""
    try:
        return Text.from_markup(raw).plain
    except Exception:
        return _MARKUP_TAG_RE.sub("", raw)


def container_cells(c: ContainerInfo, fields: list[FieldSpec[ContainerInfo]]) -> list[str]:
    """Plain per-column text for one container (Rich markup stripped)."""
    return [_strip_markup(f.cell(c)) for f in fields]


def group_header(group: RepoGroup) -> tuple[str, bool]:
    """Return ``(label, is_orphan)`` for a repo group header row."""
    is_orphan = group.repo_root is None
    label = f"{group.prefix}  (orphan)" if is_orphan else group.prefix
    return label, is_orphan


def column_headers(fields: list[FieldSpec[ContainerInfo]]) -> list[str]:
    """Column header strings in display order."""
    return [f.header for f in fields]


# State -> foreground colour (hex). Framework-free; Qt modules wrap these in
# QColor. Single source of truth shared by the table and card views.
STATE_COLORS: dict[str, str] = {
    "Running": "#2e7d32",  # green
    "Stopped": "#9e9e9e",  # grey
    "Frozen": "#1565c0",  # blue
}

# Cell strings that carry no information — dropped from card chips.
_CARD_PLACEHOLDERS = frozenset({"", "-", "—"})


@dataclass(frozen=True)
class CardField:
    """One displayable field of a container card."""

    name: str  # FieldSpec.name, e.g. "network"
    header: str  # display label, e.g. "NETWORK"
    value: str  # stripped cell text


@dataclass(frozen=True)
class CardContent:
    """Display pieces for one container card (framework-free)."""

    name: str
    state: str
    fields: list[CardField]  # all non-name/non-state visible fields, in order
    # Recorded job failure message, shown as the job badge's tooltip. Part of
    # the value so an equality check picks up a changed error on refresh.
    job_error: str | None = None
    # The AGENT line's tooltip (each agent's `waiting_for`) and whether any
    # agent is waiting, which colours the line. Part of the value for the
    # same reason as `job_error`.
    agent_tooltip: str | None = None
    agent_waiting: bool = False
    # Raw `limits.memory`, the memory chip's tooltip: the card shows used and
    # MEM% only.
    memory_limit: str | None = None
    # The busy processes, plain text, filled whatever the column selection:
    # the card keeps its DOING line by default, as the TUI details panel does.
    doing: str | None = None


def card_content(
    c: ContainerInfo, fields: list[FieldSpec[ContainerInfo]], now: datetime | None = None
) -> CardContent:
    """Split a container's visible fields into NAME + STATE plus the rest,
    each addressable by ``FieldSpec.name`` so a renderer can place it."""
    cells = container_cells(c, fields)
    if now is not None:
        for index, field in enumerate(fields):
            if field.name == "network":
                cells[index] = _strip_markup(dashboard_format.card_network(c, now))
    name = ""
    state = ""
    card_fields: list[CardField] = []
    for field, cell in zip(fields, cells, strict=True):
        if field.name == "name":
            name = cell
        elif field.name == "state":
            # Qt's status color lookup uses the canonical lifecycle state.
            state = c.state
        else:
            card_fields.append(CardField(field.name, field.header, cell))
    reasons = [f"{s.agent}: {s.waiting_for}" for s in c.agent_status if s.waiting_for]
    tooltip = "\n".join(reasons)
    now = datetime.now(UTC)
    activity = next((text for s in c.agent_status if (text := describe(s, now)) is not None), None)
    if activity is not None:
        tooltip = "\n\n".join(part for part in (tooltip, "\n".join(activity.lines())) if part)
    return CardContent(
        name=name,
        state=state,
        fields=card_fields,
        job_error=c.job_error,
        agent_tooltip=tooltip or None,
        agent_waiting=any(s.state == "waiting" for s in c.agent_status),
        memory_limit=c.memory_limit,
        doing=_strip_markup(dashboard_format.doing_cell(c)) or None,
    )


# Git field values that mean "nothing to report".
_GIT_FIELD_NAMES = ("wt", "target_diff", "ahead_count", "behind_count", "conflict")


def card_field(cc: CardContent, name: str) -> str | None:
    """Value for field ``name``, or None if absent or a placeholder."""
    for f in cc.fields:
        if f.name == name:
            return f.value if f.value not in _CARD_PLACEHOLDERS else None
    return None


def job_badge(cc: CardContent) -> tuple[str, str] | None:
    """``(text, kind)`` for the job pill, or None when there is no job.

    ``kind`` is ``"failed"`` or ``"running"``; the caller maps it to a colour.
    The text is the JOB cell as rendered everywhere else, so a dead worker's
    ``"<phase> (dead)"`` label must count as failed too — it names a
    working phase but nothing is progressing.
    """
    value = card_field(cc, "job")
    if value is None:
        return None
    dead = value.startswith("failed") or DEAD_SUFFIX in value
    return value, "failed" if dead else "running"


def git_segments(cc: CardContent) -> list[tuple[str, str]]:
    """Coloured git pieces; empty when the working tree is clean."""
    segs: list[tuple[str, str]] = []
    ahead_count = card_field(cc, "ahead_count")
    if ahead_count not in (None, "0"):
        segs.append((f"↑{ahead_count}", "ahead"))
    behind_count = card_field(cc, "behind_count")
    if behind_count not in (None, "0"):
        segs.append((f"↓{behind_count}", "ahead"))
    target_diff = card_field(cc, "target_diff")
    if target_diff not in (None, "clean", "✓"):
        segs.append((target_diff, "diff"))
    wt = card_field(cc, "wt")
    if wt not in (None, "clean", "✓"):
        segs.append((f"wt {wt}", "diff"))
    conflict = card_field(cc, "conflict")
    if conflict not in (None, "ok"):
        # An in-progress state is already a verb ("merging"); only the
        # prediction words need the "merge " prefix to read as a sentence.
        label = conflict if conflict in IN_PROGRESS_CELL_LABELS else f"merge {conflict}"
        segs.append((label, "conflict"))
    outbox = card_field(cc, "outbox")
    if outbox:
        # OUTBOX is exactly "✉N" (PR plus issue manifests). Same "ahead" style
        # as ↑N: like ahead commits, it is something the container has that
        # GitHub does not yet.
        segs.append((outbox, "ahead"))
    return segs


def is_git_clean(cc: CardContent) -> bool:
    return not git_segments(cc)


def compact_meta(cc: CardContent) -> list[str]:
    """Non-empty mode/base/network values, in that order."""
    return [v for name in ("mode", "base", "network") if (v := card_field(cc, name))]


def grid_rows(cc: CardContent) -> list[tuple[str, str]]:
    """(header, value) rows for the Grid style: every non-placeholder,
    non-git field, then a single folded GIT row."""
    rows = [
        (f.header, f.value)
        for f in cc.fields
        if f.name not in _GIT_FIELD_NAMES and f.value not in _CARD_PLACEHOLDERS
    ]
    segs = git_segments(cc)
    rows.append(("GIT", "clean" if not segs else "  ".join(t for t, _ in segs)))
    return rows


_FIELD_MEANINGS = {
    "name": "Container display name",
    "full_name": "Full Incus container name",
    "repo": "Repository",
    "mode": "Repository mode: cln = clone, mnt = host mount",
    "base": "Base branch; ↗ means last-fetched remote-tracking base (not a live remote)",
    "state": "Container state: ▶ Running, ■ Stopped, Ⅱ Frozen",
    "created": "Container age (s/m/h/d); tooltip shows exact creation timestamp",
    "network": (
        "Loose network: red ● = loose with its remaining auto-revert time, "
        "∞ = no auto-revert; empty = strict"
    ),
    "ttl": "Remaining loose-network auto-revert time",
    "loose_until": "Exact loose-network auto-revert deadline",
    "mem": "Memory usage / configured limit",
    "mem_used": "Memory in use",
    "mem_pct": "Memory in use as a share of the configured limit",
    "memory_limit": "Configured memory limit",
    "cpu": "CPU usage; suffix is configured core limit",
    "doing": "Active processes; ×N = process count",  # noqa: RUF001 - intentional multiplication sign
    "job": "Background job phase; failed and (dead) identify failures",
    "wt": "Working-tree diff; ✓ = clean",
    "target_diff": "Diff against host target branch; ✓ = clean",
    "local_diff": "Diff against checked-out host HEAD; ✓ = clean",
    "ahead_count": "Commits ahead of host target",
    "behind_count": "Commits behind host target",
    "conflict": "Merge prediction or actual in-progress Git operation",
    "git_status": "Combined Git status",
    "pr": "Pull request; ↓ = review container (the outbox count is in OUTBOX)",
    "issues": "Pending issue outbox actions",
    "outbox": "Staged PR and issue outbox manifests waiting to be published (✉N)",
    "group": "Credential group",
    "agent_compact": (
        "Agent status: ◆ waiting, ● busy, ◐ shell, ○ idle (bright: idle under 30 min); ? unknown"
    ),
    "agent": "Full agent state and duration",
}


def field_tooltip(field: FieldSpec[ContainerInfo]) -> str:
    """Expand compact labels without depending on Qt."""
    return _FIELD_MEANINGS.get(field.name, field.name.replace("_", " ").capitalize())


def cell_tooltip(c: ContainerInfo, field: FieldSpec[ContainerInfo]) -> str:
    """Full facts behind a compact table cell, including agent wait reasons."""
    meaning = field_tooltip(field)
    if field.name == "state":
        detail = c.state
    elif field.name == "created":
        detail = c.created_at.isoformat() if c.created_at else "Unknown creation time"
    elif field.name in ("network", "ttl", "loose_until"):
        deadline = c.loose_until.isoformat() if c.loose_until else "no auto-revert deadline"
        detail = f"{c.network or 'unknown'}; {deadline}"
    elif field.name in ("agent", "agent_compact"):
        details = [
            f"{s.agent}: {s.state}; {s.count} session(s)"
            + (f"; since {s.since.isoformat()}" if s.since else "")
            + (f"; {s.waiting_for}" if s.waiting_for else "")
            for s in c.agent_status
        ]
        now = datetime.now(UTC)
        for summary in c.agent_status:
            activity = describe(summary, now)
            if activity is not None:
                details.extend(activity.lines())
        detail = "\n".join(details) or "No agent status"
    elif field.name == "job":
        label = job_label_or_empty(c.job_phase, c.job_pid, kind=c.job_kind)
        detail = "\n".join(part for part in (label, c.job_error) if part)
    else:
        detail = _strip_markup(field.cell(c))
    return f"{meaning}\n{detail}"
