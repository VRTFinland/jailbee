"""Structural checks for the published website.

The site has no build step, so these tests are its only safety net: a
mistyped asset path or a stray CDN reference is invisible until the page is
live, and a font shipped without its licence is a licence violation.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "website"
INDEX = SITE / "index.html"
FONTS = SITE / "assets" / "fonts"

CANONICAL_URL = "https://jailbee.gisgro.io/"

# Discovered rather than listed, so a page added to website/ inherits every
# whole-page check below instead of shipping unverified.
PAGES = sorted(SITE.glob("*.html"))


def canonical_url_for(page: Path) -> str:
    """The URL a page must name as its own."""
    if page.name == "index.html":
        return CANONICAL_URL
    return f"{CANONICAL_URL}{page.name}"


def test_the_site_publishes_the_pages_this_module_thinks_it_does() -> None:
    """Guards the glob above: if it ever matches nothing, every per-page
    test below would pass vacuously by iterating an empty list."""
    assert {p.name for p in PAGES} == {"index.html", "comparison.html"}


# Weights the stylesheet declares. Shipping more is dead weight — even as
# woff2 these are 39-64 KB each, so every extra weight costs more than the
# page's own HTML and CSS put together. Shipping fewer breaks a @font-face
# rule silently: the browser just substitutes.
#
# woff2 only: the TrueType originals were 520 KB for the same three faces.
EXPECTED_FONTS = (
    "IBMPlexSans-Regular.woff2",
    "IBMPlexSans-SemiBold.woff2",
    "IBMPlexMono-Regular.woff2",
)


class _RefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in ("src", "href", "poster") and value:
                self.refs.append((name, value))


def collect_references(html: str) -> list[tuple[str, str]]:
    """Every ``src``/``href``/``poster`` value in the document, in order."""
    parser = _RefCollector()
    parser.feed(html)
    return parser.refs


class _TaggedRefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in ("src", "href", "poster") and value:
                self.refs.append((tag, name, value))


def collect_tagged_references(html: str) -> list[tuple[str, str, str]]:
    """Every ``src``/``href``/``poster`` value, tagged with its element name."""
    parser = _TaggedRefCollector()
    parser.feed(html)
    return parser.refs


def test_the_site_ships_a_cname_for_the_brand_domain() -> None:
    assert (SITE / "CNAME").read_text().strip() == "jailbee.gisgro.io"


def test_bundled_fonts_ship_the_open_font_licence() -> None:
    for name in EXPECTED_FONTS:
        assert (FONTS / name).is_file(), f"missing font file: {name}"
    licence = (FONTS / "OFL.txt").read_text()
    assert "SIL OPEN FONT LICENSE Version 1.1" in licence
    assert "IBM Corp" in licence, "the licence must carry IBM Plex's copyright line"
    # The licence text has five numbered sections after the preamble; a
    # truncated extraction (e.g. cut off right after PREAMBLE) must fail
    # here rather than pass silently — a partial licence is worse than none.
    for heading in (
        "DEFINITIONS",
        "PERMISSION & CONDITIONS",
        "TERMINATION",
        "DISCLAIMER",
    ):
        assert heading in licence, f"licence text is truncated before {heading}"
    assert licence.rstrip().endswith("OTHER DEALINGS IN THE FONT SOFTWARE."), (
        "licence text is truncated before the end of the DISCLAIMER paragraph"
    )


def test_no_italic_or_unused_weights_are_shipped() -> None:
    """Also catches a left-behind .ttf after the woff2 conversion."""
    shipped = {p.name for p in FONTS.iterdir() if p.suffix in (".woff2", ".ttf", ".woff")}
    assert shipped == set(EXPECTED_FONTS)


def test_the_hero_illustrations_are_present() -> None:
    img = SITE / "assets" / "img"
    # The page uses the transparent PNG; the opaque JPG stays for og:image,
    # since link-preview scrapers want an image that carries its own
    # background rather than one that inherits the page's.
    assert (img / "jailbee-logo.png").is_file()
    assert (img / "jailbee-logo-dark.jpg").is_file()
    assert (img / "hive-entrance.jpg").is_file()
    assert (img / "hive-observation.jpg").is_file()


def test_the_authored_installer_state_agrees_with_itself() -> None:
    """Four hand-written places have to name the same installer.

    The toggle script rewrites the command and the copy payload only when
    someone clicks, so the HTML as authored is what every first visitor
    sees. Swap which installer leads and miss one of the four — the block's
    `data-installer`, which tab is `aria-pressed`, the visible command, the
    copy button's payload — and the page ships a pressed `pipx` tab above a
    `uv tool install` line.
    """
    import re

    html = INDEX.read_text()

    class _Tabs(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.pressed: list[str] = []
            self.choices: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            a = dict(attrs)
            if tag == "button" and "install__tab" in (a.get("class") or ""):
                choice = a.get("data-installer-choice") or ""
                self.choices.append(choice)
                if a.get("aria-pressed") == "true":
                    self.pressed.append(choice)

    default = re.search(r'class="install" data-installer="([a-z]+)"', html)
    assert default, "the hero install block declares no default installer"
    choice = default.group(1)

    tabs = _Tabs()
    tabs.feed(html)
    assert sorted(tabs.choices) == ["pipx", "uv"], f"unexpected installer tabs: {tabs.choices}"
    assert tabs.pressed == [choice], (
        f"data-installer is {choice!r} but the pressed tab(s) are {tabs.pressed}"
    )

    command = re.search(rf'data-cmd-{choice}="([^"]+)"', html)
    assert command, f"no data-cmd-{choice} to render as the default command"
    expected = command.group(1)

    shown = re.search(r"<code data-cmd-[^>]*>\s*([^<>]+?)\s*</code", html)
    assert shown and shown.group(1) == expected, (
        f"the visible command is {shown and shown.group(1)!r}, expected {expected!r}"
    )

    copy = re.search(r'<button class="copy" data-copy="([^"]+)"', html)
    assert copy and copy.group(1) == expected, (
        f"the copy button carries {copy and copy.group(1)!r}, expected {expected!r}"
    )


def test_the_stylesheet_link_carries_its_current_content_hash() -> None:
    """Cache-bust the stylesheet, and make forgetting to bump it a failure.

    GitHub Pages publishes `website/` verbatim — there is no build step to
    write a hashed filename, and no control over the cache headers it
    serves. So the link carries `?v=<sha256[:8]>` instead, and this test is
    what keeps it honest: edit the CSS without updating the query and it
    fails here rather than by silently serving a reader the old file.
    """
    import hashlib

    css = (SITE / "assets" / "style.css").read_bytes()
    expected = hashlib.sha256(css).hexdigest()[:8]
    href = f"assets/style.css?v={expected}"
    for page in PAGES:
        assert href in page.read_text(), (
            f"{page.name}: stylesheet cache-buster is stale — set the <link> href to {href!r}"
        )


def test_every_local_reference_resolves_on_disk() -> None:
    refs = [
        (tag, attr, value)
        for page in PAGES
        for tag, attr, value in collect_tagged_references(page.read_text())
    ]
    local = [
        (tag, attr, v)
        for tag, attr, v in refs
        if not v.startswith(("http://", "https://", "#", "mailto:"))
    ]
    assert local, "the pages reference no local assets at all"
    for tag, attr, value in local:
        # The one exemption: a demo clip's <source src="assets/media/...">
        # is legitimately absent until someone runs demo/render.sh on a
        # host (vhs/ttyd/ffmpeg aren't installed here and can't be). Every
        # other reference — including the poster= on the same <video>,
        # which is committed and is what the page actually shows today —
        # must still resolve.
        if tag == "source" and attr == "src" and value.startswith("assets/media/"):
            continue
        target = (SITE / value.split("?", 1)[0]).resolve()
        # A directory reference (`./`, `subdir/`) is what a server resolves
        # to that directory's index.html — so check the file it will
        # actually serve, rather than rejecting the link as broken.
        if target.is_dir():
            target = target / "index.html"
        assert target.is_file(), f"broken local reference: {value}"


def test_only_anchors_use_absolute_urls() -> None:
    """No CDN, no webfont service, no third-party image — ever.

    The one exemption per page is its own canonical link. A canonical link
    names which host should rank; unlike a stylesheet, script or image
    reference it causes no fetch, so it cannot drag in a CDN — which is the
    only thing this test exists to prevent.
    """
    for page in PAGES:
        exempt = ("link", "href", canonical_url_for(page))
        for tag, attr, value in collect_tagged_references(page.read_text()):
            if (tag, attr, value) == exempt:
                continue
            if value.startswith(("http://", "https://")):
                assert tag == "a" and attr == "href", (
                    f"{page.name}: non-anchor absolute URL: <{tag} {attr}={value!r}>"
                )


def test_every_page_declares_its_own_canonical_url() -> None:
    """GitHub Pages serves the same bytes from two hosts; name the winner.

    Asserted on `rel="canonical"` specifically rather than on the URL
    alone, so that dropping the rel — which silently turns the tag into a
    no-op the exemption above still waves through — fails here. Asserted
    per page so that a new page cannot copy index.html's canonical and
    quietly declare itself a duplicate of the front page.
    """
    for page in PAGES:
        expected = f'<link rel="canonical" href="{canonical_url_for(page)}" />'
        assert expected in page.read_text(), (
            f"{page.name} must declare exactly one canonical URL: {expected}"
        )


VERDICTS = frozenset({"mx--yes", "mx--no", "mx--partial", "mx--na"})


def test_every_verdict_cell_is_readable_without_colour_or_sight() -> None:
    """Green/red ticks are the fastest way to read the grid and the easiest
    to get wrong. Each cell has to carry its answer three ways: the colour,
    the glyph shape (for red-green colour deficiency), and text (for a
    screen reader, which sees neither). This asserts the latter two, since
    a cell that loses its hidden text still *looks* perfect.
    """
    import re

    html = (SITE / "comparison.html").read_text()
    cells = re.findall(r'<td class="(mx[^"]*)">(.*?)</td>', html, re.DOTALL)
    assert len(cells) >= 40, f"expected a full verdict grid, found {len(cells)} cells"

    for classes, body in cells:
        verdict = set(classes.split()) & VERDICTS
        assert len(verdict) == 1, f"cell must carry exactly one verdict class, got {classes!r}"

        assert 'class="mx__mark" aria-hidden="true"' in body, (
            f"{classes}: verdict cell has no glyph, so colour is its only carrier"
        )

        # Whatever is left once the aria-hidden glyph and all markup are
        # gone is what a screen reader actually announces. `</span\s*>`
        # rather than `</span>`: the markup breaks the line before the
        # closing bracket to avoid a space between glyph and text, which is
        # valid HTML that a stricter pattern silently skips past — taking
        # the very text this test exists to find with it.
        readable = re.sub(r'<span class="mx__mark"[^>]*>.*?</span\s*>', "", body, flags=re.DOTALL)
        readable = re.sub(r"<[^>]+>", "", readable).strip()
        assert readable, f"{classes}: verdict cell announces nothing to a screen reader"


def test_the_stylesheet_makes_no_external_requests() -> None:
    css = (SITE / "assets" / "style.css").read_text()
    assert "http://" not in css
    assert "https://" not in css


def test_every_page_has_exactly_one_top_level_heading() -> None:
    for page in PAGES:
        assert page.read_text().count("<h1") == 1, f"{page.name} needs exactly one <h1>"


DOC_LINK_PREFIX = "https://github.com/VRTFinland/jailbee/blob/main/"


def test_documentation_links_point_at_files_that_exist_in_this_repo() -> None:
    """Verified against the local tree, which is what the repo will publish.

    Every page, not just the front one: comparison.html hands the reader
    off to docs/comparison.md, and a rename there would otherwise break
    that link silently.
    """
    for page in PAGES:
        refs = collect_references(page.read_text())
        doc_links = [v for a, v in refs if a == "href" and v.startswith(DOC_LINK_PREFIX)]
        for link in doc_links:
            path = REPO_ROOT / link[len(DOC_LINK_PREFIX) :]
            assert path.is_file(), (
                f"{page.name}: documentation link has no local counterpart: {link}"
            )

    index_links = [
        v
        for a, v in collect_references(INDEX.read_text())
        if a == "href" and v.startswith(DOC_LINK_PREFIX)
    ]
    assert len(index_links) >= 6, "the docs section should link at least six documents"


def test_every_expected_section_id_exists() -> None:
    html = INDEX.read_text()
    for anchor in ("network", "desktop", "agents", "features", "install", "docs"):
        assert f'id="{anchor}"' in html, f"missing section: {anchor}"


def test_incus_is_not_named_in_the_visible_hero() -> None:
    """Positioning rule: Incus belongs in Requirements, not the pitch.

    Scoped to what a reader sees. The meta description names Incus on
    purpose — it is what someone searching for this tool types — and the
    rule is about the space the pitch occupies, not about hiding the
    dependency.
    """
    html = INDEX.read_text()
    # Bounded by the first section after the pitch. That used to be #demos;
    # it is #network while the clips are out.
    body = html[html.index("</head>") : html.index('id="network"')]
    assert "Incus" not in body


def test_the_page_never_calls_a_container_a_machine() -> None:
    assert "machine" not in INDEX.read_text().lower()


def test_generated_scenes_match_what_jailbees_renderer_produces_today() -> None:
    """The demo table is real output over invented data — keep it that way.

    If a column is renamed or dropped in the code, this fails and the scene
    is regenerated, rather than the site quietly showing a table that the
    tool no longer prints.
    """
    import sys

    sys.path.insert(0, str(SITE / "demo"))
    try:
        import generate
    finally:
        sys.path.pop(0)

    generated = SITE / "demo" / "scenes" / "generated"
    assert generate.render_ls() == (generated / "ls.txt").read_text()
    assert generate.render_net_switch() == (generated / "net-switch.txt").read_text()


def test_the_page_ships_no_clips_while_they_are_being_rerecorded() -> None:
    """The staged clips were pulled for 1.0; real recordings replace them.

    Asserted positively rather than by deleting the old checks, which would
    have left nothing to notice a half-finished restoration. When a clip
    comes back this fails, and whoever brings it back owes the page the
    guards this test replaced: every `<video>` muted, looping, playsinline,
    `controls`, `preload="none"` and carrying a `poster=`; every
    `.tabs__radio` matched by a `for=` label and a `#demo-*` panel, with
    exactly one `checked`. Restore them from git history alongside the clip
    rather than rewriting them from scratch.
    """
    html = INDEX.read_text()
    assert "<video" not in html, "a clip is back — restore the playback guards with it"
    assert "tabs__radio" not in html, "the demo tabs are back — restore their wiring test"


def test_no_media_file_is_a_zero_byte_stand_in() -> None:
    """An empty file that "exists" but plays nothing is worse than no file.

    website/demo/render.sh is the only thing that should ever write here,
    and it writes real clips. Nothing should shortcut
    test_every_local_reference_resolves_on_disk's one media exemption by
    committing an empty file at the path it stops checking.
    """
    media_dir = SITE / "assets" / "media"
    if not media_dir.is_dir():
        return
    for path in media_dir.iterdir():
        if path.is_file():
            assert path.stat().st_size > 0, f"zero-byte media file: {path.name}"


def test_the_stylesheet_honours_reduced_motion() -> None:
    css = (SITE / "assets" / "style.css").read_text()
    assert "prefers-reduced-motion: reduce" in css


def test_render_script_is_executable() -> None:
    import os

    script = SITE / "demo" / "render.sh"
    assert os.access(script, os.X_OK)


def test_robots_allows_crawling_and_points_at_the_sitemap() -> None:
    """A stray ``Disallow: /`` here would delist the site silently."""
    robots = (SITE / "robots.txt").read_text()
    assert "Sitemap: https://jailbee.gisgro.io/sitemap.xml" in robots
    disallows = [
        line.split(":", 1)[1].strip()
        for line in robots.splitlines()
        if line.lower().startswith("disallow:")
    ]
    assert "/" not in disallows, "robots.txt disallows the whole site"


def test_the_sitemap_lists_every_page_the_site_publishes() -> None:
    """Add a page under website/ and the sitemap has to learn about it.

    Without this, a second page ships unlisted and the sitemap quietly
    describes a site that no longer exists.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring((SITE / "sitemap.xml").read_bytes())
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    listed = {loc.text for loc in root.findall(".//sm:url/sm:loc", ns)}

    published = {p.name for p in SITE.glob("*.html")}
    expected = {
        CANONICAL_URL if name == "index.html" else f"{CANONICAL_URL}{name}" for name in published
    }
    assert listed == expected, (
        f"sitemap lists {sorted(listed)}, but website/ publishes {sorted(published)}"
    )


