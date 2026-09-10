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


def test_no_published_page_references_a_relative_image() -> None:
    """Staging copies only the `.md` files `nav` names — an image referenced
    by a relative path would 404 on the site, and `--strict` does not flag a
    missing image the way it flags a missing page or anchor."""
    import re

    image_md = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
    img_tag = re.compile(r'<img\s[^>]*\bsrc="([^"]+)"', re.IGNORECASE)

    for name in docs_site.published_pages():
        text = (DOCS / name).read_text()
        for pattern in (image_md, img_tag):
            for target in pattern.findall(text):
                assert target.startswith(("http://", "https://")), (
                    f"{name}: relative image reference {target!r} would 404 on the site"
                )


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


CANONICAL = '<link rel="canonical" href="https://jailbee.gisgro.io/docs/config/">'


def _page(body: str, tmp_path: Path) -> Path:
    site = tmp_path / "_site" / "docs"
    site.mkdir(parents=True)
    (site / "index.html").write_text(f"<html><head>{CANONICAL}</head><body>{body}</body></html>")
    return site


def test_check_passes_a_page_that_only_links_out_through_anchors(tmp_path: Path) -> None:
    site = _page('<a href="https://github.com/VRTFinland/jailbee">source</a>', tmp_path)
    assert docs_site.check(site) == []


def test_check_rejects_a_script_from_a_cdn(tmp_path: Path) -> None:
    site = _page(
        '<script src="https://unpkg.com/mermaid@11/dist/mermaid.min.js"></script>', tmp_path
    )
    assert any("unpkg.com" in problem for problem in docs_site.check(site))


def test_check_rejects_a_webfont_stylesheet(tmp_path: Path) -> None:
    site = _page(
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Roboto">', tmp_path
    )
    assert any("fonts.googleapis.com" in problem for problem in docs_site.check(site))


def test_check_rejects_a_protocol_relative_image(tmp_path: Path) -> None:
    site = _page('<img src="//example.com/pixel.png">', tmp_path)
    assert any("example.com" in problem for problem in docs_site.check(site))


def test_check_rejects_the_repository_stats_component(tmp_path: Path) -> None:
    """`data-md-component="source"` makes the bundle call api.github.com from
    the reader's browser for star and release counts."""
    site = _page('<a href="https://github.com/x" data-md-component="source">x</a>', tmp_path)
    assert any("data-md-component" in problem for problem in docs_site.check(site))


def test_check_rejects_an_off_site_url_in_a_stylesheet(tmp_path: Path) -> None:
    site = _page("", tmp_path)
    (site / "extra.css").write_text("@font-face { src: url(https://fonts.gstatic.com/x.woff2); }")
    assert any("fonts.gstatic.com" in problem for problem in docs_site.check(site))


def test_check_rejects_an_off_site_url_in_an_inline_style(tmp_path: Path) -> None:
    site = _page(
        '<style>body { background: url("https://cdn.example.com/bg.png"); }</style>', tmp_path
    )
    assert any("cdn.example.com" in problem for problem in docs_site.check(site))


def test_check_notices_an_empty_build(tmp_path: Path) -> None:
    """An empty site would otherwise pass every rule above vacuously."""
    site = tmp_path / "_site" / "docs"
    site.mkdir(parents=True)
    assert any("no pages" in problem for problem in docs_site.check(site))


def test_check_rejects_an_off_site_candidate_in_a_srcset(tmp_path: Path) -> None:
    """`_ABSOLUTE.match` on the whole attribute value would miss this: the
    value starts with a local path, and only the second comma-separated
    candidate is off-site."""
    site = _page('<img srcset="local.png 1x, https://cdn.example.com/big.png 2x">', tmp_path)
    assert any("cdn.example.com" in problem for problem in docs_site.check(site))


def test_check_rejects_an_off_site_url_in_a_style_attribute(tmp_path: Path) -> None:
    site = _page('<div style="background:url(https://cdn.example.com/x.png)"></div>', tmp_path)
    assert any("cdn.example.com" in problem for problem in docs_site.check(site))


def test_check_ignores_a_css_looking_string_in_a_code_sample(tmp_path: Path) -> None:
    """A `url(...)` string inside a documentation code sample is text, not
    something the browser fetches."""
    site = _page("<pre><code>url(https://cdn.example.com/bg.png)</code></pre>", tmp_path)
    assert docs_site.check(site) == []


def test_check_rejects_a_canonical_link_pointing_off_site(tmp_path: Path) -> None:
    """Guards `value.startswith(site_url)`: only an in-site canonical is
    exempt from the off-site check, never a canonical pointing elsewhere."""
    site = tmp_path / "_site" / "docs"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        '<html><head><link rel="canonical" href="https://evil.example.com/"></head>'
        "<body></body></html>"
    )
    assert any("evil.example.com" in problem for problem in docs_site.check(site))


DOCS_CSS = REPO_ROOT / "website" / "docs-theme" / "assets" / "jailbee-docs.css"
SITE_CSS = REPO_ROOT / "website" / "assets" / "style.css"

# The landing page tokens the docs theme reuses. Fewer than style.css defines:
# these are the ones the docs actually need, and a token that is not used here
# has no business being copied.
SHARED_TOKENS = ("ground", "surface", "surface-2", "border", "text", "muted", "amber", "amber-dim")


def _tokens(css: str) -> dict[str, str]:
    import re

    return {
        name: value.strip() for name, value in re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]+)", css)
    }


