"""htop-style one-line host status: memory, swap, storage pools and root filesystem.

Pure Rich, frontend-agnostic. `system_line` always returns a single line no
wider than the given width, degrading as the terminal narrows: bars shrink
(they share one width, at most 30, but a bar never gets narrower than its label
and numbers plus two cells), then the root segment drops to ``/ NN%``, then
every bar is dropped, and finally the line is cropped with an ellipsis.

The label sits inside the bar at its left end and the numbers at its right
end, both coloured cell by cell like the bar beneath them: the level colour as
far as the used share reaches, dark grey beyond. The unused part of the bar is
plain blanks with no background. The swap bar is left out on a host without
swap.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from jailbee.dashboard.system import SystemStatus, level
from jailbee.lifecycle import _format_bytes
from jailbee.sysstat import DiskStatus, MemoryStatus

MAX_BAR = 30
_PAD = 2  # bar cells left for the fill beyond the numbers
_GAP = "  "
_LEVEL_STYLE = {"ok": "bold green", "warn": "bold yellow", "crit": "bold red"}
_EMPTY_STYLE = "bright_black"  # numbers over the unused part of the bar
_BRACKET_STYLE = "bright_white"  # the bar's ends, so its bounds stand out


@dataclass(frozen=True)
class _Segment:
    label: str
    used: float  # fraction of the bar drawn in the level colour
    numbers: str
    short: str  # numbers when the bar is dropped and the segment is compact
    lvl: str


def _disk_segment(label: str, disk: DiskStatus) -> _Segment:
    total = disk.total or 1
    used = disk.used / total
    return _Segment(
        label=label,
        used=used,
        numbers=f"{_format_bytes(disk.used)}/{_format_bytes(disk.total)}",
        short=f"{round(used * 100)}%",
        lvl=level(disk.free_ratio),
    )


def _memory_segment(mem: MemoryStatus) -> _Segment:
    total = mem.total or 1
    return _Segment(
        label="Mem",
        used=mem.used / total,
        numbers=f"{_format_bytes(mem.used)}/{_format_bytes(mem.total)}",
        short=f"{round(mem.used / total * 100)}%",
        lvl=level(mem.free_ratio),
    )


def _swap_segment(mem: MemoryStatus) -> _Segment:
    total = mem.swap_total or 1
    return _Segment(
        label="Swap",
        used=mem.swap_used / total,
        numbers=f"{_format_bytes(mem.swap_used)}/{_format_bytes(mem.swap_total)}",
        short=f"{round(mem.swap_used / total * 100)}%",
        lvl=level(mem.swap_free_ratio),
    )


def memory_numbers(used: int, total: int) -> str:
    return f"{_format_bytes(used)}/{_format_bytes(total)}"


def min_usage_bar_cells(label: str, used: int, total: int) -> int:
    """Cells `usage_bar` needs at least: brackets, label, the numbers and the pad."""
    return len(label) + 2 + len(memory_numbers(used, total)) + _PAD


def usage_bar(label: str, used: int, total: int, width: int) -> Text:
    """`[label|||    used/total]` for one usage figure, `width` cells inside the brackets.

    The level colour follows the free share, as in the status line. `width`
    must be at least the label, the numbers and the pad (see
    `min_usage_bar_cells`).
    """
    total = max(total, 1)
    seg = _Segment(
        label=label,
        used=used / total,
        numbers=memory_numbers(used, total),
        short="",
        lvl=level(max(0.0, (total - used) / total)),
    )
    return _bar(seg, width, seg.numbers)


def _segments(status: SystemStatus) -> tuple[list[_Segment], _Segment | None]:
    """Mem, swap and pool segments, and the root segment kept apart (it degrades first)."""
    segs: list[_Segment] = []
    if status.memory is not None:
        segs.append(_memory_segment(status.memory))
        if status.memory.swap_total:
            segs.append(_swap_segment(status.memory))
    many = len(status.pools) > 1
    for pool in status.pools:
        segs.append(_disk_segment(pool.name if many else "Pool", pool.disk))
    root = _disk_segment("/", status.root) if status.root is not None else None
    return segs, root


def _numbers_style(seg: _Segment) -> str:
    return _LEVEL_STYLE[seg.lvl] if seg.lvl != "ok" else ""


def _bar(seg: _Segment, width: int, text: str) -> Text:
    """`[label|||||    text]`: fill drawn as `|`, label and `text` overlaid at the ends."""
    used = min(width, max(0, round(seg.used * width)))
    fill = _LEVEL_STYLE[seg.lvl]
    text_at = width - len(text)
    bar = Text()
    bar.append("[", style=_BRACKET_STYLE)
    for i in range(width):
        if i < used:
            style = fill
        else:
            style = _EMPTY_STYLE
        if i >= text_at:
            bar.append(text[i - text_at], style=style)
        elif i < len(seg.label):
            bar.append(seg.label[i], style=style)
        elif i < used:
            bar.append("|", style=style)
        else:
            bar.append(" ")
    bar.append("]", style=_BRACKET_STYLE)
    return bar


def _inner(seg: _Segment) -> str:
    """The text shown inside a bar: the root shows its percentage only."""
    return seg.short if seg.label == "/" else seg.numbers


def _min_bar(seg: _Segment) -> int:
    """The narrowest bar that shows `seg`'s label and numbers whole, plus the pad."""
    return len(seg.label) + len(_inner(seg)) + _PAD


def _render(segs: list[_Segment], root: _Segment | None, bar: int | None, root_short: bool) -> Text:
    """The line with every bar `bar` cells wide, or wider where its own content needs it."""
    line = Text(no_wrap=True, overflow="ellipsis")
    items = [(s, False) for s in segs]
    if root is not None:
        items.append((root, root_short))
    for i, (seg, compact) in enumerate(items):
        if i:
            line.append(_GAP)
        if compact or bar is None:
            line.append(seg.label + " ")
            line.append(_inner(seg), style=_numbers_style(seg))
            continue
        line.append_text(_bar(seg, max(bar, _min_bar(seg)), _inner(seg)))
    return line


def _fit_bars(
    segs: list[_Segment], root: _Segment | None, width: int, root_short: bool
) -> Text | None:
    """The line with the widest shared bar width that fits, or None if none does."""
    shown = list(segs)
    if root is not None and not root_short:
        shown.append(root)
    if not shown:
        return None
    for bar in range(MAX_BAR, 0, -1):
        line = _render(segs, root, bar, root_short)
        if line.cell_len <= width:
            return line
    return None


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
