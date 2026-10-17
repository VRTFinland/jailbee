from __future__ import annotations

import pytest
from rich.console import Console
from rich.text import Text

from jailbee.dashboard.system import PoolStatus, SystemStatus
from jailbee.dashboard.system_bar import system_line
from jailbee.sysstat import DiskStatus, MemoryStatus

GIB = 1024**3


def _mem(avail_ratio: float = 0.5) -> MemoryStatus:
    total = 16 * GIB
    available = int(total * avail_ratio)
    return MemoryStatus(total=total, available=available, cache=available // 4)


def _disk(free_ratio: float = 0.5) -> DiskStatus:
    total = 100 * GIB
    return DiskStatus(total=total, available=int(total * free_ratio))


def _status(
    *,
    memory: MemoryStatus | None = None,
    pools: tuple[PoolStatus, ...] | None = None,
    root: DiskStatus | None = None,
) -> SystemStatus:
    return SystemStatus(
        memory=memory if memory is not None else _mem(),
        pools=pools if pools is not None else (PoolStatus("default", _disk()),),
        root=root if root is not None else _disk(),
    )


def _styles_of(line: Text, needle: str) -> list[str]:
    start = line.plain.index(needle)
    out = []
    for span in line.spans:
        if span.start <= start < span.end:
            out.append(str(span.style))
    return out


def test_line_has_all_segments_at_wide_width() -> None:
    line = system_line(_status(), 120)
    assert "Mem[" in line.plain
    assert "Pool[" in line.plain
    assert "/[" in line.plain
    assert "\n" not in line.plain


def test_line_colours_warn_and_crit() -> None:
    warn = system_line(_status(pools=(PoolStatus("default", _disk(0.10)),)), 120)
    assert any("yellow" in str(sp.style) for sp in warn.spans)
    crit = system_line(_status(memory=_mem(0.03)), 120)
    assert any("red" in str(sp.style) for sp in crit.spans)


def test_ok_is_green() -> None:
    line = system_line(_status(), 120)
    assert any("green" in str(sp.style) for sp in line.spans)
    assert not any("yellow" in str(sp.style) or "red" in str(sp.style) for sp in line.spans)


def test_warn_colours_numbers_too() -> None:
    line = system_line(_status(pools=(PoolStatus("default", _disk(0.10)),)), 120)
    assert any("yellow" in s for s in _styles_of(line, "90.0G/100.0G"))


def test_multiple_pools_use_names() -> None:
    pools = (PoolStatus("fast", _disk()), PoolStatus("slow", _disk()))
    line = system_line(_status(pools=pools), 160)
    assert "fast[" in line.plain
    assert "slow[" in line.plain
    assert "Pool" not in line.plain


@pytest.mark.parametrize("width", [100, 70, 50, 40, 20])
def test_degrades_without_wrapping(width: int) -> None:
    status = _status(pools=(PoolStatus("a", _disk()), PoolStatus("b", _disk())))
    line = system_line(status, width)
    assert len(line.wrap(Console(), width)) == 1
    assert line.cell_len <= width
    if width == 40:
        assert "[" not in line.plain


@pytest.mark.parametrize("width", range(0, 11))
def test_tiny_widths_do_not_crash(width: int) -> None:
    line = system_line(_status(), width)
    assert line.cell_len <= width
    assert "\n" not in line.plain


def test_bars_have_equal_width_within_limits() -> None:
    line = system_line(_status(), 300)
    bars = [seg.split("]")[0] for seg in line.plain.split("[")[1:]]
    widths = {len(b) for b in bars}
    assert len(widths) == 1
    assert 4 <= widths.pop() <= 30


def test_numbers_sit_inside_the_bar_at_its_right_end() -> None:
    line = system_line(_status(), 120)
    assert "8.0G/16.0G]" in line.plain
    assert "50.0G/100.0G]" in line.plain
    assert "50%]" in line.plain  # root shows its percentage inside the bar


def test_root_shortens_before_bars_drop() -> None:
    status = _status()
    found = False
    for width in range(30, 120):
        plain = system_line(status, width).plain
        if "/ " in plain and "/[" not in plain and "Mem[" in plain:
            assert "%" in plain
            found = True
    assert found


def test_missing_memory_omits_segment() -> None:
    status = SystemStatus(memory=None, pools=(PoolStatus("d", _disk()),), root=_disk())
    for width in (120, 40):
        line = system_line(status, width)
        assert "Mem" not in line.plain
        assert line.cell_len <= width


def test_nothing_to_show_is_empty() -> None:
    line = system_line(SystemStatus(memory=None, pools=(), root=None), 80)
    assert line.plain == ""


def test_mem_bar_has_fill_then_blanks_and_no_shade_glyph() -> None:
    line = system_line(_status(), 120)
    plain = line.plain
    assert "░" not in plain
    bar = plain[plain.index("[") + 1 : plain.index("]")]
    assert bar.startswith("|")
    assert " " in bar  # the unused part is blank


def test_unused_part_of_the_bar_has_no_style() -> None:
    line = system_line(_status(), 120)
    plain = line.plain
    blank = plain.index(" ", plain.index("["))
    assert plain[blank - 1] == "|"  # first blank follows the fill
    covering = [sp for sp in line.spans if sp.start <= blank < sp.end]
    assert covering == []


def test_numbers_fit_in_the_narrowest_bar() -> None:
    status = _status(pools=(PoolStatus("default", _disk(0.5)),))
    for width in range(30, 120):
        plain = system_line(status, width).plain
        if "Mem[" in plain and "Pool[" in plain:
            assert "8.0G/16.0G]" in plain
            assert "50.0G/100.0G]" in plain
