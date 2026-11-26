"""The design system foundation (#167): the /ui gallery, the bundled font and motion."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from playwright.sync_api import Locator, Page, Position, expect

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


# Every transition and animation time declared in any of the app's stylesheets
# (the build ships one bundle, so this covers every view, not only the page on
# screen), with the motion tokens resolved against the root.
_DECLARED_MOTION_MS = r"""
() => {
    const root = getComputedStyle(document.documentElement)
    const sub = (v) => v.replace(/var\((--[\w-]+)\)/g, (_m, n) => root.getPropertyValue(n).trim())
    const found = []
    const walk = (rules) => {
        for (const rule of rules) {
            if (rule.cssRules) walk(rule.cssRules)
            if (!rule.style) continue
            for (const prop of ["transition", "transition-duration", "transition-delay",
                                "animation", "animation-duration", "animation-delay"]) {
                const value = rule.style.getPropertyValue(prop)
                if (!value) continue
                for (const m of sub(value).matchAll(/(?<![\w.-])(\d*\.?\d+)(ms|s)\b/g)) {
                    found.push([rule.selectorText || rule.cssText.slice(0, 60),
                                m[2] === "s" ? Number(m[1]) * 1000 : Number(m[1])])
                }
            }
        }
    }
    for (const sheet of document.styleSheets) walk(sheet.cssRules)
    return found
}
"""

# What each gallery panel's sample shape shows: its transition (the display
# profile) and its hairline border (the theme).
_PANEL_LOOK = """
() => Object.fromEntries(
    [...document.querySelectorAll("section[data-theme][data-profile]")].map((p) => {
        const s = getComputedStyle(p.querySelector(".shape"))
        return [p.getAttribute("aria-label"), [s.transitionDuration, s.borderTopColor]]
    })
)
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


# Every icon's painted fill and the text colour of the element it sits in.
_ICON_INK = """
(panel) => [...panel.querySelectorAll("svg")].map((svg) => [
    getComputedStyle(svg.querySelector("path")).fill,
    getComputedStyle(svg.parentElement).color,
])
"""


def test_bundled_icons_render_at_token_sizes_and_follow_the_text_colour(logged_in: Page) -> None:
    page = logged_in
    _open_gallery(page)
    for name in PANELS:
        panel = page.get_by_role("region", name=name, exact=True)
        icons = panel.get_by_role("img")
        expect(icons).to_have_count(26)
        for icon in icons.all():
            expect(icon).to_be_visible()
            # A real vector with painted geometry, not a missing font glyph or external image.
            assert icon.locator("path").evaluate("el => el.getBBox().width > 0")
        assert icons.first.evaluate("el => el.getBoundingClientRect().width") == 24
        assert icons.first.evaluate("el => el.getBoundingClientRect().height") == 24
        # Colour belongs to the theme (ADR 0007): an icon is drawn in its
        # surroundings' text colour, light on a dark e-ink-profile panel and on
        # a primary button, black in the e-ink theme because its text is.
        ink = panel.evaluate(_ICON_INK)
        assert len(ink) > 30
        assert [fill for fill, _ in ink] == [colour for _, colour in ink], name
        if "E-ink theme" in name:
            assert {fill for fill, _ in ink} <= {"rgb(0, 0, 0)", "rgb(255, 255, 255)"}, name
        if "Dark theme" in name:
            assert "rgb(0, 0, 0)" not in {fill for fill, _ in ink}, name

    sizes = page.get_by_role("region", name="Light theme, normal profile").get_by_label(
        "Icon sizes", exact=True
    )
    assert sizes.locator("svg").evaluate_all(
        "els => els.map(el => el.getBoundingClientRect().width)"
    ) == [16, 24, 32]
    assert sizes.locator("svg").evaluate_all(
        "els => els.map(el => el.getBoundingClientRect().height)"
    ) == [16, 24, 32]

    license_link = page.get_by_role("link", name="Material Symbols licence", exact=True).first
    response = page.request.get(license_link.get_attribute("href") or "")
    assert response.ok
    assert "Apache License" in response.text()
    assert "Version 2.0, January 2004" in response.text()


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


