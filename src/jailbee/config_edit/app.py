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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    DynamicContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import TextArea

from jailbee.config_edit import render, values
from jailbee.config_edit import state as st
from jailbee.config_edit.layers import raw_for
from jailbee.config_edit.schema import FieldKind

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import Input
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent
    from prompt_toolkit.output import Output

    from jailbee.config_edit.layers import LayerName, LayerSet, Origin
    from jailbee.config_edit.save import WritePolicy
    from jailbee.config_edit.schema import FieldSpec

_SECTION_WIDTH = 20
_HELP_HEIGHT = 9

_TEXT_KINDS = frozenset(
    {FieldKind.STR, FieldKind.INT, FieldKind.PATH, FieldKind.CHOICE, FieldKind.SCALAR_UNION}
)
_BLOCK_KINDS = frozenset({FieldKind.STR_LIST, FieldKind.STR_MAP, FieldKind.BOOL_MAP})


@dataclass
class _Prompt:
    """The modal editor open over one field, or over the search line.

    `spec` is `None` exactly for the search prompt — it edits `state.query`,
    not a field, and has no `FieldSpec` to stage against. `multiline` decides
    the commit key, which is the one thing the user has to know: a single
    line commits on Enter, a block commits on Ctrl-S because Enter has to
    keep inserting rows.
    """

    spec: FieldSpec | None
    area: TextArea
    multiline: bool

    @property
    def label(self) -> str:
        if self.spec is None:
            return "Search — Enter to apply, Esc to cancel"
        verb = "Ctrl-S to commit" if self.multiline else "Enter to commit"
        return f"{'.'.join(self.spec.path)} — {verb}, Esc to cancel"


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
    prompt: _Prompt | None = field(default=None)

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

    # -- editing --------------------------------------------------------

    def toggle(self) -> None:
        """Space: flip the boolean under the cursor, if it is one and editable."""
        spec = st.current(self.state)
        if spec is None:
            return
        blocked = render.edit_block(spec, self.state.layer)
        if blocked is not None:
            self.notice(blocked, style="class:error")
            return
        if spec.kind is not FieldKind.BOOL:
            self.notice("Space toggles a true/false field — press Enter to edit this one.")
            return
        self.state = st.toggle_current(self.state)

    def reset(self) -> None:
        """`r`: stage a delete of this key from the open layer.

        Deleting rather than writing the default out (spec 4.3): a written-out
        default freezes at today's value, an inherited one keeps following
        jailbee's own.
        """
        spec = st.current(self.state)
        if spec is None:
            return
        blocked = render.edit_block(spec, self.state.layer)
        if blocked is not None:
            self.notice(blocked, style="class:error")
            return
        self.state = st.reset_current(self.state, raw_for(self.layer_set, self.state.layer))

    def edit_current(self) -> None:
        """Open the modal editor on the field under the cursor."""
        spec = st.current(self.state)
        if spec is None:
            return
        blocked = render.edit_block(spec, self.state.layer)
        if blocked is not None:
            self.notice(blocked, style="class:error")
            return
        value = st.effective(self.state, spec.path)
        if spec.kind is FieldKind.STR_LIST:
            self._open_prompt(spec, values.list_to_text(value), multiline=True)
        elif spec.kind in _BLOCK_KINDS:  # STR_MAP or BOOL_MAP; STR_LIST is handled above
            self._open_prompt(spec, values.map_to_text(value), multiline=True)
        elif spec.kind in _TEXT_KINDS:
            self._open_prompt(spec, values.to_text(spec, value), multiline=False)
        elif spec.kind is FieldKind.BOOL:
            self.state = st.toggle_current(self.state)
        else:
            self.notice(f"`{spec.kind.value}` fields are not editable here.", style="class:error")

    def open_search(self) -> None:
        """`/`: a modal line whose commit sets the search query."""
        self._open_prompt(None, self.state.query, multiline=False)

    def _open_prompt(self, spec: FieldSpec | None, text: str, *, multiline: bool) -> None:
        completer = None
        if spec is not None and spec.choices:
            completer = WordCompleter([str(c) for c in spec.choices], ignore_case=True)
        area = TextArea(
            text=text,
            multiline=multiline,
            completer=completer,
            complete_while_typing=completer is not None,
            height=6 if multiline else 1,
        )
        area.buffer.cursor_position = len(text)
        self.prompt = _Prompt(spec=spec, area=area, multiline=multiline)

    def cancel_prompt(self) -> None:
        self.prompt = None

    def commit_prompt(self) -> None:
        """Read the modal editor's text back into the staged changes.

        A parse failure keeps the prompt open with the error on the message
        line: closing it would throw away what was typed, which is exactly the
        moment it is worth keeping.
        """
        prompt = self.prompt
        if prompt is None:
            return
        text = prompt.area.text
        if prompt.spec is None:
            self.state = st.set_query(self.state, text.strip())
            self.prompt = None
            return
        spec = prompt.spec
        parsed: object
        error: str | None
        if spec.kind is FieldKind.STR_LIST:
            parsed, error = values.parse_list(spec, text)
        elif spec.kind in _BLOCK_KINDS:  # STR_MAP or BOOL_MAP; STR_LIST is handled above
            parsed, error = values.parse_map(spec, text)
        else:
            parsed, error = values.parse_value(spec, text)
        if error is not None:
            self.notice(error, style="class:error")
            return
        self.state = st.stage(self.state, spec.path, parsed)
        self.prompt = None


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

    def prompt_label() -> StyleAndTextTuples:
        return [("class:dim", f" {editor.prompt.label} ")] if editor.prompt is not None else []

    def prompt_area() -> TextArea | Window:
        return editor.prompt.area if editor.prompt is not None else Window()

    fields_window = Window(FormattedTextControl(fields_pane, get_cursor_position=fields_cursor))

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
                            fields_window,
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
            ConditionalContainer(
                HSplit(
                    [
                        Window(FormattedTextControl(prompt_label), height=1),
                        DynamicContainer(prompt_area),
                    ]
                ),
                filter=Condition(lambda: editor.prompt is not None),
            ),
            Window(FormattedTextControl(message_line), height=1),
            Window(FormattedTextControl(render.footer), height=1),
        ]
    )
    return Application(
        layout=Layout(root),
        key_bindings=_bindings(editor, fields_window),
        style=render.EDITOR_STYLE,
        full_screen=True,
        input=input,
        output=output,
    )