def test_the_docs_palette_matches_the_landing_page() -> None:
    """One brand, two stylesheets. A colour edited on the landing page and not
    here shows up as a docs section that no longer matches the front door."""
    landing = _tokens(SITE_CSS.read_text())
    docs = _tokens(DOCS_CSS.read_text())
    for token in SHARED_TOKENS:
        assert token in docs, f"the docs stylesheet does not define --{token}"
        assert docs[token] == landing[token], (
            f"--{token} is {docs[token]} in the docs and {landing[token]} on the landing page"
        )


def test_the_docs_stylesheet_makes_no_external_requests() -> None:
    css = DOCS_CSS.read_text()
    assert "http://" not in css
    assert "https://" not in css


def test_the_docs_stylesheet_uses_the_fonts_the_site_already_ships() -> None:
    """Root-absolute paths into website/assets/fonts — the files are shipped
    once, for both halves of the site."""
    css = DOCS_CSS.read_text()
    for font in ("IBMPlexSans-Regular", "IBMPlexSans-SemiBold", "IBMPlexMono-Regular"):
        assert f"/assets/fonts/{font}.woff2" in css, f"the docs do not use {font}"
        assert (REPO_ROOT / "website" / "assets" / "fonts" / f"{font}.woff2").is_file()


def test_the_brand_layer_is_wired_into_the_theme() -> None:
    """Dropping either line silently unbrands the docs and nothing else fails:
    `extra_css` and `custom_dir` are the only two config keys that connect
    website/docs-theme/assets/jailbee-docs.css to the built site."""
    config = docs_site.config()
    assert config["extra_css"] == ["assets/jailbee-docs.css"]
    assert config["theme"]["custom_dir"] == "website/docs-theme"
    assert DOCS_CSS.is_file()


THEME = REPO_ROOT / "website" / "docs-theme"
# mermaid 11.17.2, package/dist/mermaid.min.js from the npm tarball whose
# published integrity is
# sha512-V6K3C8EBdEsPFZXSKMJe6ppQOENxuHARr9GvHX4hh47lAbhMRD9qf4oEK7LoaRQxULMa80/qt5gHO73aCleBBg==
MERMAID = "mermaid-11.17.2.min.js"
MERMAID_SHA256 = "581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8"


def test_mermaid_is_the_pinned_file_loaded_only_where_needed() -> None:
    """The theme fetches mermaid from unpkg unless something defines
    window.mermaid first. main.html does that conditionally — only on a page
    whose content contains a mermaid diagram — instead of extra_javascript
    shipping the 3.6 MB bundle on all 14 pages. This pins the file itself,
    byte for byte, and that the template is what references it."""
    import hashlib

    assert "extra_javascript" not in docs_site.config(), (
        "extra_javascript would ship mermaid on every page — load it from main.html instead"
    )
    main_html = (THEME / "main.html").read_text()
    assert f"assets/{MERMAID}" in main_html, "main.html does not reference the pinned mermaid file"
    path = THEME / "assets" / MERMAID
    assert path.is_file(), "the pinned mermaid bundle is not committed"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == MERMAID_SHA256


def test_no_configured_asset_is_an_absolute_url() -> None:
    """A CDN reference would enter through configuration, not through markup."""
    config = docs_site.config()
    for value in [*config["extra_css"], *config.get("extra_javascript", [])]:
        assert not value.startswith(("http://", "https://", "//")), value


def test_the_repository_link_does_not_call_the_github_api() -> None:
    source = (THEME / "partials" / "source.html").read_text()
    assert "data-md-component" not in source, (
        "the stock partial's component id is what triggers the api.github.com fetch"
    )
    assert "config.repo_url" in source, "the header should still link the repository"


def test_the_page_action_links_the_rendered_source_not_the_raw_file() -> None:
    actions = (THEME / "partials" / "actions.html").read_text()
    assert "/raw/" not in actions, "the stock partial rewrites blob/ to raw/"
    assert "page.edit_url" in actions


def test_the_resolver_agrees_with_github_on_punctuated_headings() -> None:
    """The 46 headings where Python-Markdown's default rule differs are the
    whole reason `toc.slugify` is configured; this pins the rule itself."""
    from tests.docs_links import github_slug

    assert github_slug("GPU / NVIDIA passthrough") == "gpu--nvidia-passthrough"
    assert github_slug("`jailbee git push` sent something I didn't expect — why?") == (
        "jailbee-git-push-sent-something-i-didnt-expect--why"
    )
    assert github_slug("Egress overrides") == "egress-overrides"


def test_the_resolver_rejects_a_page_and_an_anchor_that_do_not_exist() -> None:
    from tests.docs_links import resolve

    assert resolve("docs/config/#scratch") is None
    assert resolve("https://jailbee.gisgro.io/docs/") is None
    assert "no such page" in (resolve("docs/nope/") or "")
    assert "no heading" in (resolve("docs/config/#nope-zz") or "")
    # These four all 404 on the real site: the target file exists on disk but
    # is not named in zensical.toml's nav, or the slug is not a valid page at
    # all. "exists and is not excluded" means both have to hold.
    assert resolve("https://jailbee.gisgro.io/docs/manual-testing/") is not None
    assert resolve("docs/releasing/") is not None
    assert resolve("docs/skills/jailbee-usage/SKILL/") is not None
    assert resolve("docs/README/") is not None
