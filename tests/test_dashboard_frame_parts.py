"""Pure title and notice parts retain the legacy frame's border texts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui.frame import INLINE_NOTICE_MAX, frame_title, notice_parts
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
    assert title.spans  # Preserve the bold title and dim help cue, not only their text.


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
