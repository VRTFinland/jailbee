"""Horizontal geometry of the terminal dashboard's columns."""

import pytest

from jailbee.dashboard.viewport import MARK_COST, Viewport, column_viewport


@pytest.mark.parametrize(
    "widths, available, offset, expected",
    [
        ((20, 2), 24, 0, Viewport((0, 1), (20, 2), 0, False, False)),
        ((20, 5, 2), 27, 1, Viewport((0, 2), (20, 2), 1, True, False)),
    ],
)
def test_whole_short_final_column_fits_without_partial_threshold(
    widths, available, offset, expected
):
    assert column_viewport(widths, available, offset) == expected


@pytest.mark.parametrize("available", [28, 29, 30])
def test_returned_offsets_traverse_mark_only_holes_to_the_final_column(available):
    widths = (20, 5, 2, 5, 3)
    view = column_viewport(widths, available, 0)
    offsets = [view.offset]
    for _ in range(3):
        view = column_viewport(widths, available, view.offset + 1)
        offsets.append(view.offset)
        line = view.widths[0] + sum(2 + w for w in view.widths[1:])
        line += MARK_COST * (view.hidden_left + view.hidden_right)
        assert line <= available
    assert offsets == [0, 1, 2, 3]
    assert view == Viewport((0, 4), (20, 3), 3, True, False)
    assert column_viewport(widths, available, view.offset + 1) == view
    assert column_viewport(widths, available, view.offset - 1).offset == 2


def test_traversable_hole_preserves_offset_and_fitting_marks():
    assert column_viewport((20, 5, 2, 5, 3), 28, 1) == Viewport((0,), (20,), 1, True, True)


def test_everything_fits_draws_every_column_without_marks():
    assert column_viewport((10, 5, 5), 40, 0) == Viewport((0, 1, 2), (10, 5, 5), 0, False, False)


def test_offset_is_clamped_to_zero_when_everything_fits():
    assert column_viewport((10, 5, 5), 40, 3).offset == 0
    assert column_viewport((10, 5, 5), 40, -2).offset == 0


def test_a_column_too_narrow_to_truncate_is_left_out_and_marked():
    # 10 | +7 = 17 | +7 = 24 (+3 for > still fits) | last needs +7 = 31 > 30;
    # the remainder 30 - 24 - 2 - 3 = 1 is below MIN_PARTIAL.
    assert column_viewport((10, 5, 5, 5), 30, 0) == Viewport((0, 1, 2), (10, 5, 5), 0, False, True)


def test_the_first_column_that_does_not_fit_is_truncated_to_the_remainder():
    # 10 | col 1 needs 10 + 2 + 20 + 3 = 35 > 30 -> gets 30 - 10 - 2 - 3 = 15.
    view = column_viewport((10, 20, 5), 30, 0)
    assert view == Viewport((0, 1), (10, 15), 0, False, True)
    # The line is exactly the width: frozen + column + the > mark.
    assert 10 + (2 + 15) + MARK_COST == 30


def test_scrolling_skips_columns_and_marks_the_left_edge():
    assert column_viewport((10, 5, 5, 5), 30, 1) == Viewport((0, 2, 3), (10, 5, 5), 1, True, False)


def test_offset_past_the_end_is_clamped_to_the_last_column_drawn_whole():
    view = column_viewport((10, 5, 5, 5), 30, 9)
    assert view.offset == 1
    assert view.indices[-1] == 3 and view.widths[-1] == 5


def test_middle_offset_shows_both_marks():
    view = column_viewport((10, 6, 6, 6, 6, 6), 30, 1)
    assert view.hidden_left and view.hidden_right
    assert view.indices[0] == 0 and view.indices[1] == 2
    line = view.widths[0] + sum(2 + w for w in view.widths[1:]) + 2 * MARK_COST
    assert line <= 30


def test_a_column_wider_than_the_space_between_the_marks_is_always_truncated():
    # 14 frozen + < (3) + 2 padding + > (3) leaves 18 for a middle column of 20:
    # no offset shows it whole. That is inherent to the narrow terminal, and the
    # column is still reachable (drawn truncated), never dropped.
    widths = (14, 8, 3, 12, 20, 6, 9)
    drawn = {
        i for offset in range(len(widths)) for i in column_viewport(widths, 40, offset).indices
    }
    assert drawn == set(range(len(widths)))


def test_frozen_column_wider_than_the_line():
    assert column_viewport((50, 5, 5), 30, 0) == Viewport((0,), (30,), 0, False, False)
    assert column_viewport((50, 5, 5), 30, 1) == Viewport((0,), (30,), 0, False, False)


def test_single_column_never_scrolls():
    assert column_viewport((12,), 30, 4) == Viewport((0,), (12,), 0, False, False)


def test_no_columns():
    assert column_viewport((), 30, 2) == Viewport((), (), 0, False, False)


def test_every_offset_keeps_the_line_within_the_width():
    widths = (14, 8, 3, 12, 20, 6, 9)
    for available in range(5, 90):
        for offset in range(len(widths) + 2):
            v = column_viewport(widths, available, offset)
            line = (
                (v.widths[0] if v.widths else 0)
                + sum(2 + w for w in v.widths[1:])
                + MARK_COST * (v.hidden_left + v.hidden_right)
            )
            assert line <= max(available, 0), (available, offset, v)


def test_stepping_the_offset_reaches_every_column_whole():
    # Every scrolled width here is <= 40 - 14 - 2 - 2 * MARK_COST = 18, the
    # most a scrolled column can show whole between both marks.
    widths = (14, 8, 3, 12, 18, 6, 9)
    seen: set[int] = set()
    offset = 0
    for _ in range(len(widths)):
        v = column_viewport(widths, 40, offset)
        seen |= {i for i, w in zip(v.indices, v.widths, strict=True) if w == widths[i]}
        offset = v.offset + 1
    assert seen == set(range(len(widths)))


def test_frozen_only_layout_resets_offset_when_scrolled_column_cannot_be_drawn():
    view = column_viewport((10, 5, 5), 15, 1)
    assert view == Viewport((0,), (10,), 0, False, False)


def test_nonpositive_available_width_never_produces_positive_line_width():
    for available in (0, -1):
        view = column_viewport((10, 5, 5), available, 1)
        line = (view.widths[0] if view.widths else 0) + sum(2 + w for w in view.widths[1:])
        line += MARK_COST * (view.hidden_left + view.hidden_right)
        assert line <= max(available, 0), (available, view)
        assert view == Viewport((0,), (0,), 0, False, False)
