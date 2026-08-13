"""Pure transforms from jailbee's container data to plain display values.

Framework-free (no PySide6, though Rich — a shared, non-GUI dependency — is
used to strip markup from ``FieldSpec.cell`` output). Cell text comes from
``FieldSpec.cell`` (the same human-readable rendering the TUI table uses),
with any Rich markup tags stripped, rather than ``FieldSpec.json`` (whose
value may be structured data, e.g. ``mem``'s json is a ``{"usage", "limit"}``
dict — not something we want to stringify straight into a cell).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text

if TYPE_CHECKING:
    from jailbee.dashboard import RepoGroup
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


def card_content(c: ContainerInfo, fields: list[FieldSpec[ContainerInfo]]) -> CardContent:
    """Split a container's visible fields into NAME + STATE plus the rest,
    each addressable by ``FieldSpec.name`` so a renderer can place it."""
    cells = container_cells(c, fields)
    name = ""
    state = ""
    card_fields: list[CardField] = []
    for field, cell in zip(fields, cells, strict=True):
        if field.name == "name":
            name = cell
        elif field.name == "state":
            state = cell
        else:
            card_fields.append(CardField(field.name, field.header, cell))
    return CardContent(name=name, state=state, fields=card_fields, job_error=c.job_error)


# Git field values that mean "nothing to report".
_GIT_FIELD_NAMES = ("wt", "ahead_diff", "ahead_count", "conflict")


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
    ``"<phase> (worker gone)"`` label must count as failed too — it names a
    working phase but nothing is progressing.
    """
    value = card_field(cc, "job")
    if value is None:
        return None
    dead = value.startswith("failed") or "(worker gone)" in value
    return value, "failed" if dead else "running"


def git_segments(cc: CardContent) -> list[tuple[str, str]]:
    """Coloured git pieces; empty when the working tree is clean."""
    segs: list[tuple[str, str]] = []
    ahead_count = card_field(cc, "ahead_count")
    if ahead_count not in (None, "0"):
        segs.append((f"↑{ahead_count}", "ahead"))
    ahead_diff = card_field(cc, "ahead_diff")
    if ahead_diff not in (None, "clean"):
        segs.append((ahead_diff, "diff"))  # type: ignore[arg-type]  # not-None narrowed by the check
    wt = card_field(cc, "wt")
    if wt not in (None, "clean"):
        segs.append((f"wt {wt}", "diff"))
    conflict = card_field(cc, "conflict")
    if conflict not in (None, "ok"):
        segs.append((f"merge {conflict}", "conflict"))
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
