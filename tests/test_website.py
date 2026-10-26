"""Structural checks for the published website.

`website/` ships as committed — no build step, no bundler — so these tests are
its only safety net: a mistyped asset path or a stray CDN reference is
invisible until the page is live, and a font shipped without its licence is a
licence violation. The documentation under /docs/ *is* generated;
tests/test_docs_site.py and the strict build cover that half.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from tests.docs_links import UNPUBLISHED

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "website"
INDEX = SITE / "index.html"
FONTS = SITE / "assets" / "fonts"

CANONICAL_URL = "https://jailbee.gisgro.io/"


def _is_redirect(page: Path) -> bool:
    return 'http-equiv="refresh"' in page.read_text()


# Discovered rather than listed, so a page added to website/ inherits every
# whole-page check below instead of shipping unverified. A stub left at a
# retired URL is not a page — it has no content to check, and it names its
# target as canonical rather than itself — so it gets the redirect test
# instead.
_HTML_FILES = sorted(SITE.glob("*.html"))
PAGES = [p for p in _HTML_FILES if not _is_redirect(p)]
REDIRECTS = [p for p in _HTML_FILES if _is_redirect(p)]


def canonical_url_for(page: Path) -> str:
    """The URL a page must name as its own."""
    if page.name == "index.html":
        return CANONICAL_URL
    return f"{CANONICAL_URL}{page.name}"


def test_the_site_publishes_the_pages_this_module_thinks_it_does() -> None:
    """Guards the glob above: if it ever matches nothing, every per-page
    test below would pass vacuously by iterating an empty list."""
    assert {p.name for p in PAGES} == {"index.html"}
    assert {p.name for p in REDIRECTS} == {"comparison.html"}


def test_every_retired_url_redirects_into_the_docs() -> None:
    """A retired page's URL is still out there — in search results and in
    links posted elsewhere — so its stub must land on a page that exists.

    comparison.html moved to docs/comparison/. The refresh target, the
    canonical link and the visible fallback link must all name the same
    published page, or the stub sends a reader, a crawler and a no-refresh
    browser to three different places.
    """
    import re

    from tests.docs_links import resolve

    for page in REDIRECTS:
        html = page.read_text()
        refresh = re.search(r'<meta http-equiv="refresh" content="0; url=([^"]+)" />', html)
        assert refresh, f"{page.name}: no zero-delay refresh"
        target = refresh.group(1)
        problem = resolve(target)
        assert problem is None, f"{page.name}: {problem}"
        assert f'<link rel="canonical" href="{CANONICAL_URL}{target}" />' in html, (
            f"{page.name}: the canonical link must name the refresh target"
        )
        hrefs = [v for attr, v in collect_references(html) if attr == "href"]
        assert target in hrefs, f"{page.name}: no visible link to {target} without refresh"


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


class _ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def collect_elements(html: str) -> list[tuple[str, dict[str, str | None]]]:
    """Every start tag with its attributes, in document order.

    Parsed rather than string-matched because the demo markup carries
    comments that mention `<video>` and the tab classes by name, and a
    `html.split("<video")` counts those as elements — which is exactly how
    the first version of the click-to-play tests below "found" two clips on
    a page with one. A boolean attribute (``muted``, ``controls``) arrives
    with a value of None, so test membership, not truthiness.
    """
    parser = _ElementCollector()
    parser.feed(html)
    return parser.elements


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
    # The page uses the transparent PNG. The opaque JPG is no longer the
    # og:image — jailbee-og.png is — but README.md still renders it at the
    # top of the repo page, so it stays.
    assert (img / "jailbee-logo.png").is_file()
    assert (img / "jailbee-logo-dark.jpg").is_file()
    assert (img / "hive-entrance.jpg").is_file()
    assert (img / "hive-observation.jpg").is_file()


OG_CARD = SITE / "assets" / "img" / "jailbee-og.png"


def _png_dimensions(path: Path) -> tuple[int, int]:
    """Width and height straight out of the IHDR chunk.

    Read by hand rather than with Pillow: the card is generated by
    scripts/make_og_card.py, which declares Pillow inline (PEP 723) so that
    the dev group doesn't carry an image library for a file regenerated
    about once a year. The test suite must not need it either.
    """
    import struct

    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_the_link_preview_card_is_the_ratio_platforms_render_large() -> None:
    """1200x630 is load-bearing, not decorative.

    Slack, Signal, LinkedIn and X render a large card at 1.91:1 and fall
    back to a small square thumbnail at anything else — which is exactly
    what this card was made to stop. Regenerating it with a different mark
    size or a longer tagline must not silently change the ratio.
    """
    assert OG_CARD.is_file(), "the link-preview card is missing — run scripts/make_og_card.py"
    assert _png_dimensions(OG_CARD) == (1200, 630)


def test_every_page_advertises_the_card_at_its_true_size() -> None:
    """og:image:width/height are a promise about the file.

    Some scrapers lay the card out from these before fetching the image, so
    a stale pair renders the preview at the wrong size. Asserted against the
    PNG itself rather than against literals, so the file and the tags cannot
    drift apart.
    """
    width, height = _png_dimensions(OG_CARD)
    for page in PAGES:
        html = page.read_text()
        assert f'content="{CANONICAL_URL}assets/img/{OG_CARD.name}"' in html, (
            f"{page.name} does not point og:image at {OG_CARD.name}"
        )
        assert f'<meta property="og:image:width" content="{width}" />' in html, (
            f"{page.name}: og:image:width does not match the card's {width}px"
        )
        assert f'<meta property="og:image:height" content="{height}" />' in html, (
            f"{page.name}: og:image:height does not match the card's {height}px"
        )


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
    from tests.docs_links import resolve

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
        # other reference — including the poster= on the same <video> —
        # must still resolve. For clips the page actually ships, the
        # exemption is closed again by
        # test_every_clip_the_page_references_is_actually_committed.
        if tag == "source" and attr == "src" and value.startswith("assets/media/"):
            continue
        # A docs/ link is served by the generated documentation, which is not
        # in website/ — route it through the resolver instead of the
        # filesystem.
        if value.startswith("docs/"):
            problem = resolve(value)
            assert problem is None, f"{tag} {attr}={value!r}: {problem}"
            continue
        if value == "news/":
            from tempfile import TemporaryDirectory

            from scripts.news_site import build

            with TemporaryDirectory() as output:
                built = Path(output)
                build(SITE, built)
                assert (built / "news" / "index.html").is_file()
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


def test_every_subpage_links_back_to_the_front_page() -> None:
    """A subpage here is one a reader can arrive on cold, from a search
    result. Without a way to the front page from it, the site is one page
    deep and a dead end. (There is no subpage today; this is for the next.)
    """
    for page in PAGES:
        if page.name == "index.html":
            continue
        hrefs = {v for attr, v in collect_references(page.read_text()) if attr == "href"}
        assert hrefs & {"./", "/", "index.html"}, (
            f"{page.name} offers no link back to the front page"
        )


def test_the_stylesheet_makes_no_external_requests() -> None:
    css = (SITE / "assets" / "style.css").read_text()
    assert "http://" not in css
    assert "https://" not in css


def test_every_page_has_exactly_one_top_level_heading() -> None:
    for page in PAGES:
        assert page.read_text().count("<h1") == 1, f"{page.name} needs exactly one <h1>"


def test_documentation_links_land_on_published_pages() -> None:
    """The site links its own docs; only unpublished pages go to GitHub.

    Every page, not just the front one: a subpage handing the reader off to
    a document breaks just as silently when that document is renamed.
    Anchors are resolved too — a heading that no longer exists is a link
    into the middle of nowhere.
    """
    from tests.docs_links import GITHUB_DOCS_PREFIX, is_docs_link, resolve

    # The documentation's front door on GitHub — the README's documentation
    # table and the docs/ directory listing. The site has its own at docs/, and
    # neither of these starts with GITHUB_DOCS_PREFIX, so the check below
    # would wave them through.
    github_docs_indexes = {
        "https://github.com/VRTFinland/jailbee#documentation",
        "https://github.com/VRTFinland/jailbee/tree/main/docs",
    }

    for page in PAGES:
        for attribute, value in collect_references(page.read_text()):
            if attribute != "href":
                continue
            assert value.rstrip("/") not in github_docs_indexes, (
                f"{page.name}: {value} sends the reader to GitHub — link docs/ instead"
            )
            if is_docs_link(value):
                problem = resolve(value)
                assert problem is None, f"{page.name}: {problem}"
            elif value.startswith(GITHUB_DOCS_PREFIX):
                name = value[len(GITHUB_DOCS_PREFIX) :]
                assert name in UNPUBLISHED, (
                    f"{page.name}: {name} is published — link the site, not GitHub"
                )
                assert (REPO_ROOT / "docs" / name).is_file(), (
                    f"{page.name}: documentation link has no local counterpart: {value}"
                )

    index_links = [
        value
        for attribute, value in collect_references(INDEX.read_text())
        if attribute == "href" and is_docs_link(value)
    ]
    assert len(index_links) >= 6, "the docs section should link at least six documents"


def test_every_expected_section_id_exists() -> None:
    html = INDEX.read_text()
    for anchor in (
        "parallel",
        "orchestration",
        "demos",
        "usecases",
        "network",
        "desktop",
        "agents",
        "features",
        "install",
        "docs",
    ):
        assert f'id="{anchor}"' in html, f"missing section: {anchor}"


def test_orchestration_is_discoverable_without_the_news_feed() -> None:
    html = INDEX.read_text()
    start = html.index('<section id="orchestration">')
    section = html[start : html.index("</section>", start)]
    hrefs = [value for attr, value in collect_references(section) if attr == "href"]
    assert "docs/orchestration/" in hrefs
    assert "https://youtu.be/z-i82M_jzQw" in hrefs
    hero = html[: html.index('<section id="demos">')]
    assert "orchestration" not in hero.lower()
    assert html.index('<section id="agents">') < start
    assert start < html.index('<section id="features">')


def test_incus_is_not_named_in_the_visible_hero() -> None:
    """Positioning rule: Incus belongs in Requirements, not the pitch.

    Scoped to what a reader sees. The meta description names Incus on
    purpose — it is what someone searching for this tool types — and the
    rule is about the space the pitch occupies, not about hiding the
    dependency.
    """
    html = INDEX.read_text()
    # Bounded by #network. #problem, #parallel and #demos all sit inside the
    # space this rule protects, which is the intent: the whole run-up to the
    # requirements is pitch.
    body = html[html.index("</head>") : html.index('id="network"')]
    assert "Incus" not in body


def test_the_page_never_calls_a_container_a_machine() -> None:
    assert "machine" not in INDEX.read_text().lower()


def _demos(html: str) -> str:
    return html[
        html.index('<section id="demos">') : html.index(
            "</section>", html.index('<section id="demos">')
        )
    ]


def test_the_tour_is_a_click_to_load_player() -> None:
    """Nothing reaches YouTube until the reader presses play.

    The tour is a YouTube video, so the page carries a local thumbnail and a
    button, and assets/lite-yt.js swaps in the player on click. An iframe or
    an embed URL in the page itself would load YouTube for every visitor, and
    a thumbnail fetched from i.ytimg.com would do the same.
    """
    html = INDEX.read_text()
    demos = _demos(html)
    elements = collect_elements(demos)
    players = [a for t, a in elements if t == "div" and "yt" in (a.get("class") or "").split()]
    assert len(players) == 1, "the tour should be exactly one click-to-load player"
    video_id = players[0].get("data-yt-id")
    assert video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
    buttons = [a for t, a in elements if t == "button" and "yt__play" in (a.get("class") or "")]
    assert len(buttons) == 1 and buttons[0].get("aria-label"), "the player needs a named button"
    tour = demos[demos.index('<figure class="demo" id="demo-tour">') :]
    tour = tour[: tour.index("</figure>")]
    thumbnails = [a.get("src") for t, a in collect_elements(tour) if t == "img"]
    assert len(thumbnails) == 1
    thumbnail = thumbnails[0]
    assert thumbnail and thumbnail.startswith("assets/videos/")
    assert (SITE / thumbnail).is_file(), f"the tour's thumbnail is not committed: {thumbnail}"
    assert "<iframe" not in html and "youtube-nocookie" not in html and "ytimg" not in html
    assert f'href="https://www.youtube.com/watch?v={video_id}"' in demos, (
        "no plain link to the video — a reader without JavaScript has no way to it"
    )
    import hashlib

    script = SITE / "assets" / "lite-yt.js"
    expected = hashlib.sha256(script.read_bytes()).hexdigest()[:8]
    assert f'<script src="assets/lite-yt.js?v={expected}" defer></script>' in html, (
        f"the player script is missing or its cache-buster is stale — use ?v={expected}"
    )


def test_the_demo_tabs_are_wired() -> None:
    """A radio group fails silently: a label pointing at nothing, a panel with
    no tab, or a tab the stylesheet has no :checked rule for all just show the
    wrong panel, or none."""
    elements = collect_elements(_demos(INDEX.read_text()))
    css = (SITE / "assets" / "style.css").read_text()
    radios = [a for t, a in elements if t == "input" and "tabs__radio" in (a.get("class") or "")]
    panels = [
        a.get("id") for t, a in elements if t == "figure" and "demo" in (a.get("class") or "")
    ]
    assert len(radios) >= 2 and len(radios) == len(panels)
    labelled = {a.get("for") for t, a in elements if t == "label"}
    for attrs in radios:
        tab_id = attrs.get("id")
        assert tab_id and tab_id in labelled, f"tab {tab_id} has no label"
        panel_id = tab_id.replace("tab-", "demo-")
        assert panel_id in panels, f"tab {tab_id} points at no panel"
        assert f"#{tab_id}:checked ~ .tabs__panels > #{panel_id}" in css, (
            f"style.css never shows #{panel_id} when #{tab_id} is checked"
        )
        assert f'#{tab_id}:checked ~ .tabs__bar .tabs__tab[for="{tab_id}"]' in css, (
            f"style.css never highlights the tab for #{tab_id}"
        )
    assert sum(1 for a in radios if "checked" in a) == 1, "exactly one tab must start selected"


def test_every_demo_panel_is_one_16_9_frame() -> None:
    """Panels of different heights make the page jump under the reader on
    every tab switch. Each picture declares 16:9 and the file really is."""
    from scripts.news_site import _image_size

    elements = collect_elements(_demos(INDEX.read_text()))
    pictures = [a for t, a in elements if t == "img"]
    assert len(pictures) >= 2
    for attrs in pictures:
        src = attrs.get("src") or ""
        width, height = int(attrs.get("width") or 0), int(attrs.get("height") or 0)
        assert width * 9 == height * 16, f"{src} is declared {width}x{height}, not 16:9"
        assert _image_size(SITE / src) == (width, height), f"{src} is not {width}x{height}"


def test_the_command_line_recording_is_committed() -> None:
    """The recording plays from data- attributes that no reference check
    reads, so a renamed file would fail only when a reader clicks."""
    elements = collect_elements(_demos(INDEX.read_text()))
    runs = [a for t, a in elements if t == "a" and "demo__run" in (a.get("class") or "")]
    assert len(runs) == 1, "the command-line panel has lost its recording link"
    for attr in ("href", "data-webm", "data-poster"):
        path = runs[0].get(attr) or ""
        assert path.startswith("assets/media/"), f"{attr}={path!r} is not a committed clip"
        assert (SITE / path).is_file(), f"{attr} points at a missing file: {path}"


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


def test_robots_allows_crawling_and_points_at_both_sitemaps() -> None:
    """A stray ``Disallow: /`` here would delist the site silently."""
    robots = (SITE / "robots.txt").read_text()
    assert "Sitemap: https://jailbee.gisgro.io/sitemap.xml" in robots
    # The docs half has its own sitemap, generated by the build from site_url.
    assert "Sitemap: https://jailbee.gisgro.io/docs/sitemap.xml" in robots
    disallows = [
        line.split(":", 1)[1].strip()
        for line in robots.splitlines()
        if line.lower().startswith("disallow:")
    ]
    assert "/" not in disallows, "robots.txt disallows the whole site"


def test_the_sitemap_lists_every_page_the_site_publishes() -> None:
    """Add a page under website/ and the sitemap has to learn about it.

    Without this, a second page ships unlisted and the sitemap quietly
    describes a site that no longer exists. A redirect stub is not listed:
    a sitemap names pages to index, and the stub's target is already in the
    docs sitemap.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring((SITE / "sitemap.xml").read_bytes())
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    listed = {loc.text for loc in root.findall(".//sm:url/sm:loc", ns)}

    published = {p.name for p in PAGES}
    expected = {
        CANONICAL_URL if name == "index.html" else f"{CANONICAL_URL}{name}" for name in published
    }
    assert listed == expected, (
        f"sitemap lists {sorted(listed)}, but website/ publishes {sorted(published)}"
    )


