#!/usr/bin/env python3
"""Keep the website's JSON-LD `softwareVersion` in step with pyproject.toml.

website/ is served as committed, with no build step, so the version a crawler
reads is a literal in `website/index.html`. `make release` calls `set` here
right after `uv version`, before it commits anything, so a release cannot
leave the page advertising the previous version.

`tests/test_website.py::test_the_structured_data_version_tracks_pyproject`
guards the outcome. This script is the thing that makes the guard pass
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
INDEX = REPO_ROOT / "website" / "index.html"

# The JSON-LD block is small and hand-maintained; a targeted substitution
# keeps its formatting and comments untouched, which re-serialising would not.
_FIELD = re.compile(r'("softwareVersion":\s*")([^"]*)(")')


def _read(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"site_version: {path} does not exist")
    return path.read_text(encoding="utf-8")


def cmd_get(path: Path) -> None:
    match = _FIELD.search(_read(path))
    if match is None:
        sys.exit(f"site_version: no softwareVersion field in {path}")
    print(match.group(2))


def cmd_set(path: Path, version: str) -> None:
    text = _read(path)
    new, count = _FIELD.subn(rf"\g<1>{version}\g<3>", text, count=2)
    # Exactly one: zero means the field was renamed or removed, and more than
    # one means there is a second copy this would leave inconsistent. Either
    # way, guessing is worse than stopping.
    if count != 1:
        sys.exit(f"site_version: expected 1 softwareVersion field in {path}, found {count}")
    if new == text:
        print(f"site_version: already {version}")
        return
    path.write_text(new, encoding="utf-8")
    print(f"site_version: set softwareVersion to {version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=INDEX, help=f"page to edit (default: {INDEX})")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("get", help="print the version the page advertises")
    p_set = sub.add_parser("set", help="rewrite the version the page advertises")
    p_set.add_argument("version")

    args = parser.parse_args()
    if args.cmd == "get":
        cmd_get(args.file)
    elif args.cmd == "set":
        cmd_set(args.file, args.version)


if __name__ == "__main__":
    main()
