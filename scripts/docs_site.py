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
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "zensical.toml"
DOCS = REPO_ROOT / "docs"
STAGE = REPO_ROOT / "_build" / "docs"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("stage", help="copy the published pages into _build/docs")
    args = parser.parse_args(argv)
    if args.command == "stage":
        stage()
    return 0


if __name__ == "__main__":
    sys.exit(main())
