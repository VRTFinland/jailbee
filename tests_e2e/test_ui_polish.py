"""User-tested chrome geometry and motion through the rendered dashboard."""

from typing import Literal

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.flows import open_quick_settings, open_settings_overlay


@pytest.mark.parametrize("width", [360, 1280])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_title_bar_spans_the_viewport(logged_in: Page, width: int, profile: str) -> None:
    page = logged_in
    page.set_viewport_size({"width": width, "height": 800})
    page.evaluate("profile => localStorage.setItem('jailbee.displayProfile', profile)", profile)
    page.reload()
    bar = page.locator("header").filter(has=page.get_by_role("navigation", name="Breadcrumb"))
    expect(bar).to_be_visible()
    box = bar.bounding_box()
    assert box is not None
    assert box["x"] == 0
    assert box["width"] == width
    assert page.evaluate("document.documentElement.scrollWidth === innerWidth")


@pytest.mark.parametrize("theme,profile", [("light", "normal"), ("dark", "normal"), ("eink", "normal"), ("light", "eink")])
def test_preferences_have_menu_surface_and_spaced_rows(logged_in: Page, theme: str, profile: str) -> None:
    page = logged_in
    page.set_viewport_size({"width": 360, "height": 800})
    page.evaluate("([theme, profile]) => { localStorage.setItem('jailbee.theme', theme); localStorage.setItem('jailbee.displayProfile', profile) }", [theme, profile])
    page.reload()
    trigger = page.get_by_role("button", name="Quick settings", exact=True)
    trigger_box = trigger.bounding_box()
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    box = panel.bounding_box()
    assert box and trigger_box
    assert abs(box["x"] + box["width"] - trigger_box["x"] - trigger_box["width"]) < 1
    assert 0 <= box["y"] - trigger_box["y"] - trigger_box["height"] <= 8
    assert panel.evaluate("el => parseFloat(getComputedStyle(el).borderTopLeftRadius)") <= 8
    if theme == "eink" or profile == "eink":
        expect(panel).to_have_css("box-shadow", "none")
        expect(panel).to_have_css("border-top-width", "1px")
        assert panel.evaluate("el => getComputedStyle(el).borderTopColor") != "rgba(0, 0, 0, 0)"
    else:
        assert panel.evaluate("el => getComputedStyle(el).boxShadow") != "none"
    for row in panel.locator(".menu-row:visible").all():
        assert row.evaluate("el => el.getBoundingClientRect().height >= parseFloat(getComputedStyle(el).getPropertyValue('--md-target-row'))")


@pytest.mark.parametrize("profile,motion", [("normal", "no-preference"), ("eink", "no-preference"), ("normal", "reduce")])
def test_drawer_has_side_sheet_shape_and_respects_motion(logged_in: Page, profile: str, motion: Literal["no-preference", "reduce"]) -> None:
    page = logged_in
    page.set_viewport_size({"width": 1280, "height": 800})
    page.emulate_media(reduced_motion=motion)
    page.evaluate("profile => localStorage.setItem('jailbee.displayProfile', profile)", profile)
    page.reload()
    open_settings_overlay(page)
    drawer = page.get_by_role("dialog", name="Settings", exact=True)
    assert drawer.evaluate("el => parseFloat(getComputedStyle(el).borderTopLeftRadius)") <= 16
    expect(drawer).to_have_css("border-top-right-radius", "0px")
    duration = drawer.evaluate("el => parseFloat(getComputedStyle(el).animationDuration)")
    scrim = page.locator(".ui-dialog-scrim")
    if profile == "eink" or motion == "reduce":
        assert duration == 0
        expect(scrim).to_have_css("animation-duration", "0s")
    else:
        assert 0.25 <= duration <= 0.3
        assert scrim.evaluate("el => parseFloat(getComputedStyle(el).animationDuration)") > 0
        # Observe actual travel, not merely a nonzero duration or an animation name.
        assert drawer.evaluate("""el => {
            const animation = el.getAnimations()[0];
            animation.pause(); animation.currentTime = 0;
            const start = el.getBoundingClientRect().left;
            animation.currentTime = animation.effect.getTiming().duration;
            return start > el.getBoundingClientRect().left;
        }""")