def test_gallery_panels_follow_their_own_theme_and_profile_not_the_page(logged_in: Page) -> None:
    """A panel shows its own theme and profile whatever the page's: the e-ink
    profile stops motion and the e-ink theme draws hairlines, and neither leaks
    from a root e-ink page into a normal or light panel."""
    page = logged_in
    _open_gallery(page)
    expected = {
        f"{theme} theme, {profile} profile": [
            "0s" if profile == "e-ink" else "0.15s",
            "rgb(0, 0, 0)" if theme == "E-ink" else "rgba(0, 0, 0, 0)",
        ]
        for theme in ("Light", "Dark", "E-ink")
        for profile in ("normal", "e-ink")
    }
    for root_theme, root_profile in (("light", "normal"), ("eink", "eink"), ("dark", "eink")):
        page.evaluate(
            "([t, p]) => { document.documentElement.dataset.theme = t;"
            " document.documentElement.dataset.profile = p }",
            [root_theme, root_profile],
        )
        assert page.evaluate(_PANEL_LOOK) == expected, (root_theme, root_profile)


def test_transitions_stay_brief_and_stop_under_reduced_motion_and_eink(logged_in: Page) -> None:
    page = logged_in
    _open_gallery(page)
    declared = page.evaluate(_DECLARED_MOTION_MS)
    assert declared, "no stylesheet declares any motion: the scan found nothing"
    # Sheet travel and its scrim use the explicitly longer M3 entry duration;
    # ordinary state transitions retain their 150ms ceiling.
    sheet = [(where, ms) for where, ms in declared if "drawer" in where]
    assert sheet and all(250 <= ms <= 300 for _, ms in sheet)
    too_long = [(where, ms) for where, ms in declared if "drawer" not in where and ms > 150]
    assert too_long == []
    longest = page.evaluate(_MAX_TRANSITION_MS)
    assert 0 < longest <= 150

    page.emulate_media(reduced_motion="reduce")
    assert page.evaluate(_MAX_TRANSITION_MS) == 0
    page.emulate_media(reduced_motion="no-preference")

    open_settings(page)
    page.get_by_role("group", name="Display profile", exact=True).get_by_role(
        "radio", name="E-ink", exact=False
    ).check()
    # The app's own page: the gallery's normal-profile panels keep their motion
    # on an e-ink page by design (see the panel test above).
    page.goto(f"{page.url.split('#')[0]}#/")
    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    assert page.evaluate(_MAX_TRANSITION_MS) == 0
    _open_gallery(page)
    eink_panels = page.locator('section[data-profile="eink"] .shape')  # 3 panels x 6 shapes
    assert (
        eink_panels.evaluate_all("(els) => els.map((e) => getComputedStyle(e).transitionDuration)")
        == ["0s"] * 18
    )


def test_the_inter_licence_ships_next_to_the_font(server: E2EServer, page: Page) -> None:
    """The OFL requires the licence to travel with the font: the build puts it
    beside the bundled woff2 in the app's assets, which the wheel carries."""
    response = page.request.get(f"{server.url.split('#')[0].rstrip('/')}/assets/Inter-OFL.txt")
    assert response.ok, response.status
    text = response.text()
    assert "SIL Open Font License, Version 1.1" in text
    assert "The Inter Project Authors" in text


_PRIMITIVES = """
(panel) => [...panel.querySelectorAll(
    ".button, .icon-button, [role=switch], .status-chip, .list-row"
)].map((el) => getComputedStyle(el).boxShadow)
"""

_LAYER_OPACITY = "(el) => getComputedStyle(el, '::before').opacity"


