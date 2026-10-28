from __future__ import annotations

import pytest
from rich.console import Console
from rich.text import Text

from jailbee.dashboard.system import PoolStatus, SystemStatus
from jailbee.dashboard.system_bar import format_load, system_line
from jailbee.sysstat import DiskStatus, LoadStatus, MemoryStatus

GIB = 1024**3


def _mem(avail_ratio: float = 0.5, swap_total: int = 0, swap_free: int = 0) -> MemoryStatus:
    total = 16 * GIB
    available = int(total * avail_ratio)
    return MemoryStatus(
        total=total,
        available=available,
        cache=available // 4,
        swap_total=swap_total,
        swap_free=swap_free,
    )


def _disk(free_ratio: float = 0.5) -> DiskStatus:
    total = 100 * GIB
    return DiskStatus(total=total, available=int(total * free_ratio))


def _status(
    *,
    memory: MemoryStatus | None = None,
    pools: tuple[PoolStatus, ...] | None = None,
    root: DiskStatus | None = None,
    load: LoadStatus | None = None,
) -> SystemStatus:
    return SystemStatus(
        memory=memory if memory is not None else _mem(),
        pools=pools if pools is not None else (PoolStatus("default", _disk()),),
        root=root if root is not None else _disk(),
        load=load,
    )


def _styles_of(line: Text, needle: str) -> list[str]:
    start = line.plain.index(needle)
    out = []
    for span in line.spans:
        if span.start <= start < span.end:
            out.append(str(span.style))
    return out


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.0, " 0.00"),
        (2.345, " 2.35"),
        (9.996, "10.00"),
        (12.34, "12.34"),
        (99.996, "100.0"),
        (123.44, "123.4"),
        (999.96, " 1000"),
        (1234.4, " 1234"),
    ],
)
def test_format_load_is_five_wide(value: float, text: str) -> None:
    assert format_load(value) == text


def test_load_sits_after_swap_before_pools() -> None:
    status = _status(memory=_mem(swap_total=100, swap_free=50), load=LoadStatus(1.5, 8))
    plain = system_line(status, 160).plain
    assert plain.index("Swap") < plain.index(" 1.50") < plain.index("Pool")
    assert "Load" not in plain


def test_load_alone_has_no_padding() -> None:
    status = SystemStatus(memory=None, pools=(), root=None, load=LoadStatus(0.5, 4))
    assert system_line(status, 80).plain == " 0.50"


def test_no_load_segment_when_unread() -> None:
    assert system_line(_status(), 120).plain == system_line(_status(load=None), 120).plain


@pytest.mark.parametrize(("load1", "style"), [(2.0, ""), (2.8, "bold yellow"), (4.0, "bold red")])
def test_load_colour_follows_per_cpu_level(load1: float, style: str) -> None:
    line = system_line(_status(load=LoadStatus(load1, 4)), 160)
    assert _styles_of(line, format_load(load1)) == ([style] if style else [])


def test_line_has_all_segments_at_wide_width() -> None:
    line = system_line(_status(), 120)
    assert "[Mem" in line.plain
    assert "[Pool" in line.plain
    assert "[/" in line.plain
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
    assert "[fast" in line.plain
    assert "[slow" in line.plain
    assert "Pool" not in line.plain


@pytest.mark.parametrize("width", [100, 70, 50, 40, 20])
def test_degrades_without_wrapping(width: int) -> None:
    status = _status(
        pools=(PoolStatus("a", _disk()), PoolStatus("b", _disk())), load=LoadStatus(1.0, 4)
    )
    line = system_line(status, width)
    assert len(line.wrap(Console(), width)) == 1
    assert line.cell_len <= width
    if width == 40:
        assert "[" not in line.plain
        assert " 1.00" in line.plain


@pytest.mark.parametrize("width", range(0, 11))
def test_tiny_widths_do_not_crash(width: int) -> None:
    line = system_line(_status(load=LoadStatus(1.0, 4)), width)
    assert line.cell_len <= width
    assert "\n" not in line.plain


