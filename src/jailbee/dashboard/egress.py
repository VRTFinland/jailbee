"""Pure state and labels for the dashboard's scoped egress panel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.text import Text

from jailbee.egress import is_wildcard_entry
from jailbee.egress_scope import EntryRow


@dataclass(frozen=True)
class EgressState:
    """One repo or container scope's rows; ``start_index`` is where the cursor opens."""

    prefix: str
    container: str | None
    rows: tuple[EntryRow, ...]
    start_index: int = 0
    can_add: bool = True
    can_rm: bool = True


def removable_entry(state: EgressState, row: EntryRow) -> str | None:
    """``row``'s entry when this scope can remove it."""
    if state.container is None:
        return row.entry if row.source in ("local", "db (legacy)") else None
    return row.entry if row.source == "container" else None


def egress_label(state: EgressState, row: EntryRow) -> Text:
    """One row: the entry, where it comes from, proxy and redundancy notes; plain text only."""
    sources = (
        {r.source for r in state.rows if r.entry == row.entry} if state.container is None else set()
    )
    note = (
        f"[{row.source}; removes both repo copies]"
        if {"local", "db (legacy)"} <= sources
        else f"[{row.source}]"
    )
    line = Text(row.entry, no_wrap=True, overflow="ellipsis")
    line.append(f"  {note}")
    if is_wildcard_entry(row.entry):
        line.append("  [proxy]")
    if row.redundant:
        line.append("  (redundant)", style="dim")
    return line


def egress_argv(state: EgressState, action: Literal["add", "rm"], entry: str) -> list[str]:
    """Build explicit CLI arguments for the current scope (without executable)."""
    target = "--repo" if state.container is None else state.container
    return ["net", "egress", action, entry, target]