def test_llms_txt_follows_the_format_and_links_only_to_real_docs() -> None:
    """The file LLM crawlers read. A dead link here is a wrong answer later."""
    import re

    from tests.docs_links import GITHUB_DOCS_PREFIX, SITE_DOCS_URL, resolve

    text = (SITE / "llms.txt").read_text()
    assert text.startswith("# JailBee\n"), "llms.txt must open with an H1 naming the project"
    assert "\n> " in text, "llms.txt must carry a blockquote summary after the H1"

    links = re.findall(rf"{re.escape(SITE_DOCS_URL)}[^\s\)\"']*", text)
    assert links, "llms.txt links no documentation at all"
    for link in links:
        problem = resolve(link)
        assert problem is None, problem

    for link in re.findall(rf"{re.escape(GITHUB_DOCS_PREFIX)}([^\s\)\"']+)", text):
        assert link in UNPUBLISHED, f"{link} is published — link the site, not GitHub"


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


def test_the_header_matches_the_docs_header() -> None:
    """The top bar is the docs header rebuilt by hand, so nothing but this
    keeps the two from drifting apart: the source box must name the
    repository, mark and release the docs header does. The Docs link is
    the bar's only way into the documentation, standing where the docs
    have their search box.
    """
    import tomllib

    elements = collect_elements(INDEX.read_text())

    def the_one(tag: str, css_class: str) -> dict[str, str | None]:
        found = [a for t, a in elements if t == tag and css_class in (a.get("class") or "").split()]
        assert len(found) == 1, f"expected one <{tag} class={css_class!r}>, found {len(found)}"
        return found[0]

    the_one("header", "topbar")
    header_links = [
        a.get("href")
        for t, a in elements
        if t == "a" and "topbar__link" in (a.get("class") or "").split()
    ]
    assert header_links == ["news/", "docs/"]

    with (REPO_ROOT / "zensical.toml").open("rb") as handle:
        docs = tomllib.load(handle)["project"]
    assert the_one("a", "topbar__source").get("href") == docs["repo_url"]
    start = elements.index(("a", the_one("a", "topbar__brand")))
    brand = next(a for t, a in elements[start:] if t == "img")
    assert brand.get("src") == docs["theme"]["logo"].lstrip("/"), "the mark differs from the docs'"

    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert f'<span class="topbar__version">v{version}</span>' in INDEX.read_text(), (
        f"the header's version is stale — set it to v{version}"
    )


