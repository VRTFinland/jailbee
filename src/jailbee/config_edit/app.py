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

from dataclasses import dataclass, field, replace
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
    from jailbee.config_edit.save import SavePlan, WritePolicy
    from jailbee.config_edit.schema import FieldSpec

_SECTION_WIDTH = 20
_HELP_HEIGHT = 9
_UNSAVED = "Unsaved changes — press q again to discard, or s to save."

_TEXT_KINDS = frozenset(
    {FieldKind.STR, FieldKind.INT, FieldKind.PATH, FieldKind.CHOICE, FieldKind.SCALAR_UNION}
)
_MAP_KINDS = frozenset({FieldKind.STR_MAP, FieldKind.BOOL_MAP})
"""`STR_LIST` is deliberately absent: both dispatches below check it first
(it needs `values.list_to_text`/`parse_list`, not the map functions), so a
kind set that included it here could never actually match on it — that
mismatch between name and contents is exactly what this set used to be
called (`_BLOCK_KINDS`, holding all three) before a review caught it."""


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
    confirm: SavePlan | None = field(default=None)
    diff_open: bool = False

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
        elif spec.kind in _MAP_KINDS:
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
        elif spec.kind in _MAP_KINDS:
            parsed, error = values.parse_map(spec, text)
        else:
            parsed, error = values.parse_value(spec, text)
        if error is not None:
            self.notice(error, style="class:error")
            return
        self.state = st.stage(self.state, spec.path, parsed)
        self.prompt = None

    # -- saving -----------------------------------------------------------

    def save(self) -> None:
        """Validate, then write — in that order, with nothing written on failure.

        Spec 3.5. Validation runs the *real* loader over the staged mapping,
        which is what makes it impossible for the editor to write a file the
        CLI would then reject: the retired-key check, the placement bans, the
        `container_prefix` regex and the cross-layer uniqueness rules are all
        loader-level and none of them is visible to pydantic alone.
        """
        from jailbee.config_edit.layers import raw_for, validate
        from jailbee.config_edit.save import build_plan

        edits = st.changes(self.state, raw_for(self.layer_set, self.state.layer))
        if not edits:
            self.notice("Nothing to save.")
            return
        error = validate(self.layer_set, self.state.layer, edits)
        if error is not None:
            self.notice(error, style="class:error")
            return
        plan = build_plan(self.layer_set, self.state.layer, edits, self.state.specs, self.policy)
        if plan.must_confirm:
            self.confirm = plan
            return
        self._write(plan)

    def confirm_save(self, *, accept: bool) -> None:
        """Answer the mandatory diff confirmation."""
        plan = self.confirm
        self.confirm = None
        if plan is None or not accept:
            self.notice("Not saved.")
            return
        self._write(plan)

    def _write(self, plan: SavePlan) -> None:
        from jailbee.config_edit.save import commit

        backup = commit(plan)
        self._reload()
        where = f" (backup: {backup.name})" if backup is not None else ""
        self.notice(f"Saved {plan.path}{where}")

    def _reload(self) -> None:
        """Re-read both layers and re-resolve origins, keeping the view put.

        Origins describe the layers as saved (spec 10.1 option b), and a save
        has just changed what "as saved" means — so they are rebuilt here, and
        only here. The cursor, the open section, the search and the show-all
        flag survive: the user's place in a 169-field tree is expensive to
        find again.
        """
        from jailbee.config_edit.layers import read_layers, resolve

        self.layer_set = read_layers(self.layer_set.repo_path, self.layer_set.global_path)
        fresh = st.open_editor(
            layer=self.state.layer,
            specs=self.state.specs,
            origins=resolve(self.state.specs, self.layer_set),
        )
        self.state = replace(
            fresh,
            section=self.state.section,
            index=self.state.index,
            query=self.state.query,
            show_all=self.state.show_all,
        )

    def show_diff(self) -> None:
        """`d`: the same preview the mandatory confirmation shows, on demand."""
        from jailbee.config_edit.layers import raw_for, validate
        from jailbee.config_edit.save import build_plan

        edits = st.changes(self.state, raw_for(self.layer_set, self.state.layer))
        if not edits:
            self.notice("Nothing staged — no diff to show.")
            return
        error = validate(self.layer_set, self.state.layer, edits)
        if error is not None:
            self.notice(error, style="class:error")
            return
        self.confirm = build_plan(
            self.layer_set, self.state.layer, edits, self.state.specs, self.policy
        )
        self.diff_open = True

    def close_diff(self) -> None:
        self.confirm = None
        self.diff_open = False

    def dirty(self) -> bool:
        from jailbee.config_edit.layers import raw_for

        return st.is_dirty(self.state, raw_for(self.layer_set, self.state.layer))


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
            ConditionalContainer(
                Window(
                    FormattedTextControl(_diff_text(editor)),
                    wrap_lines=False,
                ),
                filter=Condition(lambda: editor.confirm is not None),
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


def _diff_text(editor: Editor):  # type: ignore[no-untyped-def]  # returns a pt callable
    def render_diff() -> StyleAndTextTuples:
        plan = editor.confirm
        if plan is None:
            return []
        head: StyleAndTextTuples = [("class:title", f" {plan.policy} → {plan.path} \n")]
        if plan.dropped_comments and not editor.diff_open:
            head.append(
                (
                    "class:error",
                    f"This rewrites the file and drops {len(plan.dropped_comments)} "
                    f"hand-written comment line(s). Save anyway? [y/N]\n",
                )
            )
        elif editor.diff_open:
            head.append(("class:dim", "Esc to close\n"))
        return [*head, ("", plan.diff or "(no textual change)\n")]

    return render_diff


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
    confirming = Condition(lambda: editor.confirm is not None)

    kb.add("up", filter=browsing & ~confirming)(_act(lambda: editor.move(-1)))
    kb.add("k", filter=browsing & ~confirming)(_act(lambda: editor.move(-1)))
    kb.add("down", filter=browsing & ~confirming)(_act(lambda: editor.move(1)))
    kb.add("j", filter=browsing & ~confirming)(_act(lambda: editor.move(1)))
    kb.add("enter", filter=browsing & ~confirming)(_act_focus(editor.enter))
    kb.add("escape", filter=browsing & ~confirming, eager=True)(_act(editor.back))
    kb.add("a", filter=browsing & ~confirming)(
        _act(lambda: setattr(editor, "state", st.toggle_show_all(editor.state)))
    )
    kb.add(" ", filter=browsing & ~confirming)(_act(editor.toggle))
    kb.add("r", filter=browsing & ~confirming)(_act(editor.reset))
    kb.add("/", filter=browsing & ~confirming)(_act_focus(editor.open_search))
    kb.add("c-s", filter=browsing & ~confirming)(_act(editor.save))
    kb.add("s", filter=browsing & ~confirming)(_act(editor.save))
    kb.add("d", filter=browsing & ~confirming)(_act(editor.show_diff))

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

    @kb.add("y", filter=confirming & Condition(lambda: not editor.diff_open))
    def _yes(_event: KeyPressEvent) -> None:
        editor.confirm_save(accept=True)

    @kb.add("n", filter=confirming & Condition(lambda: not editor.diff_open))
    @kb.add("escape", filter=confirming, eager=True)
    def _no(_event: KeyPressEvent) -> None:
        if editor.diff_open:
            editor.close_diff()
        else:
            editor.confirm_save(accept=False)

    @kb.add("q", filter=browsing & ~confirming)
    def _quit(event: KeyPressEvent) -> None:
        if editor.dirty() and editor.message != _UNSAVED:
            editor.notice(_UNSAVED, style="class:error")
            return
        event.app.exit()

    def _abandon(event: KeyPressEvent) -> None:
        event.app.exit()

    kb.add("c-c")(_abandon)

    return kb
