"""Structural checks on the ```mermaid blocks in the repo's Markdown.

No renderer runs in CI (mermaid is a browser library, and GitHub renders
these pages), so a diagram that fails to parse ships silently — it just
shows up as an error box on github.com. These tests catch the failure
classes that are cheap to detect from the source text alone.

The angle-bracket rule is the one worth explaining: mermaid label text is
rendered as HTML, so a placeholder like ``<prefix>-base`` is parsed as an
unknown tag and *disappears* from the diagram. Labels therefore spell
placeholders out (``prefix-base``) and use ``<br>`` as the only markup.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DIAGRAM_TYPES = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram-v2",
    "classDiagram",
    "erDiagram",
)

_QUOTED = re.compile(r'"([^"]*)"')
_BR_TAG = re.compile(r"<br\s*/?>")


def markdown_files() -> list[Path]:
    """Every tracked-by-convention Markdown page: repo root plus docs/."""
    return sorted([*REPO_ROOT.glob("*.md"), *REPO_ROOT.glob("docs/**/*.md")])


def mermaid_blocks() -> list[tuple[Path, int, list[str]]]:
    """Return (path, 1-based line of the opening fence, body lines) per block."""
    blocks: list[tuple[Path, int, list[str]]] = []
    for path in markdown_files():
        lines = path.read_text().splitlines()
        start: int | None = None
        for number, line in enumerate(lines, start=1):
            if start is None:
                if line.strip() == "```mermaid":
                    start = number
            elif line.strip() == "```":
                blocks.append((path, start, lines[start:number - 1]))
                start = None
        assert start is None, f"{path.name}:{start} mermaid fence is never closed"
    return blocks


BLOCKS = mermaid_blocks()


def block_ids() -> list[str]:
    return [f"{path.relative_to(REPO_ROOT)}:{line}" for path, line, _ in BLOCKS]


def test_the_repo_actually_ships_diagrams() -> None:
    """Guards the tests below from silently passing over an empty set."""
    assert BLOCKS, "no ```mermaid blocks found — did the glob stop matching?"


@pytest.mark.parametrize(("path", "line", "body"), BLOCKS, ids=block_ids())
def test_block_declares_a_supported_diagram_type(path: Path, line: int, body: list[str]) -> None:
    first = next((raw.strip() for raw in body if raw.strip()), "")
    assert first.startswith(DIAGRAM_TYPES), (
        f"{path.name}:{line} starts with {first!r}, which is not one of {DIAGRAM_TYPES}"
    )


@pytest.mark.parametrize(("path", "line", "body"), BLOCKS, ids=block_ids())
def test_block_balances_its_quotes_and_brackets(path: Path, line: int, body: list[str]) -> None:
    for offset, raw in enumerate(body, start=line + 1):
        assert raw.count('"') % 2 == 0, f"{path.name}:{offset} has an unclosed quote"
    text = "\n".join(body)
    for opener, closer in (("[", "]"), ("(", ")"), ("{", "}")):
        assert text.count(opener) == text.count(closer), (
            f"{path.name}:{line} unbalanced {opener}{closer} "
            f"({text.count(opener)} vs {text.count(closer)})"
        )


@pytest.mark.parametrize(("path", "line", "body"), BLOCKS, ids=block_ids())
def test_block_closes_every_subgraph(path: Path, line: int, body: list[str]) -> None:
    opened = sum(1 for raw in body if raw.strip().startswith("subgraph"))
    closed = sum(1 for raw in body if raw.strip() == "end")
    assert opened == closed, f"{path.name}:{line} has {opened} subgraph(s) and {closed} end(s)"


@pytest.mark.parametrize(("path", "line", "body"), BLOCKS, ids=block_ids())
def test_label_text_uses_no_markup_but_line_breaks(
    path: Path, line: int, body: list[str]
) -> None:
    """`<placeholder>` in a label is parsed as HTML and vanishes when rendered."""
    for offset, raw in enumerate(body, start=line + 1):
        texts = _QUOTED.findall(raw)
        stripped = raw.strip()
        if stripped.startswith("Note") and ":" in stripped:
            # Note text is unquoted, and is rendered as HTML just the same.
            texts.append(stripped.split(":", 1)[1])
        for text in texts:
            assert "<" not in _BR_TAG.sub("", text), (
                f"{path.name}:{offset} label {text!r} contains markup other than <br>; "
                f"an unknown tag is dropped silently when the diagram renders"
            )
