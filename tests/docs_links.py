"""Resolve a documentation link without running the generator.

The landing page, README.md and llms.txt all send readers to
https://jailbee.gisgro.io/docs/…, which is built from docs/*.md. Nothing in a
unit test run builds that site, so this module answers the questions a broken
link would fail on: does the page exist, is it actually published, and does
the heading the fragment names exist.

Heading ids follow GitHub's rule, which is what `toc.slugify` reproduces on the
site — lowercase, punctuation dropped, spaces to hyphens, and *doubled* hyphens
kept where punctuation stood between two words (`GPU / NVIDIA passthrough` is
`#gpu--nvidia-passthrough`). `anchors()` numbers a duplicate heading GitHub's
way (`heading`, `heading-1`, `heading-2`, ...).

Three known divergences between GitHub and Zensical, none of which anything in
docs/ currently links to:

- a heading containing `<` or `>` slugs differently — `docs/architecture.md`'s
  "Host <-> container git bridge" is `host---container-git-bridge` on GitHub
  and `host--container-git-bridge` on the site.
- an ATX heading with a closing `##` sequence (`### Title ###`) — both
  renderers strip it, but they can disagree on the slug that results when the
  closing sequence abuts punctuation.
- a duplicate heading is numbered `heading-1`, `heading-2`, ... by GitHub but
  `heading_1`, `heading_2`, ... by Zensical. `anchors()` reproduces GitHub's
  numbering (see above), so a link that resolves here to a *duplicate*
  heading still has to be checked on the built site — this module cannot see
  Zensical's own numbering.

`resolve()` also enforces publication: a page has to be both present under
`docs/` and named in `nav` (`zensical.toml`, via `scripts/docs_site.py
published_pages()`) — `docs/manual-testing.md` exists on disk but 404s on the
site because `nav` never names it.
"""

from __future__ import annotations

import functools
import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

SITE_URL = "https://jailbee.gisgro.io/"
SITE_DOCS_URL = f"{SITE_URL}docs/"
GITHUB_DOCS_PREFIX = "https://github.com/VRTFinland/jailbee/blob/main/docs/"

# manual-testing.md (maintainer procedure) and releasing.md (maintainer
# procedure) live under docs/ but are deliberately never published — kept
# here, not in zensical.toml, so that a new docs/ page has to be added to one
# list or the other and cannot ship, or fail to ship, by accident.
UNPUBLISHED = frozenset({"manual-testing.md", "releasing.md"})


@functools.lru_cache(maxsize=1)
def _published_pages() -> frozenset[str]:
    """The exact page names `nav` in zensical.toml ships.

    Loaded from scripts/docs_site.py the way tests/test_docs_site.py loads
    it, so the flattening logic — nav nests titles inside lists inside dicts
    — lives in exactly one place.
    """
    path = REPO_ROOT / "scripts" / "docs_site.py"
    spec = importlib.util.spec_from_file_location("docs_site_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return frozenset(module.published_pages())


_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")


def github_slug(heading: str) -> str:
    """GitHub's heading id for `heading` (backticks and markup stripped)."""
    text = heading.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(page: Path) -> set[str]:
    """Every heading id in a Markdown file, duplicates numbered as GitHub does."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    in_code = False
    for line in page.read_text().splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = _HEADING.match(line)
        if not match:
            continue
        slug = github_slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(f"{slug}-{count}" if count else slug)
    return found


def is_docs_link(url: str) -> bool:
    """True for a link into the published documentation, either form."""
    return url.startswith(SITE_DOCS_URL) or url == "docs/" or url.startswith("docs/")


def resolve(url: str) -> str | None:
    """None when the link lands, otherwise why it does not.

    Accepts both forms a page may use: the absolute
    `https://jailbee.gisgro.io/docs/config/#scratch` and the site-relative
    `docs/config/#scratch`. A page must both exist under docs/ and be named
    in nav — a page that exists but is excluded (manual-testing, releasing,
    a skills/ path, or the raw `README` slug) 404s on the real site exactly
    like one that was never written.
    """
    path = url[len(SITE_DOCS_URL) :] if url.startswith(SITE_DOCS_URL) else url[len("docs/") :]
    path, _, fragment = path.partition("#")
    if path and not path.endswith("/"):
        return f"{url}: docs URLs end in a slash (use directory URLs)"
    slug = path.rstrip("/")
    if slug == "README" or "/" in slug:
        return f"{url}: {slug!r} is not a valid docs slug"
    name = "README.md" if not slug else f"{slug}.md"
    page = DOCS / name
    if not page.is_file():
        return f"{url}: no such page — expected {page.relative_to(REPO_ROOT)}"
    if name not in _published_pages():
        return f"{url}: {name} exists but is excluded from nav in zensical.toml"
    if fragment and fragment not in anchors(page):
        return f"{url}: {page.name} has no heading with id {fragment!r}"
    return None
