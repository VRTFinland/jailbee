#!/usr/bin/env python3
"""Keep the version the website advertises in step with pyproject.toml.

Two hand-written literals name the release: the JSON-LD `softwareVersion` in
`website/index.html` (website/ is served as committed, so the version a
crawler reads is that literal), and `version` under `[project.extra]` in
`zensical.toml`, which the docs header's source box shows. `make release`
calls `set` here right after `uv version`, before it commits anything, so a
release cannot leave either one advertising the previous version.

`tests/test_website.py::test_the_structured_data_version_tracks_pyproject`
and `tests/test_docs_site.py::test_the_docs_header_version_tracks_pyproject`
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
TARGETS: tuple[tuple[Path, re.Pattern[str]], ...] = (
    (REPO_ROOT / "website" / "index.html", re.compile(r'("softwareVersion":\s*")([^"]*)(")')),
    (REPO_ROOT / "zensical.toml", re.compile(r'^(version = ")([^"]*)(")', re.MULTILINE)),
)


def _read(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"site_version: {path} does not exist")
    return path.read_text(encoding="utf-8")


def _name(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def cmd_get() -> None:
    for path, field in TARGETS:
        match = field.search(_read(path))
        if match is None:
            sys.exit(f"site_version: no version field in {_name(path)}")
        print(f"{_name(path)}: {match.group(2)}")


def cmd_set(version: str) -> None:
    # Check every file before writing any, so a missing field cannot leave
    # the two literals disagreeing.
    rewritten: list[tuple[Path, str, bool]] = []
    for path, field in TARGETS:
        text = _read(path)
        new, count = field.subn(rf"\g<1>{version}\g<3>", text, count=2)
        # Exactly one: zero means the field was renamed or removed, and more
        # than one means there is a second copy this would leave
        # inconsistent. Either way, guessing is worse than stopping.
        if count != 1:
            sys.exit(f"site_version: expected 1 version field in {_name(path)}, found {count}")
        rewritten.append((path, new, new != text))
    for path, new, changed in rewritten:
        if changed:
            path.write_text(new, encoding="utf-8")
            print(f"site_version: set {_name(path)} to {version}")
        else:
            print(f"site_version: {_name(path)} already {version}")


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
