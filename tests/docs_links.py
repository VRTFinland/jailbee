"""Resolve a documentation link without running the generator.

The landing page, README.md and llms.txt all send readers to
https://jailbee.gisgro.io/docs/…, which is built from docs/*.md. Nothing in a
unit test run builds that site, so this module answers the two questions a
broken link would fail on: does the page exist, and does the heading the
fragment names exist.

Heading ids follow GitHub's rule, which is what `toc.slugify` reproduces on the
site — lowercase, punctuation dropped, spaces to hyphens, and *doubled* hyphens
kept where punctuation stood between two words (`GPU / NVIDIA passthrough` is
`#gpu--nvidia-passthrough`). The one known divergence is a heading containing
`<` or `>`; `docs/architecture.md`'s "Host <-> container git bridge" slugs
differently in the two renderers, and nothing links to it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

SITE_URL = "https://jailbee.gisgro.io/"
SITE_DOCS_URL = f"{SITE_URL}docs/"
GITHUB_DOCS_PREFIX = "https://github.com/VRTFinland/jailbee/blob/main/docs/"

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
    `docs/config/#scratch`.
    """
    path = url[len(SITE_DOCS_URL) :] if url.startswith(SITE_DOCS_URL) else url[len("docs/") :]
    path, _, fragment = path.partition("#")
    if path and not path.endswith("/"):
        return f"{url}: docs URLs end in a slash (use directory URLs)"
    slug = path.rstrip("/")
    page = DOCS / ("README.md" if not slug else f"{slug}.md")
    if not page.is_file():
        return f"{url}: no such page — expected {page.relative_to(REPO_ROOT)}"
    if fragment and fragment not in anchors(page):
        return f"{url}: {page.name} has no heading with id {fragment!r}"
    return None
