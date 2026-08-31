"""Checks for README.md's own references.

README.md is the repo's front page *and* the PyPI long description
(`pyproject.toml`'s `readme`), and the sdist ships the file without
`website/` or `docs/images/`. So every image it shows has to be an absolute
`raw.githubusercontent.com` URL — a relative path renders as a broken image
on PyPI — and nothing in the repo checks those URLs resolve until someone
looks at the rendered page. These do.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
RAW_PREFIX = "https://raw.githubusercontent.com/VRTFinland/jailbee/main/"


def test_every_raw_asset_the_readme_shows_exists_in_the_repo() -> None:
    """A typo here is a broken image on the repo page and on PyPI both.

    Paths are checked against the working tree, not against the `main`
    branch the URL names — so an asset added on a feature branch passes here
    and 404s until the branch lands. That is the same trade the existing
    logo reference already makes, and the alternative (a network fetch in
    the unit suite) is worse.
    """
    paths = re.findall(rf"{re.escape(RAW_PREFIX)}([^\s\"')]+)", README.read_text())
    assert paths, "the README shows no raw assets — did the URL prefix change?"
    for path in paths:
        assert (REPO_ROOT / path).is_file(), f"README references a missing asset: {path}"


def test_the_readme_links_the_demo_clip_at_an_anchor_the_site_still_has() -> None:
    """The README's clip is a poster linking to where it plays.

    A `<video>` would be the nicer thing, but PyPI's sanitiser strips the
    tag outright, so the poster-plus-link is the form that renders in both
    places. That makes the link load-bearing: renaming `#demos` on the site
    turns the repo's front-page call to action into a scroll to nowhere.
    """
    readme = README.read_text()
    anchor = "https://jailbee.gisgro.io/#demos"
    assert anchor in readme, "the README no longer links the demo clip"
    index = (REPO_ROOT / "website" / "index.html").read_text()
    assert 'id="demos"' in index, f"{anchor} points at a section the page does not have"
