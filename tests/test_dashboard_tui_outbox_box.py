"""The Outbox manager box: keys to intents, cursor keeping, the preview threshold."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from textual import events
from textual.app import App, ComposeResult

from jailbee.dashboard import outbox as dob
from jailbee.dashboard.tui.native import NATIVE_LIST_ID, OverlayBox, build_box
from jailbee.dashboard.tui.outbox_box import OutboxBox
from jailbee.dashboard.tui.overlay import overlay_key
from tests.dashboard_pilot import screen_text
from tests.test_dashboard_outbox import _action, _key, _proposal, _state

ALL = dob.Abilities(True, True, False, True, True, True)
A, B = "pr/a.json", "pr/b.json"


class Host(App[None]):
    CSS = "Screen { background: ansi_default; }"

    def __init__(self, spec: dob.OutboxState, standalone: bool = False) -> None:
        super().__init__(ansi_color=True)
        self.intents: list[OutboxBox.Intent] = []
        self.spec = spec
        self.standalone = standalone

    def on_outbox_box_intent(self, message: OutboxBox.Intent) -> None:
        self.intents.append(message)

    def compose(self) -> ComposeResult:
        yield OutboxBox(self.spec, mouse_enabled=lambda: True, standalone=self.standalone)


def run(spec, scenario, *, size=(100, 30), standalone=False):
    result = []

    async def main() -> None:
        app = Host(spec, standalone)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            result.append(await scenario(app, app.query_one(OutboxBox), pilot))

    asyncio.run(main())
    return result[0]


def press(box, key, char=None):
    return box.handle_key(events.Key(key, char))


def state2(**over):
    over.setdefault("abilities", ALL)
    return _state(_proposal(A), _proposal(B), **over)


@pytest.mark.parametrize(
    ("key", "char", "name"),
    [
        ("enter", None, "expand"),
        ("right", None, "expand"),
        ("left", None, "collapse"),
        ("space", " ", "mark"),
        ("d", "d", "delete"),
        ("p", "p", "publish"),
        ("c", "c", "create-pr"),
        ("X", "X", "clear"),
        ("v", "v", "preview"),
        ("m", "m", "raw"),
        ("r", "r", "refresh"),
        ("a", "a", "scope"),
    ],
)
def test_keys_map_to_intents(key, char, name):
    async def scenario(app, box, pilot):
        await press(box, "down")  # second proposal
        return await press(box, key, char)

    got = run(state2(), scenario, standalone=True)
    assert isinstance(got, OutboxBox.Intent)
    value = "on" if name == "preview" else None  # a hidden preview is shown
    assert (got.name, got.row, got.value) == (name, _key(B), value)
    assert got.key == ("outbox", "", None)


def test_escape_and_q():
    async def scenario(app, box, pilot):
        return await press(box, "escape"), await press(box, "q", "q")

    esc, q = run(state2(), scenario)
    assert isinstance(esc, OverlayBox.Cancelled) and q is False
    esc, q = run(state2(), scenario, standalone=True)
    assert isinstance(q, OverlayBox.Cancelled)


def test_ability_off_key_is_eaten():
    off = dob.Abilities(False, False, False, False, False, False)

    async def scenario(app, box, pilot):
        return [await press(box, k, k) for k in "dpcXa"]

    assert run(state2(abilities=off), scenario, standalone=True) == [True] * 5


def test_scope_key_is_eaten_in_the_dashboard():
    async def scenario(app, box, pilot):
        return await press(box, "a", "a")

    assert run(state2(), scenario) is True


def question():
    return dob.Question(
        "publish",
        "Publish?",
        ("line one", "[red]x[/red]"),
        (("Publish", "publish"), ("Publish as draft", "draft"), ("Cancel", "cancel")),
        default="cancel",
    )


def test_question_keys_answer_and_cancel():
    async def scenario(app, box, pilot):
        first = await press(box, "enter")
        await press(box, "up")
        second = await press(box, "enter")
        third = await press(box, "d", "d")  # row keys are eaten while asking
        quit_ = await press(box, "q", "q")
        esc = await press(box, "escape")
        return first, second, third, quit_, esc

    first, second, third, quit_, esc = run(state2(question=question()), scenario)
    assert (first.name, first.value, first.row) == ("answer", "cancel", None)
    assert (second.name, second.value) == ("answer", "draft")
    assert third is True and quit_ is True
    assert (esc.name, esc.value) == ("cancel", None)


def test_show_keeps_cursor_on_same_row_key():
    async def scenario(app, box, pilot):
        await press(box, "down")
        assert (await press(box, "space", " ")) is not None
        box.show(_state(_proposal("pr/new.json"), _proposal(A), _proposal(B), abilities=ALL))
        await pilot.pause()
        return (await press(box, "d", "d")).row

    assert run(state2(), scenario) == _key(B)


def test_show_nearest_index_when_row_gone():
    async def scenario(app, box, pilot):
        await press(box, "down")
        box.show(_state(_proposal(A), abilities=ALL))
        await pilot.pause()
        return (await press(box, "d", "d")).row

    assert run(state2(), scenario) == _key(A)


def test_show_places_cursor_on_changed_spec_cursor():
    async def scenario(app, box, pilot):
        box.show(replace(state2(), cursor=_key(B)))
        await pilot.pause()
        return (await press(box, "d", "d")).row

    assert run(state2(), scenario) == _key(B)


def test_stale_spec_cursor_does_not_pull_cursor_back():
    async def scenario(app, box, pilot):
        sent = await press(box, "d", "d")  # cursor on A, as the controller will echo
        await press(box, "down")
        box.show(replace(state2(), cursor=sent.row))
        await pilot.pause()
        return (await press(box, "d", "d")).row

    assert run(state2(), scenario) == _key(B)


def test_repaint_with_unchanged_spec_cursor_keeps_arrow_moves():
    # The dashboard repaints the box with the same state on every visible change;
    # the controller's cursor (the first row) was never moved by arrow keys.
    start = replace(state2(), cursor=_key(A))

    async def scenario(app, box, pilot):
        await press(box, "down")
        box.show(start)
        await pilot.pause()
        return (await press(box, "d", "d")).row

    assert run(start, scenario) == _key(B)


def expanded_a(**over):
    """A's actions shown: rows A, A action 0, A action 1, B; no action has comments."""
    return state2(expanded=frozenset({_key(A)}), **over)


