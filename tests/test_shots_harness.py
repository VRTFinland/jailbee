"""Every screenshot fixture renders end to end: all shots written, no exception."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from website.shots.fixture import load_world
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


def test_there_is_at_least_the_docs_fixture() -> None:
    assert "docs" in {p.stem for p in FIXTURES}


def test_docs_fixture_tree_and_shots() -> None:
    world = load_world(next(p for p in FIXTURES if p.stem == "docs"))
    assert {g.prefix for g in world.groups} == {"invoicer", "storefront"}
    assert 25 <= sum(len(g.containers) for g in world.groups) <= 30
    assert [n for k, n in world.steps if k == "shot"] == [
        "dashboard-orchestration",
        "dashboard-menu",
        "dashboard-card",
        "dashboard-outbox",
        "dashboard-accounts",
    ]
