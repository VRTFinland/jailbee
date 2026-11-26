"""Reader content composes the same tonal primitives as the gallery."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import TITLE, open_transcript


@pytest.mark.parametrize("theme", ["light", "dark", "eink"])
def test_expanded_tool_input_uses_the_current_theme_palette(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    theme: str,
) -> None:
    _, transcript = open_transcript(page, make_server, tmp_path)
    with transcript.open("a") as stream:
        stream.write(json.dumps({
            "uuid": "highlight-tool",
            "type": "assistant",
            "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": "highlight-call", "name": "Bash",
                "input": {"command": "echo hello", "timeout": 42, "enabled": True},
            }]},
        }) + "\n")
    page.evaluate("theme => localStorage.setItem('jailbee.theme', theme)", theme)
    page.reload()
    page.get_by_role("button", name="Bash echo hello", exact=True).click()
    code = page.locator(".tool-input .highlight")
    expect(code).to_be_visible()
    for token, role in [("nt", "primary"), ("kc", "primary"), ("mi", "phase-job"), ("s2", "phase-autostart")]:
        colour = code.locator(f".{token}").first.evaluate("""(el, role) => {
            const probe = document.createElement('span');
            probe.style.color = `var(--md-${role})`;
            el.append(probe);
            const expected = getComputedStyle(probe).color;
            probe.remove();
            return [getComputedStyle(el).color, expected];
        }""", role)
        assert colour[0] == colour[1], (theme, token, colour)
        if theme == "eink":
            assert colour[0] == "rgb(0, 0, 0)"


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
