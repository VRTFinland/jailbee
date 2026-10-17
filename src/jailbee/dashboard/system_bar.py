"""htop-style one-line host status: memory, storage pools and root filesystem.

Pure Rich, frontend-agnostic. `system_line` always returns a single line no
wider than the given width, degrading as the terminal narrows: bars shrink
(all bars share one width, 4-30 cells), then the root segment drops to
``/ NN%``, then every bar is dropped, and finally the line is cropped with an
ellipsis.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from jailbee.dashboard.system import SystemStatus, level
from jailbee.lifecycle import _format_bytes
from jailbee.sysstat import DiskStatus, MemoryStatus

MIN_BAR = 4
MAX_BAR = 30
_GAP = "  "
_LEVEL_STYLE = {"ok": "green", "warn": "yellow", "crit": "bold red"}


@dataclass(frozen=True)
class _Segment:
    label: str
    used: float  # fraction of the bar drawn in the level colour
    cache: float  # fraction drawn dim `|`
    numbers: str
    short: str  # numbers when the bar is dropped and the segment is compact
    lvl: str


def _disk_segment(label: str, disk: DiskStatus) -> _Segment:
    total = disk.total or 1
    used = disk.used / total
    return _Segment(
        label=label,
        used=used,
        cache=0.0,
        numbers=f"{_format_bytes(disk.used)}/{_format_bytes(disk.total)}",
        short=f"{round(used * 100)}%",
        lvl=level(disk.free_ratio),
    )


def _memory_segment(mem: MemoryStatus) -> _Segment:
    total = mem.total or 1
    return _Segment(
        label="Mem",
        used=mem.used / total,
        cache=mem.cache / total,
        numbers=f"{_format_bytes(mem.used)}/{_format_bytes(mem.total)}",
        short=f"{round(mem.used / total * 100)}%",
        lvl=level(mem.free_ratio),
    )


def _segments(status: SystemStatus) -> tuple[list[_Segment], _Segment | None]:
    """Mem and pool segments, and the root segment kept apart (it degrades first)."""
    segs: list[_Segment] = []
    if status.memory is not None:
        segs.append(_memory_segment(status.memory))
    many = len(status.pools) > 1
    for pool in status.pools:
        segs.append(_disk_segment(pool.name if many else "Pool", pool.disk))
    root = _disk_segment("/", status.root) if status.root is not None else None
    return segs, root


def _bar(seg: _Segment, width: int) -> Text:
    used = min(width, max(0, round(seg.used * width)))
    cache = min(width - used, max(0, round(seg.cache * width)))
    bar = Text("[", style="dim")
    bar.append("|" * used, style=_LEVEL_STYLE[seg.lvl])
    bar.append("|" * cache, style="dim")
    bar.append("░" * (width - used - cache), style="dim")
    bar.append("]", style="dim")
    return bar


def _render(
    segs: list[_Segment], root: _Segment | None, bar: int | None, root_short: bool
) -> Text:
    line = Text(no_wrap=True, overflow="ellipsis")
    items = [(s, False) for s in segs]
    if root is not None:
        items.append((root, root_short))
    for i, (seg, compact) in enumerate(items):
        if i:
            line.append(_GAP)
        line.append(seg.label + " ")
        numbers_style = _LEVEL_STYLE[seg.lvl] if seg.lvl != "ok" else ""
        if compact or bar is None:
            if seg.label == "/":
                line.append(seg.short, style=numbers_style)
            else:
                line.append(seg.numbers, style=numbers_style)
            continue
        line.append_text(_bar(seg, bar))
        line.append(" ")
        line.append(seg.short if seg.label == "/" else seg.numbers, style=numbers_style)
    return line


def _fit_bars(
    segs: list[_Segment], root: _Segment | None, width: int, root_short: bool
) -> Text | None:
    """The line with the widest equal bars that fit, or None if even 4 cells do not."""
    bars = len(segs) + (1 if root is not None and not root_short else 0)
    if bars == 0:
        return None
    base = _render(segs, root, MIN_BAR, root_short)
    spare = width - base.cell_len
    if spare < 0:
        return None
    bar = min(MAX_BAR, MIN_BAR + spare // bars)
    return _render(segs, root, bar, root_short)


def system_line(status: SystemStatus, width: int) -> Text:
    """One-line status no wider than `width` cells."""
    segs, root = _segments(status)
    if width <= 0:
        return Text("")
    for root_short in (False, True):
        fitted = _fit_bars(segs, root, width, root_short)
        if fitted is not None:
            return fitted
    line = _render(segs, root, None, True)
    if line.cell_len > width:
        line.truncate(width, overflow="ellipsis")
    return line
