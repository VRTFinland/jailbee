"""Every screenshot fixture renders end to end: all shots written, no exception."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jailbee.lifecycle import ContainerInfo
from website.shots.fixture import FixtureError, World, load_world
from website.shots.render import render_world, svg_size

FIXTURES = sorted((Path(__file__).parent.parent / "website" / "shots" / "fixtures").glob("*.yaml"))


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_fixture_renders_every_shot(fixture: Path, tmp_path: Path) -> None:
    world = load_world(fixture)
    expected = [name for kind, name in world.steps if kind == "shot"]
    written = render_world(world, tmp_path)
    assert [p.stem for p in written] == expected
    for path in written:
        svg = path.read_text(encoding="utf-8")
        assert svg_size(svg)[0] > 0
        # The docs' no-CDN rule: nothing but the SVG namespace (and Rich's credit comment).
        urls = set(re.findall(r"https?://[^\s\"')]+", svg))
        assert urls <= {"http://www.w3.org/2000/svg", "https://www.textualize.io"}


def _svg_text(svg: str) -> str:
    """The text a viewer reads: tspans/text content with entities decoded and nbsp folded."""
    import html

    text = "".join(re.findall(r">([^<>]*)<", svg))
    return html.unescape(text).replace("\xa0", " ")


def test_docs_shots_show_their_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank or cropped render must fail: header, rows and overlay titles are in the SVG."""
    # conftest pins COLUMNS=1000, which would hide the headless console's 80-column crop.
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)
    world = load_world(next(p for p in FIXTURES if p.stem == "docs"))
    texts = {p.stem: _svg_text(p.read_text("utf-8")) for p in render_world(world, tmp_path)}
    global_menu = texts["dashboard-global-menu"]
    for label in (
        "Global",
        "Stop all…",
        "[A] Accounts",
        "[Q] Notifications",
        "[F2] Settings",
    ):
        assert label in global_menu
    # The docs fixture has no saved resume set.
    assert "Resume…" not in global_menu
    hero = texts["dashboard-orchestration"]
    # The header runs past DOING: a crop at the default console width loses these.
    header_line = next(line for line in hero.splitlines() if "NAME" in line)
    for header in ("MERGE", "PR", "OUTBOX"):
        assert header in header_line
    for row in ("promo-codes", "stripe-3ds", "monthly-summary"):
        assert row in hero
    assert "Outbox ·" in texts["dashboard-outbox"]
    assert "credential groups" in texts["dashboard-accounts"]
    assert "◆" in hero  # a waiting agent
    assert "⟳" not in hero  # no queue badge before the fixture's queue is seeded
    queue = texts["dashboard-queue"]
    assert "Notifications" in queue
    # A running badge may be captured at any refresh frame: one running, one failed.
    assert any(f"{glyph} 1" in queue for glyph in "⟳◷◶◵") and "✗ 1" in queue
    for line in ("✓ storefront-theme-dark", "✗ invoicer-csv-import", "⟳ invoicer"):
        assert line in queue
    entry = texts["dashboard-queue-entry"]
    assert "failed (exit 1)" in entry and "Merge conflict" in entry and "Dismiss" in entry


def test_there_is_at_least_the_docs_fixture() -> None:
    assert "docs" in {p.stem for p in FIXTURES}


def test_docs_fixture_tree_and_shots() -> None:
    world = load_world(next(p for p in FIXTURES if p.stem == "docs"))
    assert {g.prefix for g in world.groups} == {"invoicer", "storefront"}
    assert 25 <= sum(len(g.containers) for g in world.groups) <= 30
    assert [n for k, n in world.steps if k == "shot"] == [
        "dashboard-global-menu",
        "dashboard-orchestration",
        "dashboard-menu",
        "dashboard-card",
        "dashboard-outbox",
        "dashboard-accounts",
        "dashboard-queue",
        "dashboard-queue-entry",
    ]


def _docs() -> str:
    return next(p for p in FIXTURES if p.stem == "docs").read_text("utf-8")


def _container(world: World, name: str) -> ContainerInfo:
    return next(c for g in world.groups for c in g.containers if c.name == name)


def test_docs_outbox_column_has_counts_matching_the_outbox_shot() -> None:
    world = load_world(_docs())
    row = _container(world, "storefront-stripe-3ds")
    proposals = world.outbox["storefront-stripe-3ds"]
    assert row.outbox_pr == sum(1 for p in proposals if p["kind"] == "pr")
    assert row.outbox_issue == sum(1 for p in proposals if p["kind"] == "issue")
    # The OUTBOX cell reads the git status's counts, as a gathered row's stamp does.
    assert row.git_status is not None
    assert row.git_status.pending_pr_actions == row.outbox_pr
    assert row.git_status.pending_issue_actions == row.outbox_issue
    counted = [c for g in world.groups for c in g.containers if c.outbox_pr or c.outbox_issue]
    assert len(counted) >= 2
    for c in counted:
        assert c.git_status is not None
        assert (c.git_status.pending_pr_actions or 0) == (c.outbox_pr or 0)
        assert (c.git_status.pending_issue_actions or 0) == (c.outbox_issue or 0)


def test_docs_rows_read_like_real_rows() -> None:
    world = load_world(_docs())
    rows = [c for g in world.groups for c in g.containers]
    for c in rows:
        assert c.git_status is not None and c.git_status.conflict in {"ok", "conflict"}, c.name
        if c.pr_number is not None:
            assert c.pr_author, c.name
    assert any(s.state == "waiting" for c in rows for s in c.agent_status)
    for proposals in world.outbox.values():
        for p in proposals:
            assert " <- " not in p.get("target", "")
            assert (p["kind"] == "pr") == bool(p.get("target")), p


def test_unknown_key_in_an_outbox_proposal_names_the_key() -> None:
    text = _docs().replace("kind: pr, repo:", "kind: pr, colour: red, repo:", 1)
    with pytest.raises(FixtureError, match="colour"):
        load_world(text)


def test_unknown_key_in_an_account_row_names_the_key() -> None:
    text = _docs().replace(
        "{agent: claude, group: spare,", "{agent: claude, nickname: x, group: spare,"
    )
    with pytest.raises(FixtureError, match="nickname"):
        load_world(text)
