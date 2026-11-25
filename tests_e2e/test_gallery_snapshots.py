"""Visual regression checks for every gallery group, theme and profile at two widths."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

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
    # One reference per gallery group, so a single recoloured chip or icon is
    # a large share of its image and a design change rewrites only its group.
    groups = panel.locator(".group")
    titles = groups.locator(".group-title").all_inner_texts()
    assert len(titles) >= 10 and len(set(titles)) == len(titles), titles
    references = Path(__file__).parent / "snapshots"
    stem = f"{width}-{theme.lower()}-{profile}"
    update = bool(pytestconfig.getoption("--update-gallery-snapshots"))
    failures: list[str] = []
    expected: set[Path] = set()
    for index, title in enumerate(titles):
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        reference = references / f"{stem}-{slug}.png"
        expected.add(reference)
        try:
            assert_image_matches(
                groups.nth(index).screenshot(animations="disabled", caret="hide", scale="css"),
                reference=reference,
                output=Path(output_path) / slug,
                update=update,
            )
        except AssertionError as error:
            failures.append(str(error))
    # Open real portalled primitives rather than imitating them in gallery markup.
    overlays: list[tuple[str, str, Literal["dialog", "menu", "listbox"]]] = [
        ("dialog", "Open centred dialog", "dialog"),
        ("drawer", "Open drawer", "dialog"),
        ("menu", "Container actions", "menu"),
        ("select", "Theme", "listbox"),
    ]
    for name, trigger, role in overlays:
        control = panel.get_by_role(
            "combobox" if name == "select" else "button", name=trigger, exact=True
        )
        control.click()
        overlay = page.get_by_role(role)
        expect(overlay).to_be_visible()
        if role == "dialog":
            box = overlay.bounding_box()
            assert box is not None
            if width == 390:
                assert abs(box["y"] + box["height"] - 844) < 2
                assert abs(box["width"] - width) < 2
            elif name == "drawer":
                assert abs(box["x"] + box["width"] - width) < 2
                assert abs(box["height"] - 844) < 2
            else:
                assert abs(box["x"] + box["width"] / 2 - width / 2) < 2
                assert abs(box["y"] + box["height"] / 2 - 844 / 2) < 2
        reference = references / f"{stem}-open-{name}.png"
        expected.add(reference)
        try:
            assert_image_matches(
                overlay.screenshot(animations="disabled", caret="hide", scale="css"),
                reference=reference,
                output=Path(output_path) / f"open-{name}",
                update=update,
            )
        except AssertionError as error:
            failures.append(str(error))
        page.keyboard.press("Escape")
        expect(overlay).not_to_be_visible()
    # A reference of a removed or renamed group is dead weight in the repo.
    for orphan in sorted(set(references.glob(f"{stem}-*.png")) - expected):
        if update:
            orphan.unlink()
        else:
            failures.append(f"No gallery group for reference {orphan.name}; delete it.")
    assert not failures, "\n".join(failures)
