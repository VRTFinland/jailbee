"""The design system foundation (#167): the /ui gallery, the bundled font and motion."""

from __future__ import annotations

from urllib.parse import urlsplit

from playwright.sync_api import Page, expect

from tests_e2e.flows import open_settings
from tests_e2e.server import E2EServer

PANELS = [
    f"{theme} theme, {profile} profile"
    for theme in ("Light", "Dark", "E-ink")
    for profile in ("normal", "e-ink")
]

_MAX_TRANSITION_MS = """
() => {
    const ms = (v) => Math.max(0, ...v.split(",").map((part) => {
        const s = part.trim()
        return s.endsWith("ms") ? parseFloat(s) : parseFloat(s) * 1000
    }))
    let max = 0
    for (const el of document.querySelectorAll("*")) {
        for (const pseudo of [null, "::before", "::after"]) {
            const style = getComputedStyle(el, pseudo)
            max = Math.max(max, ms(style.transitionDuration), ms(style.animationDuration))
        }
    }
    return max
}
"""


def _open_gallery(page: Page) -> None:
    page.goto(f"{page.url.split('#')[0]}#/ui")
    expect(page.get_by_role("heading", name="Design system", exact=True)).to_be_visible()


def test_gallery_shows_every_theme_and_profile_after_login(logged_in: Page) -> None:
    page = logged_in
    _open_gallery(page)
    for name in PANELS:
        panel = page.get_by_role("region", name=name, exact=True)
        expect(panel.get_by_role("heading", name=name, exact=True)).to_be_visible()
        expect(panel.get_by_text("--md-primary-container", exact=True)).to_be_visible()
        expect(panel.get_by_text("Title large", exact=False)).to_be_visible()
    # Each panel scopes its own palette, whatever the page's theme.
    expect(page.get_by_role("region", name="Light theme, normal profile")).to_have_css(
        "background-color", "rgb(249, 249, 255)"
    )
    expect(page.get_by_role("region", name="Dark theme, normal profile")).to_have_css(
        "background-color", "rgb(17, 19, 24)"
    )
    expect(page.get_by_role("region", name="E-ink theme, normal profile")).to_have_css(
        "background-color", "rgb(255, 255, 255)"
    )


def test_gallery_is_not_reachable_without_login(server: E2EServer, page: Page) -> None:
    page.goto(f"{server.url}#/ui")
    expect(page.get_by_label("PIN")).to_be_visible()
    expect(page.get_by_role("heading", name="Design system", exact=True)).to_have_count(0)


def test_inter_is_served_by_the_app_and_no_other_host_is_contacted(
    server: E2EServer, page: Page
) -> None:
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(server.url)
    page.get_by_label("PIN").fill(server.pin.current)
    page.get_by_role("button", name="Log in").click()
    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    _open_gallery(page)
    page.evaluate("document.fonts.ready")
    assert page.evaluate("document.fonts.check('16px Inter')")
    assert page.evaluate(
        "[...document.fonts].some((f) => f.family.replace(/\"/g, '') === 'Inter'"
        " && f.status === 'loaded')"
    )
    assert page.evaluate("getComputedStyle(document.body).fontFamily").startswith(
        ('"Inter"', "Inter")
    )

    origin = urlsplit(server.url).netloc
    fonts = [url for url in requests if url.endswith(".woff2")]
    assert fonts, requests
    foreign = [
        url
        for url in requests
        if urlsplit(url).scheme in {"http", "https", "ws", "wss"} and urlsplit(url).netloc != origin
    ]
    assert foreign == []


def test_transitions_stay_brief_and_stop_under_reduced_motion_and_eink(logged_in: Page) -> None:
    page = logged_in
    _open_gallery(page)
    longest = page.evaluate(_MAX_TRANSITION_MS)
    assert 0 < longest <= 150

    page.emulate_media(reduced_motion="reduce")
    assert page.evaluate(_MAX_TRANSITION_MS) == 0
    page.emulate_media(reduced_motion="no-preference")

    open_settings(page)
    page.get_by_role("group", name="Display profile", exact=True).get_by_role(
        "radio", name="E-ink", exact=False
    ).check()
    _open_gallery(page)
    assert page.evaluate(_MAX_TRANSITION_MS) == 0