def test_primitives_draw_material_states_from_tokens(logged_in: Page) -> None:
    """The static primitives (#167 ticket 05) in the gallery: token colours, a
    44px icon button, Material 3 state layers (8 % hover, 10 % pressed) and a
    visible focus ring, no shadows, and black-on-white e-ink rules, each panel
    by its own theme whatever the page's."""
    page = logged_in
    _open_gallery(page)
    for root_theme, root_profile in (("light", "normal"), ("eink", "eink")):
        page.evaluate(
            "([t, p]) => { document.documentElement.dataset.theme = t;"
            " document.documentElement.dataset.profile = p }",
            [root_theme, root_profile],
        )
        light = page.get_by_role("region", name="Light theme, normal profile", exact=True)
        eink = page.get_by_role("region", name="E-ink theme, e-ink profile", exact=True)

        for panel in (light, eink):
            shadows = panel.evaluate(_PRIMITIVES)
            assert len(shadows) > 30
            assert set(shadows) == {"none"}
            menu = panel.get_by_role("button", name="Menu", exact=True)
            assert menu.evaluate("el => el.getBoundingClientRect().width") == 44
            assert menu.evaluate("el => el.getBoundingClientRect().height") == 44
            row = panel.get_by_role("listitem").filter(has_text="fix-issue-165")
            assert row.evaluate("el => el.getBoundingClientRect().height") >= 48

        primary = light.get_by_role("button", name="primary", exact=True)
        expect(primary).to_have_css("background-color", "rgb(58, 95, 148)")
        expect(primary).to_have_css("border-top-left-radius", "9999px")
        tonal = light.get_by_role("button", name="tonal", exact=True)
        expect(tonal).to_have_css("background-color", "rgb(213, 227, 255)")
        tonal.hover()
        page.wait_for_function(
            "el => getComputedStyle(el, '::before').opacity === '0.08'", arg=tonal.element_handle()
        )
        page.mouse.down()
        page.wait_for_function(
            "el => getComputedStyle(el, '::before').opacity === '0.1'", arg=tonal.element_handle()
        )
        page.mouse.up()

        # Keyboard focus draws the ring; the disabled primary is skipped.
        primary.focus()
        page.keyboard.press("Tab")
        expect(tonal).to_be_focused()
        expect(tonal).to_have_css("outline-style", "solid")
        expect(tonal).to_have_css("outline-width", "2px")
        expect(tonal).to_have_css("outline-color", "rgb(58, 95, 148)")
        page.mouse.move(0, 0)

        selected = light.locator('li[aria-current="true"]')
        expect(selected).to_have_css("background-color", "rgb(213, 227, 255)")
        expect(selected).to_have_css("outline-color", "rgba(0, 0, 0, 0)")

        # E-ink: no tints, no greys, hairlines and outlines instead.
        eink_tonal = eink.get_by_role("button", name="tonal", exact=True)
        eink_tonal.hover()
        page.wait_for_timeout(200)
        assert eink_tonal.evaluate(_LAYER_OPACITY) == "0"
        expect(eink_tonal).to_have_css("border-top-color", "rgb(0, 0, 0)")
        disabled = eink.get_by_role("button", name="tonal disabled", exact=True)
        expect(disabled).to_have_css("color", "rgb(0, 0, 0)")
        expect(disabled).to_have_css("border-top-style", "dashed")
        eink_selected = eink.locator('li[aria-current="true"]')
        expect(eink_selected).to_have_css("background-color", "rgb(255, 255, 255)")
        expect(eink_selected).to_have_css("outline-color", "rgb(0, 0, 0)")
        expect(eink_selected).to_have_css("outline-width", "2px")
        for chip in eink.locator(".status-chip").all():
            expect(chip).to_have_css("border-top-color", "rgb(0, 0, 0)")
            expect(chip).to_have_css("color", "rgb(0, 0, 0)")
            expect(chip).to_have_css("background-color", "rgb(255, 255, 255)")
        page.mouse.move(0, 0)


def test_context_rows_and_emphasized_chips_are_primitive_variants(logged_in: Page) -> None:
    page = logged_in
    _open_gallery(page)
    for theme, opacity, border in (
        ("Light", "0.5", "solid"),
        ("Dark", "0.5", "solid"),
        ("E-ink", "1", "dashed"),
    ):
        panel = page.get_by_role("region", name=f"{theme} theme, normal profile", exact=True)
        row = panel.get_by_role("listitem").filter(
            has_text="A long container name that wraps instead of truncating"
        )
        expect(row).to_have_css("opacity", opacity)
        expect(row).to_have_css("border-top-style", border)
        if theme == "E-ink":
            expect(row).to_have_css("border-top-color", "rgb(0, 0, 0)")
        chip = panel.get_by_text("idle 5m", exact=True)
        expect(chip).to_have_css("font-weight", "700")


def test_gallery_switch_toggles_by_pointer_and_keyboard(logged_in: Page) -> None:
    page = logged_in
    _open_gallery(page)
    panel = page.get_by_role("region", name="Dark theme, normal profile", exact=True)
    follow = panel.get_by_role("switch", name="Follow live", exact=True)
    expect(follow).to_have_attribute("aria-checked", "false")
    follow.click()
    expect(follow).to_have_attribute("aria-checked", "true")
    track = follow.locator(".switch-track")
    expect(track).to_have_css("background-color", "rgb(167, 200, 255)")
    follow.press("Space")
    expect(follow).to_have_attribute("aria-checked", "false")
    # Other panels keep their own sample.
    other = page.get_by_role("region", name="Light theme, normal profile", exact=True)
    expect(other.get_by_role("switch", name="Follow live", exact=True)).to_have_attribute(
        "aria-checked", "false"
    )
    disabled = panel.get_by_role("switch", name="On, disabled", exact=True)
    expect(disabled).to_be_disabled()
    # Keyboard focus rings the switch and the icon button too.
    expect(follow).to_be_focused()
    expect(track).to_have_css("outline-style", "solid")
    expect(track).to_have_css("outline-width", "2px")
    menu = panel.get_by_role("button", name="Actions for feat-reader-chrome", exact=True)
    menu.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(menu).to_be_focused()
    expect(menu).to_have_css("outline-style", "solid")
    # The 32px track sits in a 44px hit target.
    assert follow.evaluate("el => el.getBoundingClientRect().height") == 44
    assert track.evaluate("el => el.getBoundingClientRect().height") == 32


