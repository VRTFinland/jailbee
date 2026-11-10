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
            shown["option_texts"] = [
                str(o.prompt)
                for widget in host.screen.query("OptionList, SelectionList")
                for o in (widget.get_option_at_index(i) for i in range(widget.option_count))
            ]
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


def test_text_accepts_an_empty_answer_like_the_terminal():
    # At the terminal "" is a normal answer; the command's validator decides.
    assert run(Prompt(1, "text", "name"), ["enter"])[0] == ""


def test_text_with_a_clear_shortcut_yields_empty_only_through_the_shortcut():
    p = Prompt(1, "text", "name", clear_shortcut=True)
    answer, _ = run(p, ["enter", "escape"])
    assert answer is None  # Enter on empty did not answer; Esc did
    assert run(p, ["space", "enter", "escape"])[0] is None  # blank is not an answer either
    assert run(p, ["ctrl+r"])[0] == ""
    assert run(p, [*"ab", "enter"])[0] == "ab"


def test_text_shows_why_the_previous_answer_was_refused():
    p = Prompt(2, "text", "name", error="not a valid branch name")
    _a, host = run(p, ["escape"])
    assert any("not a valid branch name" in t for t in host.shown["texts"])  # type: ignore[attr-defined]


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


def test_confirm_shows_the_detail_above_the_buttons():
    p = Prompt(1, "confirm", "Destroy x?", destructive=True, detail="3 unpushed commits")
    _a, host = run(p, ["n"])
    assert any("3 unpushed commits" in t for t in host.shown["texts"])  # type: ignore[attr-defined]


def test_choose_one_rows_show_the_title_the_terminal_shows_not_the_label():
    row = WireOption("0", "(config default)", "default", "")
    other = WireOption("1", "4h", "4h", "")
    p = Prompt(1, "choose_one", "How long?", options=(row, other))
    _a, host = run(p, ["escape"])
    rendered = _option_texts(host)
    assert rendered[0].strip() == "(config default)"


def _option_texts(host: Host) -> list[str]:
    return host.shown["option_texts"]  # type: ignore[attr-defined,no-any-return]


def test_choose_many_rows_show_titles_and_description_and_preticked_defaults():
    options = (
        WireOption("0", "first row", "one", "about one"),
        WireOption("1", "second row", "two", ""),
    )
    p = Prompt(1, "choose_many", "Publish first?", options=options, default=("1",))
    answer, host = run(p, ["enter"])
    assert answer == ["1"]  # pre-ticked: Enter alone confirms it
    rendered = _option_texts(host)
    assert "first row" in rendered[0] and "about one" in rendered[0]
    assert "second row" in rendered[1]
    assert "one" not in rendered[1]
