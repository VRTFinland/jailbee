"""The web dashboard design guide (#167) covers what an implementing agent needs.

The guide is `frontend/DESIGN.md`. It must keep up with the code: every UI
primitive in `frontend/src/components/ui/` has a row in its primitive table,
and every `--md-*` token in `tokens.css` belongs to a family in its token
table. The agent entry points and the web dashboard contributor notes point
to it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
GUIDE = FRONTEND / "DESIGN.md"
GUIDE_PATH = "frontend/DESIGN.md"
TOKENS = FRONTEND / "src" / "styles" / "tokens.css"


def _section(heading: str) -> str:
    """The guide's text under the `## heading`, up to the next `## `."""
    text = GUIDE.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"{GUIDE_PATH} has no '## {heading}' section"
    return match.group(1)


def _table_column(section: str, column: int) -> list[str]:
    """One column of every table row of `section`, header and rule rows skipped."""
    cells = []
    header = True
    for line in section.splitlines():
        if not line.startswith("|"):
            header = True
            continue
        if re.fullmatch(r"[|\s:-]+", line):
            continue
        if header:
            header = False
            continue
        cells.append(line.strip("|").split("|")[column].strip())
    return cells


def _code_spans(text: str) -> set[str]:
    return set(re.findall(r"`([^`\n]+)`", text))


def _families() -> tuple[set[str], set[str]]:
    """The token table's families: (exact names, wildcard prefixes)."""
    exact: set[str] = set()
    prefixes: set[str] = set()
    for cell in _table_column(_section("Token families"), 1):
        for span in _code_spans(cell):
            if not re.fullmatch(r"--md-[a-z0-9-]+\*?", span):
                continue
            if span.endswith("*"):
                prefixes.add(span[:-1])
            else:
                exact.add(span)
    return exact, prefixes


def _md_tokens() -> set[str]:
    return set(re.findall(r"(--md-[a-z0-9-]+)\s*:", TOKENS.read_text(encoding="utf-8")))


def test_the_primitive_table_has_a_row_for_every_ui_primitive() -> None:
    primitives = {p.stem for p in (FRONTEND / "src" / "components" / "ui").glob("*.vue")}
    assert primitives, "no primitives found"
    section = _section("Which primitive for which job")
    rows = {cell.strip("`") for cell in _table_column(section, 1)}
    assert not primitives - rows, f"{GUIDE_PATH} has no row for: {sorted(primitives - rows)}"
    assert not rows - primitives, f"{GUIDE_PATH} names unknown primitives: {rows - primitives}"


def test_every_md_token_belongs_to_a_family_in_the_token_table() -> None:
    exact, prefixes = _families()
    tokens = _md_tokens()
    assert tokens, "no --md-* tokens found"
    uncovered = sorted(
        t for t in tokens if t not in exact and not any(t.startswith(p) for p in prefixes)
    )
    assert not uncovered, f"{GUIDE_PATH} names no token family for: {uncovered}"


def test_every_family_in_the_token_table_exists() -> None:
    exact, prefixes = _families()
    tokens = _md_tokens()
    stale = sorted(
        {e for e in exact if e not in tokens}
        | {p + "*" for p in prefixes if not any(t.startswith(p) for t in tokens)}
    )
    assert not stale, f"{GUIDE_PATH} names tokens tokens.css does not define: {stale}"


def test_the_spacing_guide_names_the_supported_scale_by_value() -> None:
    section = _section("Token families")
    assert not any(name.startswith("--space-") for name in _code_spans(section))
    for px, rem in ((4, "0.25"), (8, "0.5"), (12, "0.75"), (16, "1"), (24, "1.5"), (32, "2")):
        assert f"`--md-space-{px}` {rem}rem" in section


def test_the_scoping_convention_forbids_root_scoped_rules() -> None:
    section = _section("Themes and the e-ink display profile")
    assert "Never write `:root[data-…] .x`" in section
    assert '`[data-profile="eink"] .x { … }`' in section


def test_the_guide_names_only_real_make_targets_and_npm_scripts() -> None:
    section = _section("Running the checks") + _section("Adding or changing a primitive")
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    targets = re.findall(r"make ([a-z0-9-]+)", section)
    assert {"web-test", "web-e2e", "web-e2e-snapshots"} <= set(targets)
    for target in targets:
        assert re.search(rf"^{re.escape(target)}:", makefile, re.M), f"no make target {target}"
    package = (FRONTEND / "package.json").read_text(encoding="utf-8")
    scripts = re.findall(r"npm run ([a-z:]+)", section)
    assert "lint:styles" in scripts
    for script in scripts:
        assert f'"{script}":' in package, f"no npm script {script}"


def test_the_agent_entry_points_link_the_guide() -> None:
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        links = re.findall(r"\]\(([^)\s]+)\)", text)
        assert GUIDE_PATH in links, f"{name} does not link {GUIDE_PATH}"
    assert GUIDE.is_file()


def test_the_web_dashboard_contributor_notes_name_the_guide() -> None:
    # docs/ is published on its own site, where a link out of docs/ breaks, so
    # the page names the checkout path instead of linking it.
    text = (REPO_ROOT / "docs" / "web-dashboard.md").read_text(encoding="utf-8")
    assert f"`{GUIDE_PATH}`" in text
