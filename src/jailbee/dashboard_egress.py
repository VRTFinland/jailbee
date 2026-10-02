"""Pure state and rendering for the dashboard's scoped egress panel."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from rich import box
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from jailbee.egress import is_wildcard_entry
from jailbee.egress_scope import EntryRow

if TYPE_CHECKING:
    from rich.console import RenderableType

_VISIBLE_ROWS = 10


@dataclass(frozen=True)
class EgressState:
    """Rows and selection for one repo or container scope."""

    prefix: str
    container: str | None
    rows: tuple[EntryRow, ...]
    index: int = 0
    can_add: bool = True
    can_rm: bool = True


def _clamped(state: EgressState, index: int) -> int:
    return max(0, min(index, len(state.rows) - 1)) if state.rows else 0


def move_egress(state: EgressState, delta: int) -> EgressState:
    """Move the selected row, clamping at either end."""
    return replace(state, index=_clamped(state, state.index + delta))


def replace_egress_rows(state: EgressState, rows: tuple[EntryRow, ...]) -> EgressState:
    """Replace loaded rows, retaining the selected entry/source where possible."""
    selected = state.rows[state.index] if state.rows and state.index < len(state.rows) else None
    index = next(
        (
            i
            for i, row in enumerate(rows)
            if selected is not None and (row.entry, row.source) == (selected.entry, selected.source)
        ),
        _clamped(replace(state, rows=rows), state.index),
    )
    return replace(state, rows=rows, index=index)


def removable_entry(state: EgressState) -> str | None:
    """Return selected override entry if it can be removed at this scope."""
    if not state.rows or state.index >= len(state.rows):
        return None
    row = state.rows[state.index]
    if state.container is None:
        return row.entry if row.source in ("local", "db (legacy)") else None
    return row.entry if row.source == "container" else None


def egress_argv(state: EgressState, action: Literal["add", "rm"], entry: str) -> list[str]:
    """Build explicit CLI arguments for the current scope (without executable)."""
    target = "--repo" if state.container is None else state.container
    return ["net", "egress", action, entry, target]


def _window(index: int, total: int) -> tuple[int, int]:
    if total <= _VISIBLE_ROWS:
        return 0, total
    start = min(max(0, index - _VISIBLE_ROWS + 1), total - _VISIBLE_ROWS)
    return start, start + _VISIBLE_ROWS


def render_egress(state: EgressState, *, can_add: bool, can_rm: bool) -> RenderableType:
    """Render a compact, bounded panel; entry strings are always plain text."""
    lines: list[Text] = []
    if not state.rows:
        lines.append(Text("No egress entries in this scope."))
    else:
        repo_sources: dict[str, set[str]] = {}
        if state.container is None:
            for row in state.rows:
                repo_sources.setdefault(row.entry, set()).add(row.source)
        start, end = _window(state.index, len(state.rows))
        if start:
            lines.append(Text(f"↑ {start} more", style="dim"))
        for i in range(start, end):
            row = state.rows[i]
            line = Text(
                "▸ " if i == state.index else "  ", style="bold magenta" if i == state.index else ""
            )
            line.append(row.entry, style="bold" if i == state.index else "")
            sources = repo_sources.get(row.entry, set())
            duplicate_repo_entry = {"local", "db (legacy)"} <= sources
            source_note = (
                f"[{row.source}; removes both repo copies]"
                if duplicate_repo_entry
                else f"[{row.source}]"
            )
            line.append(f"  {source_note}")
            if is_wildcard_entry(row.entry):
                line.append("  [proxy]")
            if row.redundant:
                line.append("  (redundant)", style="dim")
            lines.append(line)
        if end < len(state.rows):
            lines.append(Text(f"↓ {len(state.rows) - end} more", style="dim"))
    hints = ["↑/↓ navigate", "Esc back"]
    if can_add:
        hints.append("a add")
    if can_rm and removable_entry(state) is not None:
        hints.append("r remove")
    lines.append(Text("  ·  ".join(hints), style="dim"))
    scope = "repo" if state.container is None else f"container {escape(state.container)}"
    return Panel(Group(*lines), title=f"Egress · {scope}", box=box.ROUNDED)
