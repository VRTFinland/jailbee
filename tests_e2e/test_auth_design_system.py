"""Login and setup use the gallery's form controls, without view overrides."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer


def appearance(control: Locator) -> dict[str, Any]:
    # Compare intrinsic appearance, not the layout widths owned by the view.
    return dict(
        control.evaluate("""element => {
        const style = getComputedStyle(element);
        return Object.fromEntries([
            'backgroundColor', 'color', 'borderTopColor', 'borderTopWidth',
            'borderTopStyle', 'borderRadius', 'fontFamily', 'fontSize',
            'fontWeight', 'lineHeight', 'minHeight', 'padding', 'boxShadow'
        ].map(name => [name, style[name]]));
    }""")
    )


@pytest.mark.parametrize("view", ["login", "setup"])
@pytest.mark.parametrize("theme", ["light", "dark", "eink"])
@pytest.mark.parametrize("width", [390, 1280])
def test_auth_controls_match_the_gallery(
    make_server: Callable[..., E2EServer],
    page: Page,
    browser: Browser,
    tmp_path: Path,
    view: str,
    theme: str,
    width: int,
) -> None:
    server = make_server(setup=view == "setup")
    page.set_viewport_size({"width": width, "height": 900})
    if view == "login":
        # Exercise both code tabs and the independent security-key action.
        page.route(
            "**/api/auth/methods",
            lambda route: route.fulfill(json={"methods": ["pin", "totp", "fido2"]}),
        )
    page.goto(server.url)
    page.locator("html").evaluate("(element, theme) => element.dataset.theme = theme", theme)
    secret = page.get_by_label("Setup PIN" if view == "setup" else "PIN", exact=True)
    secret.fill("123456")
    submit = page.get_by_role(
        "button", name="Register key" if view == "setup" else "Log in", exact=True
    )
    expect(submit).to_be_enabled()
    secret.blur()
    page.mouse.move(0, 0)

    reference_server = make_server()
    reference = browser.new_page(viewport={"width": width, "height": 900})
    try:
        log_in_with_pin(reference, reference_server.url, reference_server.pin.current)
        reference.goto(reference_server.url + "/#/ui")
        panel = reference.get_by_role(
            "region",
            name=f"{'E-ink' if theme == 'eink' else theme.title()} theme, normal profile",
            exact=True,
        )
        field = panel.get_by_label("Container name", exact=True)
        expect(field).to_be_visible()
        assert appearance(secret) == appearance(field)
        assert appearance(submit) == appearance(
            panel.get_by_role("button", name="primary", exact=True)
        )
        if view == "setup":
            assert appearance(page.get_by_label("Key name")) == appearance(field)
        else:
            assert appearance(page.get_by_role("tab", name="PIN", exact=True)) == appearance(submit)
            tonal = appearance(panel.get_by_role("button", name="tonal", exact=True))
            assert appearance(page.get_by_role("tab", name="Authenticator code")) == tonal
            assert appearance(page.get_by_role("button", name="Use security key")) == tonal
            page.get_by_role("tab", name="Authenticator code").click()
            expect(page.get_by_label("Code", exact=True)).to_be_visible()
            assert appearance(page.get_by_label("Code", exact=True)) == appearance(field)
            page.get_by_role("tab", name="PIN", exact=True).click()
        for control in page.locator("main input, main button").all():
            bounds = control.bounding_box()
            assert bounds is not None and bounds["height"] >= 44
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=tmp_path / f"{view}-{theme}-{width}.png", full_page=True)
        panel.screenshot(path=tmp_path / f"gallery-{theme}-{width}.png")
    finally:
        reference.close()