def test_llms_txt_follows_the_format_and_links_only_to_real_docs() -> None:
    """The file LLM crawlers read. A dead link here is a wrong answer later."""
    import re

    text = (SITE / "llms.txt").read_text()
    assert text.startswith("# JailBee\n"), "llms.txt must open with an H1 naming the project"
    assert "\n> " in text, "llms.txt must carry a blockquote summary after the H1"

    for link in re.findall(rf"\]\({re.escape(DOC_LINK_PREFIX)}([^)]+)\)", text):
        assert (REPO_ROOT / link).is_file(), f"llms.txt links a missing document: {link}"


def _structured_data() -> dict[str, object]:
    import json
    import re

    html = INDEX.read_text()
    block = re.search(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', html, re.DOTALL
    )
    assert block, "the page ships no JSON-LD block"
    parsed = json.loads(block.group(1))
    assert isinstance(parsed, dict)
    return parsed


def test_the_structured_data_describes_this_software() -> None:
    data = _structured_data()
    assert data["@type"] == "SoftwareApplication"
    assert data["name"] == "JailBee"
    assert data["url"] == CANONICAL_URL
    # The positioning rule of test_the_page_never_calls_a_container_a_machine
    # applies to the description a search engine quotes, too.
    assert "machine" not in str(data["description"]).lower()


def test_the_structured_data_version_tracks_pyproject() -> None:
    """Bump the version and forget this, and the page advertises the old one."""
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert _structured_data()["softwareVersion"] == version, (
        f"JSON-LD softwareVersion is stale — set it to {version!r}"
    )