def test_every_workflow_tape_renders_the_same_frame() -> None:
    """Two clips of different sizes make the demo tabs jump on a switch.

    The page shows one clip at a time behind a radio group, and the frame is
    what makes that switch invisible: `common.tape` owns Width, FontSize and
    Padding and no tape may override them, and every tape sets the same
    Height. A recording that does not fit its frame loses a column — it never
    gets a different frame. See
    .local/superpowers/specs/2026-09-11-dashboard-video-design.md, section 6.
    """
    all_tapes = sorted((SITE / "demo" / "workflows").glob("*.tape"))
    tapes = [t for t in all_tapes if t.name != "common.tape"]
    assert tapes, "no workflow tapes found"
    heights = {}
    for tape in tapes:
        text = tape.read_text()
        directives = [ln.strip() for ln in text.splitlines() if ln.startswith("Set ")]
        for owned in ("Set Width", "Set FontSize", "Set Padding"):
            assert not any(d.startswith(owned) for d in directives), (
                f"{tape.name} overrides {owned}, which common.tape owns"
            )
        found = [d for d in directives if d.startswith("Set Height")]
        assert len(found) == 1, f"{tape.name} must set Height exactly once, found {found}"
        heights[tape.name] = found[0].split()[-1]
    assert len(set(heights.values())) == 1, f"tapes disagree about the frame height: {heights}"