def _linked_row(page: Page, title: str) -> tuple[Locator, Locator]:
    """The light panel's ListRow linked by `title`, and its row link."""
    panel = page.get_by_role("region", name="Light theme, normal profile", exact=True)
    inner = page.locator("a.list-row-link").filter(has_text=re.compile(f"^{title}$"))
    row = panel.locator("li.list-row", has=inner)
    return row, row.locator("a.list-row-link")


def _over_secondary(row: Locator) -> Position:
    """A point of `row` over its secondary line, clear of the title."""
    box, line = row.bounding_box(), row.locator(".list-row-secondary").bounding_box()
    assert box and line
    return {"x": line["x"] - box["x"] + 4, "y": line["y"] - box["y"] + line["height"] / 2}


def test_linked_list_row_opens_from_anywhere_with_layer_and_focus_ring(logged_in: Page) -> None:
    """A ListRow with `to` (#167 ticket 05b): the whole row opens its route,
    hover draws the state layer over the row, keyboard focus rings the row,
    and links in its body keep their own target."""
    page = logged_in
    _open_gallery(page)
    row, link = _linked_row(page, "dev-ork")
    expect(row.locator(".list-row-body")).to_contain_text("Orchestrator · 2 children")
    assert row.evaluate("el => el.getBoundingClientRect().height") >= 48
    expect(link).to_have_attribute("href", re.compile(r"#/ui\?row=dev-ork$"))

    row.hover(position=_over_secondary(row))
    page.wait_for_function(
        "el => getComputedStyle(el, '::before').opacity === '0.08'", arg=row.element_handle()
    )
    # A trailing control is its own target: hovering it does not light the
    # row, clicking it does not open the row; a chip beside it is part of the row.
    actions = row.get_by_role("button", name="Actions for dev-ork", exact=True)
    actions.hover()
    page.wait_for_timeout(200)
    assert row.evaluate(_LAYER_OPACITY) == "0"
    actions.click()
    expect(page).to_have_url(re.compile(r"#/ui$"))
    row.locator(".status-chip").first.click(force=True)
    expect(page).to_have_url(re.compile(r"#/ui\?row=dev-ork$"))
    _open_gallery(page)
    row, link = _linked_row(page, "dev-ork")

    # A click on the secondary line, not the title, still opens the route (the
    # gallery's sample rows link back to it: their names are no real containers).
    row.click(position=_over_secondary(row))
    expect(page).to_have_url(re.compile(r"#/ui\?row=dev-ork$"))

    _open_gallery(page)
    row, link = _linked_row(page, "dev-ork")
    link.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(link).to_be_focused()
    expect(row).to_have_css("outline-style", "solid")
    expect(row).to_have_css("outline-color", "rgb(58, 95, 148)")

    child_row, child_link = _linked_row(page, "dev-ork-1")
    expect(child_link).to_have_attribute("aria-current", "true")
    expect(child_row).not_to_have_attribute("aria-current", "true")
    # The body's own link opens its target, not the row's.
    child_row.locator(".list-row-body").get_by_role("link", name="dev-ork", exact=True).click()
    expect(page).to_have_url(re.compile(r"#/ui\?row=dev-ork$"))
    _open_gallery(page)
    child_row, _ = _linked_row(page, "dev-ork-1")
    child_row.click(position=_over_secondary(child_row))
    expect(page).to_have_url(re.compile(r"#/ui\?row=dev-ork-1$"))


def test_disabled_switch_draws_no_hover_or_pressed_halo(logged_in: Page) -> None:
    page = logged_in
    _open_gallery(page)
    panel = page.get_by_role("region", name="Light theme, normal profile", exact=True)
    disabled = panel.get_by_role("switch", name="On, disabled", exact=True)
    handle = disabled.locator(".switch-handle")
    disabled.hover()
    page.wait_for_timeout(200)
    assert handle.evaluate(_LAYER_OPACITY) == "0"
    page.mouse.down()
    page.wait_for_timeout(200)
    assert handle.evaluate(_LAYER_OPACITY) == "0"
    page.mouse.up()
    # The enabled switch still draws its halo.
    enabled = panel.get_by_role("switch", name="On", exact=True)
    enabled.hover()
    page.wait_for_function(
        "el => getComputedStyle(el, '::before').opacity === '0.08'",
        arg=enabled.locator(".switch-handle").element_handle(),
    )
