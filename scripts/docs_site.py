#!/usr/bin/env python3
"""Build tooling for the documentation site.

`docs/` is the single source of the documentation: the same Markdown renders on
github.com and, through this script and Zensical, on
https://jailbee.gisgro.io/docs/.

The one thing Zensical (0.0.60) cannot do is leave a file out of the build —
`exclude_docs` is accepted and ignored, and there is no equivalent. This repo
keeps maintainer procedure (`manual-testing.md`, `releasing.md`) and the
in-container agent's skills under `docs/`, none of which belong on the site, and
moving them out is not an option: `docs/skills` is force-included into the wheel
and `docs/manual-testing.md` is referenced from `src/` and the repo config.

So publication is a whitelist. `nav` in `zensical.toml` names what ships, `stage`
copies exactly those files into `_build/docs/`, and that staged tree is the
generator's `docs_dir`.

    scripts/docs_site.py stage
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "zensical.toml"
DOCS = REPO_ROOT / "docs"
STAGE = REPO_ROOT / "_build" / "docs"
SITE_DOCS = REPO_ROOT / "_site" / "docs"


def config(path: Path = CONFIG) -> dict[str, Any]:
    """The `[project]` table of the generator's configuration."""
    parsed: dict[str, Any] = tomllib.loads(path.read_text())["project"]
    return parsed


def published_pages(path: Path = CONFIG) -> list[str]:
    """Every page `nav` names, in nav order, relative to `docs/`.

    `nav` nests a title-to-target mapping inside lists, so this flattens both:
    `{ "Setup" = ["installation.md", ...] }` yields the targets, never the
    section titles.
    """
    pages: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            pages.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        else:
            raise TypeError(f"unexpected nav entry: {node!r}")

    walk(config(path)["nav"])
    return pages


def stage(
    docs: Path = DOCS,
    stage_dir: Path = STAGE,
    pages: list[str] | None = None,
) -> None:
    """Rebuild `stage_dir` from scratch with exactly `pages`.

    From scratch on purpose: a page dropped from `nav` has to disappear from
    the next build, and Zensical would happily keep publishing a leftover.
    """
    if pages is None:
        pages = published_pages()
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)
    for name in pages:
        source = docs / name
        if not source.is_file():
            raise SystemExit(f"nav names a page that does not exist: {source}")
        target = stage_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


# Any absolute or protocol-relative URL in a fetched attribute. `<a href>` is
# the one place a reader's click is what leaves the site; everything else here
# is a request the browser makes on its own.
_ABSOLUTE = re.compile(r"^(?:https?:)?//", re.IGNORECASE)
_FETCHED_ATTRS = ("src", "href", "poster", "srcset", "data")
# url(...) in a stylesheet or an inline <style>, and @import of one.
_CSS_URL = re.compile(r"""(?:url\(|@import\s+)\s*['"]?((?:https?:)?//[^)'"\s]+)""", re.IGNORECASE)


class _ReferenceCollector(HTMLParser):
    """Every fetched reference, plus whether the tag carried a component id."""

    def __init__(self) -> None:
        super().__init__()
        self.references: list[tuple[str, str, str, str | None]] = []
        self.components: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        component = attributes.get("data-md-component")
        if component:
            self.components.append(component)
        for name in _FETCHED_ATTRS:
            value = attributes.get(name)
            if value:
                self.references.append((tag, name, value, attributes.get("rel")))


def check(site_docs: Path = SITE_DOCS) -> list[str]:
    """Problems that would make a reader's browser talk to a third party.

    The site's rule — no CDN, no webfont service, no third-party image — is
    enforced on the landing page by `tests/test_website.py`. The docs are
    generated, so the same rule has to be checked on the build output: the
    theme fetches its fonts from Google and mermaid from unpkg by default, and
    a repository link carrying `data-md-component="source"` makes the bundle
    call api.github.com from every page.

    JavaScript bundles are deliberately not scanned: they carry URL strings
    that are never fetched (the unpkg fallback among them), and a text match
    there says nothing about what the page does.
    """
    problems: list[str] = []
    pages = sorted(site_docs.rglob("*.html"))
    if not pages:
        return [f"no pages under {site_docs} — was the site built?"]

    site_url = config()["site_url"]
    for page in pages:
        where = page.relative_to(site_docs)
        html = page.read_text()
        collector = _ReferenceCollector()
        collector.feed(html)
        for tag, attribute, value, rel in collector.references:
            if not _ABSOLUTE.match(value):
                continue
            if tag == "a" and attribute == "href":
                continue
            if tag == "link" and rel == "canonical" and value.startswith(site_url):
                continue
            problems.append(f"{where}: <{tag} {attribute}={value!r}> leaves the site")
        for component in collector.components:
            if component == "source":
                problems.append(
                    f'{where}: data-md-component="source" makes the page call api.github.com'
                )
        problems += [f"{where}: stylesheet fetches {url}" for url in _CSS_URL.findall(html)]

    for stylesheet in sorted(site_docs.rglob("*.css")):
        where = stylesheet.relative_to(site_docs)
        problems += [
            f"{where}: stylesheet fetches {url}" for url in _CSS_URL.findall(stylesheet.read_text())
        ]

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("stage", help="copy the published pages into _build/docs")
    subcommands.add_parser("check", help="scan the built site for off-site requests")
    args = parser.parse_args(argv)
    if args.command == "stage":
        stage()
    if args.command == "check":
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print(f"{len(problems)} off-site reference(s) in the built docs", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
