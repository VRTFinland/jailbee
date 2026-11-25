"""Observable row density and state layers for the shared overlay primitives."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    return {**browser_context_args, "is_mobile": True, "has_touch": True}


@pytest.mark.parametrize("kind", ["menu", "select"])
def test_overlay_rows_use_coarse_targets_and_disabled_rows_have_no_state_layer(
    logged_in: Page, kind: str
) -> None:
    page = logged_in
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{page.url.split('#')[0]}#/ui")
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")
    panel = page.get_by_role("region", name="Light theme, normal profile", exact=True)
    if kind == "menu":
        panel.get_by_role("button", name="Container actions", exact=True).click()
        row = page.get_by_role("menuitem", name="Shell", exact=True)
        disabled = page.get_by_role("menuitem", name="Stop", exact=True)
    else:
        panel.get_by_role("combobox", name="Theme", exact=True).click()
        row = page.get_by_role("option", name="Light", exact=True)
        disabled = page.get_by_role("option", name="System", exact=True)
    expect(row).to_be_visible()
    assert row.evaluate("el => el.getBoundingClientRect().height") >= 56
    disabled.hover(force=True)
    assert disabled.evaluate("el => getComputedStyle(el, '::before').opacity") == "0"


def test_select_checked_and_highlighted_rows_are_distinct(logged_in: Page) -> None:
    page = logged_in
    page.goto(f"{page.url.split('#')[0]}#/ui")
    panel = page.get_by_role("region", name="Light theme, normal profile", exact=True)
    control = panel.get_by_role("combobox", name="Theme", exact=True)
    control.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    checked = page.get_by_role("option", name="Light", exact=True)
    highlighted = page.get_by_role("option", name="Dark", exact=True)
    expect(highlighted).to_be_focused()
    assert checked.evaluate("el => getComputedStyle(el).backgroundColor") != highlighted.evaluate(
        "el => getComputedStyle(el).backgroundColor"
    )


def test_menu_shows_a_leading_icon_and_danger_action(logged_in: Page) -> None:
    page = logged_in
    page.goto(f"{page.url.split('#')[0]}#/ui")
    page.get_by_role("region", name="Light theme, normal profile", exact=True).get_by_role(
        "button", name="Container actions", exact=True
    ).click()
    danger = page.get_by_role("menuitem", name="Destroy", exact=True)
    expect(danger).to_be_visible()
    expect(danger.locator("svg")).to_have_count(1)
    assert danger.evaluate("el => getComputedStyle(el).color") == "rgb(186, 26, 26)"
