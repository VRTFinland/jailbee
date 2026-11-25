"""Overlay behaviour and profile inheritance through real browser portals."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.parametrize(
    "theme,background,outline",
    [
        ("Light", "rgb(231, 232, 238)", "rgb(25, 28, 32)"),
        ("Dark", "rgb(40, 42, 47)", "rgb(226, 226, 233)"),
    ],
)
@pytest.mark.parametrize("profile", ["normal", "e-ink"])
def test_portalled_overlays_keep_the_panel_theme_and_profile(
    logged_in: Page, profile: str, theme: str, background: str, outline: str
) -> None:
    page = logged_in
    page.emulate_media(reduced_motion="no-preference")
    page.goto(f"{page.url.split('#')[0]}#/ui")
    # An inner normal panel must override even an e-ink page profile.
    page.locator("html").evaluate("el => el.dataset.profile = 'eink'")
    panel = page.get_by_role("region", name=f"{theme} theme, {profile} profile", exact=True)
    panel.get_by_role("button", name="Open centred dialog", exact=True).click()
    dialog = page.get_by_role("dialog", name="Destroy dev-plan?")
    expect(dialog).to_be_visible()
    styles = dialog.evaluate(
        """el => {
            const s = getComputedStyle(el);
            const scrim = getComputedStyle(document.querySelector('.ui-dialog-scrim'));
            return {background: s.backgroundColor, animation: s.animationDuration,
                border: s.borderTopColor, shadow: s.boxShadow, scrim: scrim.backgroundColor};
        }"""
    )
    assert styles["background"] == background
    box = dialog.bounding_box()
    assert box is not None
    viewport = page.viewport_size
    assert viewport is not None
    assert abs(box["x"] + box["width"] / 2 - viewport["width"] / 2) < 1
    assert abs(box["y"] + box["height"] / 2 - viewport["height"] / 2) < 1
    assert styles["shadow"] == "none"
    if profile == "e-ink":
        assert styles["scrim"] == "rgba(0, 0, 0, 0)"
        assert styles["animation"] == "0s"
        assert styles["border"] == outline
    else:
        assert styles["animation"] == "0.15s"
        assert styles["scrim"] == "rgba(0, 0, 0, 0.32)"
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(panel.get_by_role("button", name="Open centred dialog", exact=True)).to_be_focused()


def test_gallery_overlay_keyboard_and_outside_dismissal(logged_in: Page) -> None:
    page = logged_in
    page.goto(f"{page.url.split('#')[0]}#/ui")
    panel = page.get_by_role("region", name="Light theme, normal profile", exact=True)
    opener = panel.get_by_role("button", name="Open centred dialog", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog", name="Destroy dev-plan?")
    last = dialog.get_by_role("button", name="Destroy", exact=True)
    last.focus()
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="Close dialog")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(last).to_be_focused()
    page.mouse.click(2, 2)
    expect(dialog).not_to_be_visible()
    expect(opener).to_be_focused()
    menu_trigger = panel.get_by_role("button", name="Container actions", exact=True)
    menu_trigger.focus()
    page.keyboard.press("ArrowDown")
    menu = page.get_by_role("menu", name="Container actions", exact=True)
    expect(menu.get_by_role("menuitem", name="Shell", exact=True)).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.get_by_role("menuitem", name="Logs", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(menu).not_to_be_visible()
    select = panel.get_by_role("combobox", name="Theme", exact=True)
    select.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("listbox")).to_be_visible()
    expect(page.get_by_role("option", name="Light", exact=True)).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("option", name="Dark", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(select).to_have_text("Dark")
    expect(select).to_be_focused()