def test_enter_on_a_childless_row_opens_a_hidden_preview():
    async def scenario(app, box, pilot):
        parent = await press(box, "enter")  # A has actions: expands
        await press(box, "down")
        enter = await press(box, "enter")
        right = await press(box, "right")
        return parent, enter, right

    parent, enter, right = run(expanded_a(), scenario, size=(80, 30))
    assert (parent.name, parent.value) == ("expand", None)
    assert (enter.name, enter.row, enter.value) == ("preview", _key(A, 0), "on")
    assert (right.name, right.value) == ("expand", None)  # → stays expand-only


def test_enter_on_a_childless_row_expands_when_the_preview_shows():
    async def scenario(app, box, pilot):
        await press(box, "down")
        return await press(box, "enter")

    got = run(expanded_a(), scenario, size=(140, 30))
    assert (got.name, got.value) == ("expand", None)


@pytest.mark.parametrize(
    ("preview", "size", "value"),
    [
        ("auto", (80, 30), "on"),  # hidden: show it
        ("auto", (140, 30), "off"),  # beside the list: hide it
        ("off", (140, 30), "on"),
        ("on", (80, 30), "auto"),  # replacing the list: back to the list
    ],
)
def test_v_toggles_what_is_on_screen(preview, size, value):
    async def scenario(app, box, pilot):
        return await press(box, "v", "v")

    got = run(state2(preview=preview), scenario, size=size)
    assert (got.name, got.value) == ("preview", value)


@pytest.mark.parametrize("key", ["escape", "enter"])
def test_escape_and_enter_leave_a_preview_that_replaced_the_list(key):
    async def scenario(app, box, pilot):
        return await press(box, key)

    got = run(state2(preview="on"), scenario, size=(80, 30), standalone=True)
    assert isinstance(got, OutboxBox.Intent)
    assert (got.name, got.value) == ("preview", "auto")


def test_side_by_side_threshold():
    def visibility(app):
        return (
            app.query_one(f"#{NATIVE_LIST_ID}").display,
            app.query_one("#outbox-right").display,
        )

    async def wide(app, box, pilot):
        return visibility(app)

    async def narrow(app, box, pilot):
        before = visibility(app)
        box.show(replace(state2(), preview="on"))
        await pilot.pause()
        return before, visibility(app)

    assert run(state2(), wide, size=(120, 30)) == (True, True)
    assert run(state2(), narrow, size=(80, 30)) == ((True, False), (False, True))


def test_preview_off_hides_preview_even_when_wide():
    async def scenario(app, box, pilot):
        return app.query_one("#outbox-right").display

    assert run(state2(preview="off"), scenario, size=(120, 30)) is False


