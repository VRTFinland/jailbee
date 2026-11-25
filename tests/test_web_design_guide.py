"""The web dashboard design guide (#167) covers what an implementing agent needs.

The guide is `frontend/DESIGN.md`. It must keep up with the code: every UI
primitive in `frontend/src/components/ui/` is named in it, and every `--md-*`
token in `tokens.css` belongs to a token family it names. The agent entry
points and the web dashboard contributor notes point to it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
GUIDE = FRONTEND / "DESIGN.md"
GUIDE_PATH = "frontend/DESIGN.md"


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _code_spans(text: str) -> set[str]:
    return set(re.findall(r"`([^`\n]+)`", text))


def _families(text: str) -> tuple[set[str], set[str]]:
    """The guide's token families: (exact names, wildcard prefixes)."""
    exact: set[str] = set()
    prefixes: set[str] = set()
    for span in _code_spans(text):
        if not re.fullmatch(r"--md-[a-z0-9-]+\*?", span):
            continue
        if span.endswith("*"):
            prefixes.add(span[:-1])
        else:
            exact.add(span)
    return exact, prefixes


def _md_tokens() -> set[str]:
    css = (FRONTEND / "src" / "styles" / "tokens.css").read_text(encoding="utf-8")
    return set(re.findall(r"(--md-[a-z0-9-]+)\s*:", css))


def test_the_guide_names_every_ui_primitive() -> None:
    primitives = sorted(p.stem for p in (FRONTEND / "src" / "components" / "ui").glob("*.vue"))
    assert primitives, "no primitives found"
    spans = _code_spans(_guide())
    missing = [name for name in primitives if name not in spans and f"<{name}>" not in spans]
    assert not missing, f"{GUIDE_PATH} does not name these primitives: {missing}"


def test_every_md_token_belongs_to_a_family_the_guide_names() -> None:
    exact, prefixes = _families(_guide())
    tokens = _md_tokens()
    assert tokens, "no --md-* tokens found"
    uncovered = sorted(t for t in tokens if t not in exact and not any(t.startswith(p) for p in prefixes))
    assert not uncovered, f"{GUIDE_PATH} names no token family for: {uncovered}"


# The guide's never-by-index example names a token that must not exist.
COUNTEREXAMPLES = frozenset({"--md-space-3"})


def test_every_family_the_guide_names_exists() -> None:
    exact, prefixes = _families(_guide())
    exact -= COUNTEREXAMPLES
    tokens = _md_tokens()
    assert not COUNTEREXAMPLES & tokens, "a counterexample token now exists"
    stale = sorted({e for e in exact if e not in tokens} | {p + "*" for p in prefixes if not any(t.startswith(p) for t in tokens)})
    assert not stale, f"{GUIDE_PATH} names tokens tokens.css does not define: {stale}"


def test_the_guide_states_the_spacing_and_scoping_rules() -> None:
    text = _guide()
    for needle in (
        "--space-3",  # the never-by-index migration example
        ':root[data-',  # the forbidden scoping form, named as such
        '[data-profile="eink"]',
        "make web-e2e-snapshots",
        "npm run lint:styles",
        "tokens.css",
    ):
        assert needle in text, f"{GUIDE_PATH} lacks {needle!r}"


def test_the_agent_entry_points_link_the_guide() -> None:
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        links = re.findall(r"\]\(([^)\s]+)\)", text)
        assert GUIDE_PATH in links, f"{name} does not link {GUIDE_PATH}"
        assert (REPO_ROOT / GUIDE_PATH).is_file()


def test_the_web_dashboard_contributor_notes_name_the_guide() -> None:
    # docs/ is published on its own site, where a link out of docs/ breaks, so
    # the page names the checkout path instead of linking it.
    text = (REPO_ROOT / "docs" / "web-dashboard.md").read_text(encoding="utf-8")
    assert f"`{GUIDE_PATH}`" in text
