"""SYNC's terminal-cell padding survives the real browser's text layout."""

from dataclasses import replace

from playwright.sync_api import Page, expect

from jailbee.git_status import GitStatus
from jailbee.host_target import SyncTarget
from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.server import E2EServer


def test_phone_sync_wide_diff_does_not_scroll_horizontally(
    server: E2EServer, logged_in: Page
) -> None:
    logged_in.set_viewport_size({"width": 375, "height": 812})
    c = replace(
        container("work-host", "work"),
        git_status=GitStatus(
            "clean",
            "clean",
            "1",
            "ok",
            behind_count="2",
            target_diff="+" + "1234567890" * 12 + " -" + "9876543210" * 12,
        ),
    )
    server.push(snapshot_of(repo_group("work", c)))
    cell = logged_in.locator(".cell", has=logged_in.get_by_text("SYNC", exact=True))
    expect(cell).to_contain_text("↑1 ↓2")
    assert logged_in.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert cell.evaluate("el => el.scrollWidth <= el.clientWidth")


def test_sync_preserves_marker_slots_and_right_aligned_counts(
    server: E2EServer, logged_in: Page
) -> None:
    host = replace(
        container("work-host", "work"),
        git_status=GitStatus("clean", "clean", "1", "ok", behind_count="123", target_diff="+1 -0"),
    )
    pr = replace(
        container("work-pr", "work"),
        git_status=GitStatus(
            "clean",
            "clean",
            "123",
            "ok",
            behind_count="2",
            target_diff="+42 -7",
            sync_target=SyncTarget("pr", "PR #7", "p", "pr"),
        ),
    )
    server.push(snapshot_of(repo_group("work", host, pr)))
    cells = logged_in.locator(".cell", has=logged_in.get_by_text("SYNC", exact=True)).locator("pre")
    expect(cells).to_have_count(2)
    assert cells.all_text_contents() == ["    ↑  1 ↓123 +1 -0", "pr  ↑123 ↓  2 +42 -7"]
    positions = cells.evaluate_all(
        """elements => elements.map(element => {
            const text = element.firstChild;
            const left = element.getBoundingClientRect().left;
            const offset = index => {
                const range = document.createRange();
                range.setStart(text, index);
                range.setEnd(text, index + 1);
                return range.getBoundingClientRect().left - left;
            };
            return {
                whitespace: getComputedStyle(element).whiteSpace,
                ahead: offset(text.textContent.indexOf('↑')),
                behind: offset(text.textContent.indexOf('↓')),
                aheadUnits: offset(7),
                behindUnits: offset(12),
            };
        })"""
    )
    assert positions[0] == positions[1]
    assert positions[0]["whitespace"] == "pre"
