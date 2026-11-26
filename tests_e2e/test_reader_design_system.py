"""Reader content composes the same tonal primitives as the gallery."""

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import TITLE, open_transcript


@pytest.mark.parametrize("theme", ["light", "dark", "eink"])
def test_reader_prompt_uses_primary_container_and_jump_uses_shared_button(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    theme: str,
    output_path: str,
) -> None:
    page.set_viewport_size({"width": 390, "height": 800})
    server, _ = open_transcript(page, make_server, tmp_path)
    page.evaluate("theme => localStorage.setItem('jailbee.theme', theme)", theme)
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    prompt = page.locator(".entry.user").first
    assert prompt.evaluate("""el => {
        const style = getComputedStyle(el);
        const probe = document.createElement('span');
        probe.style.background = 'var(--md-primary-container)';
        el.append(probe);
        const expected = getComputedStyle(probe).backgroundColor;
        probe.remove();
        return style.backgroundColor === expected;
    }""")
    jump = page.locator(".jump-latest")
    expect(jump).to_have_attribute("data-variant", "primary")
    expect(jump.locator("svg[aria-hidden='true']")).to_have_count(1)
    evidence = Path(output_path)
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(evidence / f"reader-{theme}.png"))
    page.keyboard.press("j")
    panel = page.get_by_role("dialog", name="Jump to", exact=True)
    excerpt = panel.get_by_role("button", name=TITLE, exact=True)
    box = excerpt.bounding_box()
    assert box is not None and box["height"] == 44
    expect(excerpt.locator(".button-label")).to_have_css("white-space", "nowrap")
    page.keyboard.press("Escape")
    page.goto(f"{server.url}/#/ui")
    expect(page.get_by_role("heading", name="Design system", exact=True)).to_be_visible()
    panel = page.get_by_role(
        "region",
        name=f"{'E-ink' if theme == 'eink' else theme.title()} theme, normal profile",
        exact=True,
    )
    panel.locator(".group").filter(
        has=page.get_by_role("heading", name="Reader", exact=True)
    ).screenshot(path=str(evidence / f"gallery-reader-{theme}.png"))
