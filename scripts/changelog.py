#!/usr/bin/env python3
"""CHANGELOG.md helpers used by the release targets in the Makefile.

Pure stdlib so it runs without the project virtualenv. Subcommands:

    finalize <version> [--date YYYY-MM-DD]
        Rename the ``## Unreleased`` heading to ``## <version> - <date>``
        and insert a fresh, empty ``## Unreleased`` above it. Errors if the
        Unreleased section has no content (nothing to release).

    extract <version>
        Print the body of the ``## <version> ...`` section (used as the
        GitHub Release notes via ``gh release create --notes-file``).

    unreleased-empty
        Exit 0 if the Unreleased section is empty, 1 if it has content.
        Lets the Makefile decide whether to auto-draft entries.

    draft [--from <ref>]
        Draft entries for the Unreleased section from ``git log`` since the
        last tag (or <ref>) using the ``claude`` CLI, replacing the current
        Unreleased body. The human reviews/edits the result afterwards.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
UNRELEASED = "## Unreleased"
SEP = "-"  # heading separator, e.g. "## 1.2.3 - 2026-01-01"


def _read() -> list[str]:
    return CHANGELOG.read_text(encoding="utf-8").splitlines()


def _write(lines: list[str]) -> None:
    CHANGELOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _section_bounds(lines: list[str], heading_pred: Callable[[str], bool]) -> tuple[int, int]:
    """Return (start, end) line indices for the section whose heading matches
    ``heading_pred``. ``start`` is the heading line; ``end`` is the next ``## ``
    heading (exclusive) or len(lines). Raises SystemExit if not found."""
    start = next((i for i, ln in enumerate(lines) if heading_pred(ln)), None)
    if start is None:
        sys.exit(f"changelog: no matching section heading in {CHANGELOG.name}")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return start, end


def _body_is_empty(lines: list[str], start: int, end: int) -> bool:
    return all(not ln.strip() for ln in lines[start + 1 : end])


def cmd_finalize(version: str, date: str | None) -> None:
    stamp = date or datetime.date.today().isoformat()
    lines = _read()
    start, end = _section_bounds(lines, lambda ln: ln.strip() == UNRELEASED)
    if _body_is_empty(lines, start, end):
        sys.exit("changelog: Unreleased section is empty — nothing to release")
    lines[start] = f"{UNRELEASED}\n\n## {version} {SEP} {stamp}"
    _write(lines)
    print(f"changelog: finalized {version} ({stamp})")


def cmd_extract(version: str) -> None:
    def is_version_heading(ln: str) -> bool:
        if not ln.startswith("## "):
            return False
        return ln[3:].split()[0:1] == [version]

    lines = _read()
    start, end = _section_bounds(lines, is_version_heading)
    body = "\n".join(lines[start + 1 : end]).strip()
    print(body)


def cmd_unreleased_empty() -> None:
    lines = _read()
    start, end = _section_bounds(lines, lambda ln: ln.strip() == UNRELEASED)
    sys.exit(0 if _body_is_empty(lines, start, end) else 1)


def _last_tag() -> str | None:
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def cmd_draft(from_ref: str | None) -> None:
    base = from_ref or _last_tag()
    rng = f"{base}..HEAD" if base else "HEAD"
    log = subprocess.run(
        ["git", "log", rng, "--no-merges", "--pretty=format:- %s%n%b"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not log:
        sys.exit(f"changelog: no commits in range {rng!r} to draft from")

    prompt = (
        "You are drafting a CHANGELOG entry. Read the existing CHANGELOG.md in "
        "the working directory to match its exact heading style and tone, then "
        "summarise the commits below into entries for the Unreleased section. "
        "Group them under '### Added:', '### Changed:', '### Fixed:', "
        "'### Removed:' headings as appropriate, each with a short title after "
        "the colon and concise prose. Output ONLY the markdown entries, no "
        "preamble, no code fences.\n\nCommits since "
        f"{base or 'the start of history'}:\n\n{log}"
    )
    try:
        drafted = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except FileNotFoundError:
        sys.exit("changelog: 'claude' CLI not found — edit CHANGELOG.md by hand")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"changelog: claude draft failed: {exc.stderr.strip()}")

    drafted = re.sub(r"^```[a-z]*\n|\n```$", "", drafted).strip()
    lines = _read()
    start, end = _section_bounds(lines, lambda ln: ln.strip() == UNRELEASED)
    new = [*lines[: start + 1], "", drafted, "", *lines[end:]]
    _write(new)
    print("changelog: drafted Unreleased entries (review before releasing)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fin = sub.add_parser("finalize", help="stamp Unreleased with a version")
    p_fin.add_argument("version")
    p_fin.add_argument("--date", default=None, help="override date (YYYY-MM-DD)")

    p_ext = sub.add_parser("extract", help="print a version's section body")
    p_ext.add_argument("version")

    sub.add_parser("unreleased-empty", help="exit 0 if Unreleased is empty")

    p_draft = sub.add_parser("draft", help="draft Unreleased entries via claude")
    p_draft.add_argument("--from", dest="from_ref", default=None)

    args = parser.parse_args()
    if args.cmd == "finalize":
        cmd_finalize(args.version, args.date)
    elif args.cmd == "extract":
        cmd_extract(args.version)
    elif args.cmd == "unreleased-empty":
        cmd_unreleased_empty()
    elif args.cmd == "draft":
        cmd_draft(args.from_ref)


if __name__ == "__main__":
    main()
