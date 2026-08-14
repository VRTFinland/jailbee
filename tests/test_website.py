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


def test_every_local_reference_resolves_on_disk() -> None:
    refs = collect_tagged_references(INDEX.read_text())
    local = [
        (tag, attr, v)
        for tag, attr, v in refs
        if not v.startswith(("http://", "https://", "#", "mailto:"))
    ]
    assert local, "the page references no local assets at all"
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
        assert target.is_file(), f"broken local reference: {value}"


def test_only_anchors_use_absolute_urls() -> None:
    """No CDN, no webfont service, no third-party image — ever."""
    html = INDEX.read_text()
    refs = collect_tagged_references(html)
    for tag, attr, value in refs:
        if value.startswith(("http://", "https://")):
            assert tag == "a" and attr == "href", (
                f"non-anchor absolute URL: <{tag} {attr}={value!r}>"
            )


def test_the_stylesheet_makes_no_external_requests() -> None:
    css = (SITE / "assets" / "style.css").read_text()
    assert "http://" not in css
    assert "https://" not in css


def test_the_page_has_exactly_one_top_level_heading() -> None:
    assert INDEX.read_text().count("<h1") == 1


DOC_LINK_PREFIX = "https://github.com/VRTFinland/jailbee/blob/main/"


def test_documentation_links_point_at_files_that_exist_in_this_repo() -> None:
    """The public repo does not exist yet, so verify against the local tree."""
    refs = collect_references(INDEX.read_text())
    doc_links = [v for a, v in refs if a == "href" and v.startswith(DOC_LINK_PREFIX)]
    assert len(doc_links) >= 6, "the docs section should link at least six documents"
    for link in doc_links:
        path = REPO_ROOT / link[len(DOC_LINK_PREFIX) :]
        assert path.is_file(), f"documentation link has no local counterpart: {link}"


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