def _multiline(editor: Editor) -> bool:
    return editor.prompt is not None and editor.prompt.multiline


def _bindings(editor: Editor, fields_window: Window) -> KeyBindings:
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

    def _act_focus(fn: Callable[[], None]) -> Callable[[KeyPressEvent], None]:
        """Like `_act`, but also focuses the prompt's text area if `fn` opened one."""
        base = _act(fn)

        def handler(event: KeyPressEvent) -> None:
            base(event)
            if editor.prompt is not None:
                event.app.layout.focus(editor.prompt.area)

        return handler

    editing = Condition(lambda: editor.prompt is not None)
    browsing = ~editing

    kb.add("up", filter=browsing)(_act(lambda: editor.move(-1)))
    kb.add("k", filter=browsing)(_act(lambda: editor.move(-1)))
    kb.add("down", filter=browsing)(_act(lambda: editor.move(1)))
    kb.add("j", filter=browsing)(_act(lambda: editor.move(1)))
    kb.add("enter", filter=browsing)(_act_focus(editor.enter))
    kb.add("escape", filter=browsing, eager=True)(_act(editor.back))
    kb.add("a", filter=browsing)(
        _act(lambda: setattr(editor, "state", st.toggle_show_all(editor.state)))
    )
    kb.add(" ", filter=browsing)(_act(editor.toggle))
    kb.add("r", filter=browsing)(_act(editor.reset))
    kb.add("/", filter=browsing)(_act_focus(editor.open_search))

    @kb.add("enter", filter=editing & Condition(lambda: not _multiline(editor)))
    @kb.add("c-s", filter=editing)
    def _commit(event: KeyPressEvent) -> None:
        editor.clear_notice()
        editor.commit_prompt()
        if editor.prompt is None:
            event.app.layout.focus(fields_window)

    @kb.add("escape", filter=editing, eager=True)
    def _cancel(event: KeyPressEvent) -> None:
        editor.clear_notice()
        editor.cancel_prompt()
        event.app.layout.focus(fields_window)

    @kb.add("q", filter=browsing)
    @kb.add("c-c", filter=browsing)
    def _quit(event: KeyPressEvent) -> None:
        event.app.exit()

    return kb
