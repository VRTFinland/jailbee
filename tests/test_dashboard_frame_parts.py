"""Pure title and notice parts retain the legacy frame's border texts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from rich.console import Console

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.hit import HIT_KEY, Hit
from jailbee.dashboard.tui.frame import INLINE_NOTICE_MAX, frame_title, notice_parts, update_line
from jailbee.update_check import UpdateNotice
from tests.dashboard_fixtures import ci

NOW = datetime(2026, 10, 8, 12, 0, 5, tzinfo=UTC)


def test_the_title_counts_repos_containers_and_folds(tmp_path):
    groups = [
        dmodel.RepoGroup("a", str(tmp_path), None, [ci("a-1", "a"), ci("a-2", "a")]),
        dmodel.RepoGroup("b", str(tmp_path), None, [ci("b-1", "b")]),
        dmodel.RepoGroup("empty", str(tmp_path), None, []),
    ]
    title = frame_title(groups, frozenset({"a", "empty", "absent"}), git_enabled=True, now=NOW)
    assert title.plain == (
        "🐝 jailbee dashboard  ·  h/? help  ·  3 repos · 3 containers · 1 folded  ·  12:00:05"
    )
    console = Console()
    brand_end = title.plain.index("  ·")
    help_start = title.plain.index("h/? help")
    assert all(title.get_style_at_offset(console, i).bold for i in range(brand_end))
    assert all(
        title.get_style_at_offset(console, i).dim
        for i in range(help_start, help_start + len("h/? help"))
    )
    assert not title.get_style_at_offset(console, title.plain.index("3 repos")).bold
    assert not title.get_style_at_offset(console, title.plain.index("3 repos")).dim


def test_the_title_marks_no_git():
    assert frame_title([], frozenset(), git_enabled=False, now=NOW).plain == (
        "🐝 jailbee dashboard  ·  h/? help  ·  0 repos · 0 containers  ·  (no-git)  ·  12:00:05"
    )


@pytest.mark.parametrize("notice", [None, ""])
def test_absent_notice_has_no_parts(notice):
    assert notice_parts(notice) == (None, None)


@pytest.mark.parametrize("notice", ["view-only", "x" * INLINE_NOTICE_MAX, "bad [/x] value"])
def test_short_notice_is_plain_yellow_ellipsized_subtitle(notice):
    subtitle, inline = notice_parts(notice)
    assert subtitle is not None and inline is None
    assert subtitle.plain == notice and subtitle.style == "yellow"
    assert subtitle.no_wrap is True and subtitle.overflow == "ellipsis"
    assert not subtitle.spans


def test_long_notice_is_plain_yellow_inline_text():
    notice = "bad [/x] " + "x" * INLINE_NOTICE_MAX
    subtitle, inline = notice_parts(notice)
    assert subtitle is None and inline is not None
    assert inline.plain == notice and inline.style == "yellow"
    assert not inline.spans


def test_long_status_notice_stays_on_the_border_ellipsized():
    notice = "alpha/main: local target behind fetched " + "x" * INLINE_NOTICE_MAX
    subtitle, inline = notice_parts(notice, wraps=False)
    assert inline is None and subtitle is not None
    assert subtitle.plain == notice and subtitle.overflow == "ellipsis"


NOTICE = UpdateNotice(current="1.7.2", latest="1.7.3", command="uv tool upgrade jailbee")


def test_the_update_line_names_both_versions_and_the_command():
    text = update_line(NOTICE).plain
    assert "1.7.3" in text and "1.7.2" in text and "uv tool upgrade jailbee" in text


def test_only_the_dismiss_label_is_a_click_target():
    line = update_line(NOTICE)
    tagged = [
        line.plain[span.start : span.end]
        for span in line.spans
        if not isinstance(span.style, str) and span.style.meta.get(HIT_KEY)
    ]
    assert tagged == ["[dismiss]"]
    span = next(s for s in line.spans if not isinstance(s.style, str) and s.style.meta.get(HIT_KEY))
    assert Hit.of(span.style.meta) == Hit("dismiss-update")


def test_a_hovered_dismiss_label_is_underlined_and_an_idle_one_is_not():
    def label_style(hover):
        line = update_line(NOTICE, hover=hover)
        span = next(
            s for s in line.spans if not isinstance(s.style, str) and s.style.meta.get(HIT_KEY)
        )
        return span.style

    assert label_style(Hit("dismiss-update")).underline is True
    assert not label_style(None).underline
