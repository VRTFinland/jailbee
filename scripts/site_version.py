#!/usr/bin/env python3
"""Keep the version the website advertises in step with pyproject.toml.

Three hand-written literals name the release. Two are in `website/index.html`
(served as committed, so what a reader and a crawler see is the literal):
the JSON-LD `softwareVersion`, and the header's source box. The third is
`version` under `[project.extra]` in `zensical.toml`, which the docs
header's source box shows. `make release` calls `set` here right after
`uv version`, before it commits anything, so a release cannot leave any of
them advertising the previous version.

`tests/test_website.py::test_the_structured_data_version_tracks_pyproject`,
`tests/test_website.py::test_the_header_matches_the_docs_header` and
`tests/test_docs_site.py::test_the_docs_header_version_tracks_pyproject`
guard the outcome. This script is the thing that makes the guards pass
without a human remembering; it exits non-zero rather than guessing, so a
failure aborts the release while everything is still local.

    scripts/site_version.py get
    scripts/site_version.py set 1.2.0
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each literal is small and hand-maintained; a targeted substitution keeps
# the surrounding formatting and comments untouched, which re-serialising
# would not. zensical.toml has no other top-of-line `version =` key.
INDEX = REPO_ROOT / "website" / "index.html"
TARGETS: tuple[tuple[str, Path, re.Pattern[str]], ...] = (
    ("JSON-LD", INDEX, re.compile(r'("softwareVersion":\s*")([^"]*)(")')),
    ("header", INDEX, re.compile(r'(class="topbar__version">v)([^<]*)(<)')),
    ("docs header", REPO_ROOT / "zensical.toml", re.compile(r'^(version = ")([^"]*)(")', re.M)),
)


def _read(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"site_version: {path} does not exist")
    return path.read_text(encoding="utf-8")


def _name(label: str, path: Path) -> str:
    return f"{path.relative_to(REPO_ROOT)} ({label})"


def cmd_get() -> None:
    for label, path, field in TARGETS:
        match = field.search(_read(path))
        if match is None:
            sys.exit(f"site_version: no version field in {_name(label, path)}")
        print(f"{_name(label, path)}: {match.group(2)}")


def cmd_set(version: str) -> None:
    # Rewrite in memory, per file, and write only once every field has
    # matched: two targets share index.html, and a missing field must not
    # leave the literals disagreeing.
    texts: dict[Path, str] = {}
    for label, path, field in TARGETS:
        text = texts[path] if path in texts else _read(path)
        new, count = field.subn(rf"\g<1>{version}\g<3>", text, count=2)
        # Exactly one: zero means the field was renamed or removed, and more
        # than one means there is a second copy this would leave
        # inconsistent. Either way, guessing is worse than stopping.
        if count != 1:
            sys.exit(
                f"site_version: expected 1 version field in {_name(label, path)}, found {count}"
            )
        texts[path] = new
    for path, new in texts.items():
        name = path.relative_to(REPO_ROOT)
        if new != _read(path):
            path.write_text(new, encoding="utf-8")
            print(f"site_version: set {name} to {version}")
        else:
            print(f"site_version: {name} already {version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("get", help="print the version each file advertises")
    p_set = sub.add_parser("set", help="rewrite the version each file advertises")
    p_set.add_argument("version")

    args = parser.parse_args()
    if args.cmd == "get":
        cmd_get()
    elif args.cmd == "set":
        cmd_set(args.version)


if __name__ == "__main__":
    main()
