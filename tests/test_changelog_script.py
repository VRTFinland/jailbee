"""Tests for `scripts/changelog.py`'s release-note unwrapper.

`scripts/` helpers are normally guarded through their artifacts rather than
unit-tested (see `scripts/make_og_card.py`), but `_unwrap` has no artifact in
the tree: its output goes straight to `gh release create --notes-file`, so a
regression would first be visible on a published release page. The structural
cases below are the ones that would mangle real notes — a joined table, a
reflowed code block — and none of them appear in CHANGELOG.md today, which is
exactly why they need a test rather than a reader.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    path = REPO_ROOT / "scripts" / "changelog.py"
    spec = importlib.util.spec_from_file_location("changelog_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


unwrap = _load()._unwrap


def test_a_wrapped_paragraph_becomes_one_line():
    assert unwrap("first line\nsecond line\nthird line") == "first line second line third line"


def test_each_bullet_stays_its_own_line():
    body = "- first item\n  wrapped on\n- second item\n  also wrapped"
    assert unwrap(body) == "- first item wrapped on\n- second item also wrapped"


def test_blank_lines_survive_as_paragraph_separators():
    assert unwrap("one\ntwo\n\nthree\nfour") == "one two\n\nthree four"


def test_a_heading_is_never_joined_to_its_body():
    assert unwrap("### Added\n\n- a thing\n  wrapped") == "### Added\n\n- a thing wrapped"


def test_a_code_span_split_across_lines_ends_up_on_one_line():
    """The bug this exists for.

    GitHub renders release notes in `gfm` mode, where a lone newline becomes
    `<br>` — except inside a code span, where no break can be inserted, so
    the two source lines merged into one very long rendered line instead.
    Unwrapping removes the newline before the renderer ever sees it.
    """
    body = "documents — `jailbee exec smoke -- pnpm\ntest` — therefore died"
    out = unwrap(body)
    assert out == "documents — `jailbee exec smoke -- pnpm test` — therefore died"
    assert "\n" not in out


@pytest.mark.parametrize(
    "body",
    [
        "```bash\nfirst command\nsecond command\n```",
        "~~~\nfirst line\nsecond line\n~~~",
    ],
)
def test_a_fenced_block_passes_through_verbatim(body):
    """Joining the lines of a code block would corrupt the commands in it."""
    assert unwrap(body) == body


def test_table_rows_are_not_joined():
    body = "| a | b |\n|---|---|\n| 1 | 2 |"
    assert unwrap(body) == body


def test_nested_bullets_keep_their_own_lines_and_indentation():
    body = "- outer item\n  - inner item\n    wrapped inner\n- second outer"
    assert unwrap(body) == "- outer item\n  - inner item wrapped inner\n- second outer"


def test_a_blockquote_line_is_not_absorbed_into_the_paragraph_above():
    assert unwrap("a paragraph\n> quoted line") == "a paragraph\n> quoted line"


def test_unwrapping_is_idempotent():
    once = unwrap("- an item\n  wrapped over\n  three lines")
    assert unwrap(once) == once
