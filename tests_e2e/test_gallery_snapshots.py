"""Visual regression checks for every gallery theme/profile at two widths."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.image_snapshots import assert_image_matches


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, object], width: int) -> dict[str, object]:
    return {
        **browser_context_args,
        "device_scale_factor": 1,
        "locale": "en-US",
        "has_touch": width == 390,
        "is_mobile": width == 390,
    }


@pytest.mark.parametrize("width", [390, 1280], ids=["phone", "desktop"])
@pytest.mark.parametrize("theme", ["Light", "Dark", "E-ink"])
@pytest.mark.parametrize("profile", ["normal", "e-ink"])
def test_gallery_snapshot(
    logged_in: Page,
    width: int,
    theme: str,
    profile: str,
    output_path: str,
    pytestconfig: pytest.Config,
) -> None:
    page = logged_in
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(reduced_motion="reduce", color_scheme="light")
    page.goto(f"{page.url.split('#')[0]}#/ui")
    expect(page.get_by_role("heading", name="Design system", exact=True)).to_be_visible()
    page.evaluate("document.fonts.ready")
    # Exclude the fixed reading guard, which otherwise obscures a stripe of
    # a tall panel. Use CSSOM properties: a screenshot stylesheet violates CSP.
    page.locator(".bottom-guard").evaluate("el => el.style.visibility = 'hidden'")
    # Token labels use the platform monospace stack. Pin its Linux face so CI
    # and the container measure the same labels (the UI samples still use Inter).
    page.locator("html").evaluate(
        "el => el.style.setProperty('--md-font-mono', '\"DejaVu Sans Mono\"')"
    )
    panel = page.get_by_role("region", name=f"{theme} theme, {profile} profile", exact=True)
    assert_image_matches(
        panel.screenshot(
            animations="disabled",
            caret="hide",
            scale="css",
        ),
        reference=Path(__file__).parent / "snapshots" / f"{width}-{theme.lower()}-{profile}.png",
        output=Path(output_path),
        update=bool(pytestconfig.getoption("--update-gallery-snapshots")),
    )
