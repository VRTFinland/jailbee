"""One modal per prompt kind: what it shows and what it answers with."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App
from textual.widgets import Button, Input, OptionList, SelectionList

from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt, WireContext, WireOption


class Host(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.answers: list[Any] = []

    def ask(self, prompt: Prompt) -> None:
        self.push_screen(pm.modal_for(prompt), self.answers.append)


def opts(*titles: str) -> tuple[WireOption, ...]:
    return tuple(WireOption(str(i), t, t, "") for i, t in enumerate(titles))


def run(prompt: Prompt, keys: list[str]) -> tuple[Any, Host]:
    host = Host()
    shown: dict[str, Any] = {}

    async def script() -> None:
        async with host.run_test() as pilot:
            host.ask(prompt)
            await pilot.pause()
            shown["screen"] = host.screen
            shown["focused"] = host.focused
            shown["texts"] = [str(w.render()) for w in host.screen.query("Static")]
            for key in keys:
                await pilot.press(key)
            await pilot.pause()

    asyncio.run(script())
    host.shown = shown  # type: ignore[attr-defined]
    return (host.answers[0] if host.answers else "NO-ANSWER"), host


def test_confirm_yes_and_no_keys():
    p = Prompt(1, "confirm", "Go on?", default=True)
    assert run(p, ["y"])[0] is True
    assert run(p, ["n"])[0] is False


def test_confirm_enter_takes_the_focused_default():
    assert run(Prompt(1, "confirm", "Go on?", default=True), ["enter"])[0] is True
    assert run(Prompt(1, "confirm", "Go on?", default=False), ["enter"])[0] is False


def test_destructive_confirm_is_styled_and_focuses_no():
    p = Prompt(1, "confirm", "Delete x?", default=False, destructive=True)
    answer, host = run(p, ["enter"])
    screen = host.shown["screen"]  # type: ignore[attr-defined]
    assert screen.has_class("destructive")
    assert "DESTRUCTIVE" in screen.heading()
    assert isinstance(host.shown["focused"], Button)  # type: ignore[attr-defined]
    assert host.shown["focused"].id == "no"  # type: ignore[attr-defined]
    assert answer is False  # Enter on the preset focus must not delete


def test_plain_confirm_is_not_styled_destructive():
    _answer, host = run(Prompt(1, "confirm", "Go on?", default=True), ["y"])
    assert not host.shown["screen"].has_class("destructive")  # type: ignore[attr-defined]


def test_escape_and_ctrl_c_cancel_every_kind():
    prompts = [
        Prompt(1, "confirm", "q"),
        Prompt(1, "choose_one", "x", options=opts("a", "b")),
        Prompt(1, "choose_many", "x", options=opts("a", "b")),
        Prompt(1, "text", "x"),
        Prompt(1, "secret", "x"),
    ]
    for p in prompts:
        assert run(p, ["escape"])[0] is None, p.kind
        assert run(p, ["ctrl+c"])[0] is None, p.kind


def test_choose_one_returns_the_highlighted_option_id():
    p = Prompt(1, "choose_one", "branch", options=opts("a", "b", "c"))
    assert run(p, ["down", "enter"])[0] == "1"


def test_choose_one_preselects_the_default():
    p = Prompt(1, "choose_one", "branch", options=opts("a", "b", "c"), default="2")
    answer, host = run(p, ["enter"])
    assert answer == "2"
    assert isinstance(host.shown["focused"], OptionList)  # type: ignore[attr-defined]


def test_choose_many_returns_ticked_ids_in_option_order():
    p = Prompt(1, "choose_many", "which", options=opts("a", "b", "c"))
    assert run(p, ["down", "down", "space", "up", "up", "space", "enter"])[0] == ["0", "2"]


def test_choose_many_with_nothing_ticked_is_an_empty_list():
    p = Prompt(1, "choose_many", "which", options=opts("a", "b"))
    answer, host = run(p, ["enter"])
    assert answer == []
    assert isinstance(host.shown["focused"], SelectionList)  # type: ignore[attr-defined]


def test_text_returns_what_was_typed():
    assert run(Prompt(1, "text", "name"), [*"abc", "enter"])[0] == "abc"


def test_text_prefills_its_default():
    assert run(Prompt(1, "text", "name", default="main"), ["enter"])[0] == "main"


def test_text_refuses_an_empty_answer_unless_clear_shortcut():
    p = Prompt(1, "text", "name")
    answer, _ = run(p, ["enter", "escape"])
    assert answer is None  # Enter did not answer; Esc did
    assert run(Prompt(1, "text", "name", clear_shortcut=True), ["enter"])[0] == ""


def test_secret_is_masked_and_returned_verbatim():
    p = Prompt(1, "secret", "token")
    answer, host = run(p, [*"hunter2", "enter"])
    assert answer == "hunter2"
    assert host.shown["focused"].password is True  # type: ignore[attr-defined]


def test_text_is_not_masked():
    _answer, host = run(Prompt(1, "text", "name"), ["escape"])
    assert isinstance(host.shown["focused"], Input)  # type: ignore[attr-defined]
    assert host.shown["focused"].password is False  # type: ignore[attr-defined]


def test_context_is_shown():
    p = Prompt(1, "confirm", "q", context=WireContext(repo="alpha", container="alpha-x"))
    _a, host = run(p, ["y"])
    assert any("alpha / alpha-x" in t for t in host.shown["texts"])  # type: ignore[attr-defined]