def test_question_forces_preview_when_narrow():
    async def scenario(app, box, pilot):
        return app.query_one("#outbox-right").display, app.query_one("#outbox-question").display

    assert run(state2(question=question()), scenario, size=(70, 30)) == (True, True)


def test_hostile_text_stays_literal():
    hostile = _proposal(A, actions=[_action(0, title="[red]x[/red]", body="[bold]b[/bold]")])
    spec = _state(hostile, abilities=ALL, question=question())

    async def scenario(app, box, pilot):
        return screen_text(app)

    text = run(spec, scenario, size=(120, 30))
    assert "[red]x[/red]" in text


def test_narrow_widths_do_not_overflow():
    long_title = _proposal(A, actions=[_action(0, title="t" * 200)])
    spec = _state(long_title, abilities=ALL, expanded=frozenset({_key(A)}))

    async def scenario(app, box, pilot):
        return screen_text(app).splitlines()

    for size, preview in (((40, 20), "auto"), ((40, 20), "on"), ((120, 20), "auto")):
        lines = run(replace(spec, preview=preview), scenario, size=size)
        # every boxed line still ends in the border: nothing spilled past the box
        assert all(line.rstrip().endswith(("│", "╮", "╯")) for line in lines if line.strip())
        assert all(len(line) <= size[0] for line in lines)


def test_state_and_content_rows_and_key():
    async def scenario(app, box, pilot):
        return box.state(), box.content_rows()

    state, rows = run(state2(preview="on"), scenario)
    assert (state.kind, state.cursor, state.text) == ("outbox", 0, "on")
    assert rows == 10


def test_build_box_and_overlay_key():
    spec = replace(state2(), prefix="alpha", scope_name="alpha-x")
    assert isinstance(build_box(spec, mouse_enabled=lambda: True), OutboxBox)
    assert overlay_key(spec) == ("outbox", "alpha", "alpha-x")


def test_question_title_is_visible_and_bold_first_line():
    async def scenario(app, box, pilot):
        return screen_text(app).splitlines()

    lines = run(state2(question=question()), scenario, size=(100, 30))
    body = [line for line in lines if line.startswith("│")]
    assert "Publish?" in body[0]
    assert "line one" in body[1]


def test_busy_status_line_is_an_animated_spinner():
    from rich.spinner import Spinner

    async def scenario(app, box, pilot):
        line = app.query_one("#outbox-status")
        busy = (type(line.content), line.auto_refresh, screen_text(app))
        box.show(state2(status=("done",)))
        await pilot.pause()
        return busy, (type(line.content), line.auto_refresh)

    busy, idle = run(state2(busy="reading the outbox…"), scenario)
    assert busy[0] is Spinner and busy[1]
    assert "⏳ reading the outbox…" in busy[2]
    assert idle[0] is not Spinner and idle[1] is None


def test_hostile_status_busy_and_title_are_sanitised():
    hostile = "\x1b]0;pwn\x07[red]x[/red]\u200b"

    async def scenario(app, box, pilot):
        return screen_text(app)

    for over in (
        {"status": (hostile,)},
        {"busy": hostile},
        {"question": replace(question(), title=hostile)},
    ):
        text = run(state2(**over), scenario, size=(100, 30))
        assert "[red]x[/red]" in text
        assert not any(c in text for c in ("\x1b", "\x07", "\u200b"))


def test_question_open_adds_its_rows_to_content_rows():
    async def scenario(app, box, pilot):
        return box.content_rows()

    base = run(state2(), scenario)
    assert run(state2(question=question()), scenario) == base + 3


def test_question_keeps_three_preview_rows_in_a_short_box():
    async def scenario(app, box, pilot):
        return app.query_one("#outbox-preview").size.height

    assert run(state2(question=question()), scenario, size=(100, 12)) >= 3


def test_ctrl_c_cancels_a_question():
    async def scenario(app, box, pilot):
        return await press(box, "ctrl+c")

    got = run(state2(question=question()), scenario)
    assert (got.name, got.value) == ("cancel", None)


def test_double_click_row_posts_toggle_and_click_choice_posts_answer():
    async def rows(app, box, pilot):
        await pilot.click("#native-list", offset=(5, 1), times=2)
        await pilot.pause()
        return app.intents

    got = run(state2(), rows)
    assert [(i.name, i.row) for i in got][-1] == ("toggle", _key(B))

    async def choice(app, box, pilot):
        await pilot.click("#outbox-question", offset=(3, 1))
        await pilot.pause()
        return app.intents

    got = run(state2(question=question()), choice)
    assert [(i.name, i.value) for i in got] == [("answer", "draft")]