@pytest.mark.parametrize("width", [80, 120, 121, 122, 300])
def test_bars_stretch_to_fill_the_width_sharing_it_equally(width: int) -> None:
    line = system_line(_status(), width)
    assert line.cell_len == width
    bars = [seg.split("]")[0] for seg in line.plain.split("[")[1:]]
    assert len(bars) == 3
    widths = [len(b) for b in bars]
    assert max(widths) - min(widths) <= 1


def test_bars_stretch_when_the_root_is_compact() -> None:
    status = _status()
    for width in range(30, 120):
        line = system_line(status, width)
        if "[Mem" in line.plain and "[/" not in line.plain:
            assert line.cell_len == width
            return
    pytest.fail("no width showed bars with a compact root")


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
        if "/ " in plain and "[/" not in plain and "[Mem" in plain:
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
    assert bar.startswith("Mem|")
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
        if "[Mem" in plain and "[Pool" in plain:
            assert "8.0G/16.0G]" in plain
            assert "50.0G/100.0G]" in plain


def _style_at(line: Text, index: int) -> str:
    return " ".join(str(sp.style) for sp in line.spans if sp.start <= index < sp.end)


def test_numbers_are_coloured_only_as_far_as_the_fill_reaches() -> None:
    line = system_line(_status(root=_disk(0.5)), 300)
    start = line.plain.index("50.0G/100.0G", line.plain.index("[Pool"))
    bar_open = line.plain.index("[Pool")
    width = line.plain.index("]", start) - bar_open - 1
    used_cells = round(0.5 * width)
    first_unused = bar_open + 1 + used_cells
    assert "green" in _style_at(line, first_unused - 1)
    assert "green" not in _style_at(line, first_unused)
    assert "bright_black" in _style_at(line, start + len("50.0G/100.0G") - 1)


def test_swap_gets_its_own_bar_after_mem() -> None:
    line = system_line(_status(memory=_mem(swap_total=8 * GIB, swap_free=6 * GIB)), 200)
    assert "[Swap" in line.plain
    assert "2.0G/8.0G]" in line.plain
    assert line.plain.index("[Mem") < line.plain.index("[Swap") < line.plain.index("[Pool")


def test_no_swap_bar_without_swap() -> None:
    line = system_line(_status(), 200)
    assert "Swap" not in line.plain


def test_swap_colour_follows_its_free_share() -> None:
    line = system_line(_status(memory=_mem(swap_total=8 * GIB, swap_free=GIB // 4)), 200)
    assert "red" in _style_at(line, line.plain.index("[Swap") + 1)


def test_label_sits_inside_the_bar_coloured_like_the_cells_beneath() -> None:
    line = system_line(_status(memory=_mem(0.5)), 300)
    label = line.plain.index("[Mem") + 1
    assert "green" in _style_at(line, label)  # half full: the label is in the fill
    empty = system_line(_status(memory=_mem(1.0)), 300)
    label = empty.plain.index("[Mem") + 1
    assert "bright_black" in _style_at(empty, label)  # nothing used: dark grey


def test_compact_segments_keep_the_label_outside() -> None:
    status = _status(pools=(PoolStatus("a", _disk()), PoolStatus("b", _disk())))
    plain = system_line(status, 40).plain
    assert "[" not in plain
    assert "Mem " in plain


def test_a_long_label_widens_only_its_own_bar_when_room_is_short() -> None:
    status = _status(
        memory=_mem(swap_total=8 * GIB, swap_free=7 * GIB),
        pools=(PoolStatus("default", _disk()), PoolStatus("cow", _disk())),
    )
    plain = system_line(status, 100).plain
    bars = [seg.split("]")[0] for seg in plain.split("[")[1:]]
    assert len(bars) == 5  # every bar still shown
    assert len({len(b) for b in bars}) > 1  # "default" keeps its bar wider than the rest


def test_brackets_are_bright_white() -> None:
    line = system_line(_status(), 120)
    open_at = line.plain.index("[Mem")
    close_at = line.plain.index("]", open_at)
    assert _style_at(line, open_at) == "bright_white"
    assert _style_at(line, close_at) == "bright_white"
