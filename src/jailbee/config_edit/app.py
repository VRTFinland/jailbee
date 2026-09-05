"""The prompt_toolkit driver — the only impure module in `config_edit`.

Everything it draws comes from `render`, every state change goes through
`state`, and everything it writes goes through `save`. What is left here is
one `Application`, its key bindings, and the mutable session they act on.

prompt_toolkit rather than Rich-plus-raw-tty (spec 2.6): text buffers, focus
and scrolling are the parts of a form UI that hand-rolled code gets wrong, and
it is already in the tree via questionary, so choosing it added nothing to
install.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension

from jailbee.config_edit import render
from jailbee.config_edit import state as st

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import Output

    from jailbee.config_edit.layers import LayerName, LayerSet, Origin
    from jailbee.config_edit.save import WritePolicy
    from jailbee.config_edit.schema import FieldSpec

_SECTION_WIDTH = 20
_HELP_HEIGHT = 9


@dataclass
class Editor:
    """One open editor session: what is on screen and what is being said to it.

    A mutable holder rather than closures over locals, because the key
    bindings, the render callbacks and the save path all need the *current*
    state and Python's closure rules would make that three `nonlocal`
    declarations per binding.
    """

    layer_set: LayerSet
    state: st.EditorState
    policy: WritePolicy
    message: str = ""
    message_style: str = "class:notice"

    def notice(self, text: str, *, style: str = "class:notice") -> None:
        """Say something on the message line. Cleared by the next keypress."""
        self.message = text
        self.message_style = style

    def clear_notice(self) -> None:
        self.message = ""
        self.message_style = "class:notice"

    # -- movement -------------------------------------------------------

    def move(self, delta: int) -> None:
        self.state = st.move(self.state, delta)

    def enter(self) -> None:
        """Open the section under the cursor, or edit the field under it.

        The one Enter key does both because the two lists are never focused at
        the same time: `state.section` is `None` exactly while the section list
        has the cursor.
        """
        if self.state.section is None and not self.state.query:
            names = st.sections(self.state)
            if names:
                self.state = st.enter_section(self.state, names[self.state.index])
            return
        self.edit_current()

    def back(self) -> None:
        """Escape: clear a search first, then leave the section."""
        if self.state.query:
            self.state = st.set_query(self.state, "")
            return
        self.state = st.leave_section(self.state)

    def edit_current(self) -> None:
        """Open an editor on the field under the cursor. Filled in by task 8."""
        self.notice("Editing is not wired up yet.", style="class:error")


def run_editor(
    *,
    layer: LayerName,
    layer_set: LayerSet,
    specs: Sequence[FieldSpec],
    origins: Mapping[tuple[str, ...], Origin],
    policy: WritePolicy,
    input: Input | None = None,
    output: Output | None = None,
) -> int:
    """Run the editor until the user quits. Returns a process exit code.

    `input`/`output` exist for the tests, which drive a real `Application`
    through `create_pipe_input()` and a `DummyOutput` — the same idiom
    `tests/test_tui.py` uses for the forked questionary checkbox.
    """
    editor = Editor(
        layer_set=layer_set,
        state=st.open_editor(layer=layer, specs=specs, origins=origins),
        policy=policy,
    )
    application = _build_application(editor, input=input, output=output)
    application.run()
    return 0


def _build_application(
    editor: Editor, *, input: Input | None, output: Output | None
) -> Application[None]:
    def sections_pane() -> StyleAndTextTuples:
        return render.section_pane(editor.state).fragments

    def sections_cursor() -> Point:
        return Point(0, render.section_pane(editor.state).cursor_row)

    def fields_pane() -> StyleAndTextTuples:
        return render.field_pane(editor.state, editor.layer_set).fragments

    def fields_cursor() -> Point:
        return Point(0, render.field_pane(editor.state, editor.layer_set).cursor_row)

    def message_line() -> StyleAndTextTuples:
        return [(editor.message_style, f" {editor.message} ")] if editor.message else []

    root = HSplit(
        [
            Window(
                FormattedTextControl(lambda: render.title_bar(editor.state, editor.layer_set)),
                height=1,
            ),
            VSplit(
                [
                    Window(
                        FormattedTextControl(sections_pane, get_cursor_position=sections_cursor),
                        width=_SECTION_WIDTH,
                    ),
                    Window(char="│", width=1),
                    HSplit(
                        [
                            Window(
                                FormattedTextControl(fields_pane, get_cursor_position=fields_cursor)
                            ),
                            Window(char="─", height=1),
                            Window(
                                FormattedTextControl(
                                    lambda: render.help_pane(editor.state, editor.layer_set)
                                ),
                                height=Dimension(preferred=_HELP_HEIGHT, max=_HELP_HEIGHT),
                                wrap_lines=True,
                            ),
                        ]
                    ),
                ]
            ),
            Window(FormattedTextControl(message_line), height=1),
            Window(FormattedTextControl(render.footer), height=1),
        ]
    )
    return Application(
        layout=Layout(root),
        key_bindings=_bindings(editor),
        style=render.EDITOR_STYLE,
        full_screen=True,
        input=input,
        output=output,
    )


def _bindings(editor: Editor) -> KeyBindings:
    """The editor's keys.

    Every binding clears the message line first: a notice that outlived the
    keypress it answered would read as a fresh complaint about the key just
    pressed.
    """
    kb = KeyBindings()

    def _act(fn: Callable[[], None]) -> Callable[[KeyPressEvent], None]:
        def handler(_event: KeyPressEvent) -> None:
            editor.clear_notice()
            fn()

        return handler

    kb.add("up")(_act(lambda: editor.move(-1)))
    kb.add("k")(_act(lambda: editor.move(-1)))
    kb.add("down")(_act(lambda: editor.move(1)))
    kb.add("j")(_act(lambda: editor.move(1)))
    kb.add("enter")(_act(editor.enter))
    kb.add("escape", eager=True)(_act(editor.back))
    kb.add("a")(_act(lambda: setattr(editor, "state", st.toggle_show_all(editor.state))))

    @kb.add("q")
    @kb.add("c-c")
    def _quit(event: KeyPressEvent) -> None:
        event.app.exit()

    return kb
