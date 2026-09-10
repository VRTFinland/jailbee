"""Checks for the documentation site's configuration and build tooling.

`docs/` is the single source for both github.com and
https://jailbee.gisgro.io/docs/, and the generator only ever runs in CI and in
`make site`. These tests are what catch a misconfiguration without one: a page
that would ship unnoticed, a staged tree that disagrees with `nav`, a colour
that has drifted from the landing page.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

# Pages that live in docs/ but are deliberately not published: maintainer
# procedure, and the skills the wheel force-includes for the in-container
# agent. Listing them here rather than in zensical.toml is the point — a new
# page has to be added to one list or the other, so it cannot ship, or fail to
# ship, by accident.
UNPUBLISHED = frozenset({"manual-testing.md", "releasing.md"})


def _load() -> ModuleType:
    path = REPO_ROOT / "scripts" / "docs_site.py"
    spec = importlib.util.spec_from_file_location("docs_site_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


docs_site = _load()


def test_every_docs_page_is_either_published_or_deliberately_not() -> None:
    published = set(docs_site.published_pages())
    on_disk = {p.name for p in DOCS.glob("*.md")}
    assert published | UNPUBLISHED == on_disk, (
        "docs/ and the publication lists disagree: "
        f"unlisted {sorted(on_disk - published - UNPUBLISHED)}, "
        f"listed but missing {sorted(published - on_disk)}"
    )


def test_no_page_is_listed_in_the_nav_twice() -> None:
    pages = docs_site.published_pages()
    assert len(pages) == len(set(pages)), "a page appears in nav more than once"


def test_the_index_page_is_first_in_the_nav() -> None:
    """`README.md` becomes /docs/ — the URL every other link resolves against."""
    assert docs_site.published_pages()[0] == "README.md"


def test_staging_copies_exactly_the_published_pages(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("README.md", "published.md", "secret.md"):
        (docs / name).write_text(f"# {name}\n")
    stage_dir = tmp_path / "_build" / "docs"

    docs_site.stage(docs=docs, stage_dir=stage_dir, pages=["README.md", "published.md"])

    assert sorted(p.name for p in stage_dir.iterdir()) == ["README.md", "published.md"]
    assert (stage_dir / "published.md").read_text() == "# published.md\n"


def test_staging_starts_from_an_empty_tree(tmp_path: Path) -> None:
    """A page dropped from nav must disappear from the next build."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# index\n")
    stage_dir = tmp_path / "_build" / "docs"
    stage_dir.mkdir(parents=True)
    (stage_dir / "dropped.md").write_text("# stale\n")

    docs_site.stage(docs=docs, stage_dir=stage_dir, pages=["README.md"])

    assert [p.name for p in stage_dir.iterdir()] == ["README.md"]


def test_staging_refuses_a_nav_entry_with_no_file(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    stage_dir = tmp_path / "_build" / "docs"
    try:
        docs_site.stage(docs=docs, stage_dir=stage_dir, pages=["missing.md"])
    except SystemExit as exc:
        assert "missing.md" in str(exc)
    else:  # pragma: no cover - the assertion below reports the failure
        raise AssertionError("staging accepted a nav entry with no file")


def test_the_config_builds_from_the_staged_tree() -> None:
    """Building straight from docs/ would publish the maintainer pages."""
    config = docs_site.config()
    assert config["docs_dir"] == "_build/docs"
    assert config["site_dir"] == "_site/docs"
    assert config["site_url"] == "https://jailbee.gisgro.io/docs/"


def test_the_config_declares_no_watch_key() -> None:
    """`watch` sends `zensical serve` into an endless rebuild loop (0.0.60)."""
    assert "watch" not in docs_site.config()


def test_headings_get_github_compatible_ids() -> None:
    """46 headings slug differently under Python-Markdown's default rule, and
    164 links in docs/ are written against GitHub's."""
    toc = docs_site.config()["markdown_extensions"]["toc"]
    assert toc["slugify"]["object"] == "pymdownx.slugs.slugify"
    assert toc["slugify"]["kwds"]["case"] == "lower"


def test_the_palette_keeps_the_theme_presets_out_of_the_way() -> None:
    """A named primary outranks the brand stylesheet on link colour."""
    palette = docs_site.config()["theme"]["palette"]
    assert [entry["scheme"] for entry in palette] == ["slate"], "dark only, by decision"
    assert palette[0]["primary"] == "custom"
    assert palette[0]["accent"] == "custom"


def test_the_docs_build_never_collides_with_a_committed_directory() -> None:
    assert not (REPO_ROOT / "website" / "docs").exists(), (
        "website/docs/ would be overwritten by the generated docs"
    )
