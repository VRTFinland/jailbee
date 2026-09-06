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
from jailbee.config_edit.layers import lookup, validate_entry
from jailbee.config_edit.schema import FieldKind, dotted, is_drilldown

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import Input
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent
    from prompt_toolkit.output import Output

    from jailbee.config_edit.layers import LayerName, LayerSet
    from jailbee.config_edit.save import SavePlan, WritePolicy
    from jailbee.config_edit.schema import FieldSpec
    from jailbee.config_writer import KeyPath

_SECTION_WIDTH = 20
_HELP_HEIGHT = 9
_MAX_DROPPED_SHOWN = 12
"""How many dropped comment lines the confirmation prints before summarising.

Enough to show the whole of any hand-written note anyone actually leaves in a
config file, and few enough that the diff underneath is not pushed off the
pane by the header that introduces it."""
_UNSAVED = "Unsaved changes — press q again to discard, or s to save."
_ENTRY_INVALID = "This entry is incomplete — press Esc again to discard it. "

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
    map_key_for: FieldSpec | None = None
    """Set by `new_entry_here` for a `MODEL_MAP`: this prompt names the new
    entry's key rather than editing a field. `spec` is `None` here too (there
    is no field yet to attach to), so `commit_prompt` must tell the two
    `spec is None` prompts apart by checking this first."""
    secret_key: str | None = None
    """Set when this prompt is setting one key's token inside a secret map:
    `spec` is the map itself (`github.api_tokens`), and this is the key under
    the cursor (`"gisgro"`). Unlike `map_key_for`, `spec` is *not* `None`
    here — the map field already exists — so `commit_prompt` tells this
    apart from an ordinary field edit by checking `secret_key` first."""
    password: bool = False
    """Mirrors the `password=` the `TextArea` was built with (spec 11.9).

    Not redundant bookkeeping: `TextArea` accepts `password=` in its
    constructor but exposes no attribute for it (verified against the
    installed prompt_toolkit), so this is the only way a test — or a future
    reviewer — can prove the input is actually hidden rather than trusting
    that `_open_prompt` passed the right thing at construction time.
    """

    @property
    def label(self) -> str:
        if self.map_key_for is not None:
            return f"New {self.map_key_for.label} entry — name it, Enter to create"
        if self.secret_key is not None and self.spec is not None:
            # Names the key, never the value: this label is painted on every
            # redraw while the prompt is open, so it is exactly the kind of
            # place the one rule (never paint a token) has to hold.
            return f"{dotted(self.spec.path)}.{self.secret_key} — Enter to set, Esc to cancel"
        if self.spec is None:
            return "Search — Enter to apply, Esc to cancel"
        verb = "Ctrl-S to commit" if self.multiline else "Enter to commit"
        return f"{dotted(self.spec.path)} — {verb}, Esc to cancel"


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
    new_entry: KeyPath | None = None
    """The entry `n` created this session, so a second `Esc` on an invalid
    one removes it outright rather than merely discarding staged edits — an
    entry that was never saved has nothing to "keep". Set by `n` (Task 8);
    always `None` here."""

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
        """Descend: into a section, into a collection, into an entry, or edit.

        One key for all four because the screens are never focused at once —
        `state.trail` says which one is open, and `state.screen` reads it.
        """
        view = st.screen(self.state)
        if view.kind == "sections":
            names = st.sections(self.state)
            if names:
                self.state = st.enter_crumb(self.state, names[self.state.index])
            return
        if view.kind == "collection" and view.collection is not None:
            crumbs = st.entries(self.state, view.collection)
            if not crumbs:
                self.notice("No entries yet — press `n` to add one.")
                return
            if view.collection.item_model is None:
                # A secret map: there is no entry form to descend into, so
                # open a hidden input on the key under the cursor instead —
                # never seeded with the current token (see `_open_prompt`).
                self._open_prompt(view.collection, "", multiline=False, password=True)
                if self.prompt is not None:
                    self.prompt.secret_key = str(crumbs[self.state.index])
                return
            self.state = st.enter_crumb(self.state, crumbs[self.state.index])
            return
        spec = st.current(self.state)
        if spec is not None and is_drilldown(spec):
            self.state = st.enter_crumb(self.state, spec.path[len(self.state.trail)])
            return
        self.edit_current()

    def back(self) -> None:
        """Escape: clear a search, then leave — refusing an invalid entry once.

        The first press reports what is wrong and stays put, so the user can fix
        it; the second leaves anyway and throws the entry's staged edits away
        (spec 11.8). `message` is what tells the two presses apart, the same
        mechanism `_quit` uses for the unsaved-changes confirmation — so, like
        `_quit`, the key binding must not clear the message line before calling
        this (see `_bindings`'s docstring).
        """
        if self.state.query:
            self.state = st.set_query(self.state, "")
            self.clear_notice()
            return
        view = st.screen(self.state)
        if view.kind == "entry" and view.collection is not None:
            error = validate_entry(view.collection, st.own(self.state, view.entry_path))
            if error is not None and not self.message.startswith(_ENTRY_INVALID):
                self.notice(f"{_ENTRY_INVALID}{error}", style="class:error")
                return
            if error is not None:
                self.state = self._discard_entry(view)
        self.state = st.leave_crumb(self.state)
        self.clear_notice()

    def _discard_entry(self, view: st.Screen) -> st.EditorState:
        """Undo this entry: remove it if `n` made it, else drop its staged edits.

        Either branch walks the trail out of the entry via the caller's
        `leave_crumb` right after this returns — never the other way round.
        `delete_entry` changes the collection but deliberately leaves `trail`
        alone (its own docstring), and `stage` now raises on a path whose
        index no longer exists; discarding before leaving would land the
        cursor on a now-nonexistent entry with no crash to show for it until
        the *next* edit, discarding after leaving never happens because the
        entry is already gone by then. Doing it in this order — discard while
        still standing on the entry, leave right after — means the trail
        never points at a hole.
        """
        if view.collection is None:
            return self.state
        if self.new_entry == view.entry_path:
            self.new_entry = None
            return st.delete_entry(self.state, view.collection, view.entry_path[-1])
        return st.discard_under(self.state, view.entry_path)

    # -- collection editing ----------------------------------------------

    def _open_collection(self) -> FieldSpec | None:
        """The collection under the cursor, or `None` with a notice if there is none."""
        view = st.screen(self.state)
        if view.kind != "collection" or view.collection is None:
            self.notice("That key is not a collection — open one to add or remove entries.")
            return None
        return view.collection

    def new_entry_here(self) -> None:
        """`n`: append an entry and open it. A map is asked for its key first.

        A secret map is also asked for its key first — its condition joins
        `MODEL_MAP`'s rather than replacing it, since both need a name before
        there is anything to stage. `commit_prompt`'s `map_key_for` branch is
        what tells the two apart afterward and stages the new key's value as
        an empty string rather than `{}` (a secret map's entries are strings,
        not models).
        """
        spec = self._open_collection()
        if spec is None:
            return
        if spec.item_model is None or spec.kind is FieldKind.MODEL_MAP:
            self._open_prompt(None, "", multiline=False)
            if self.prompt is not None:
                self.prompt.map_key_for = spec
            return
        self.state, crumb = st.add_entry(self.state, spec)
        self.new_entry = (*self.state.trail, crumb)
        self.state = st.enter_crumb(self.state, crumb)

    def delete_entry_here(self) -> None:
        """`x`: remove the entry under the cursor. `x`, not `d`: `d` is the diff.

        Only reachable from the collection screen (`_open_collection`'s
        guard), which means the trail never extends into an entry here — so
        this can never delete the entry the trail itself stands in. That
        matters because `delete_entry` deliberately does not move the trail,
        and `stage` raises on a path whose index no longer exists; the guard
        is what keeps that pairing unreachable rather than a coincidence.
        """
        spec = self._open_collection()
        if spec is None:
            return
        crumbs = st.entries(self.state, spec)
        if not crumbs:
            self.notice("Nothing to delete here.")
            return
        self.state = st.delete_entry(self.state, spec, crumbs[self.state.index])
        self.state = st.move(self.state, 0)  # re-clamp: the list just got shorter

    def move_entry_here(self, delta: int) -> None:
        """`J`/`K`: swap the entry under the cursor with its neighbour."""
        spec = self._open_collection()
        if spec is None:
            return
        if spec.kind is FieldKind.MODEL_MAP:
            self.notice("A mapping has no order to change.")
            return
        before_state = self.state
        before = self.state.index
        self.state = st.move_entry(self.state, spec, before, delta)
        if self.state is not before_state:
            self.state = st.move(self.state, delta)

    # -- editing --------------------------------------------------------

    def _pending_reset_of(self, path: KeyPath) -> KeyPath | None:
        """The nearest staged ancestor of `path`, if it is a pending reset.

        `state.py` stays pure and cannot say so itself — `stage`'s own
        docstring: "Whether to *say* so belongs to `app.py`; this module is
        pure." A peek at `state.staged` (a public field) rather than a call
        into `state`'s private `_staged_ancestor`, and only useful called
        *before* the `stage`/`toggle_current` that is about to fold into (and
        so silently cancel) the reset it finds.
        """
        for i in range(len(path) - 1, 0, -1):
            ancestor = path[:i]
            if ancestor in self.state.staged:
                return ancestor if self.state.staged[ancestor] is st.UNSET else None
        return None

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
        cancelled = self._pending_reset_of(spec.path)
        self.state = st.toggle_current(self.state)
        if cancelled is not None:
            self.notice(f"This also cancels the pending reset of {dotted(cancelled)}.")

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
        discarding = any(
            len(p) > len(spec.path) and p[: len(spec.path)] == spec.path for p in self.state.staged
        )
        self.state = st.reset_current(self.state)
        if discarding:
            self.notice(f"Discarded pending edits inside {dotted(spec.path)}.")

    def edit_current(self) -> None:
        """Open the modal editor on the field under the cursor.

        `enter()` is the only place a drill-down field's row is supposed to
        reach this: it checks `is_drilldown` first and routes a collection —
        secret map included — to its own screen instead. But this method is
        public and takes no state from `enter()` about how it was reached, so
        it re-checks here rather than trusting that invariant to hold
        forever. For a `MODEL_LIST`/`MODEL_MAP` that would just be defence in
        depth (the kind dispatch below already falls through to the same
        "not editable here" notice, since neither kind matches a case
        above). For a secret `STR_MAP` it is load-bearing: `edit_block` now
        lets it through (Task 9 — it *is* editable, just not here), and its
        kind *does* match `_MAP_KINDS` below, which would otherwise hand
        `values.map_to_text` — every token in the map — straight to a plain
        multiline prompt. Confirmed by direct call in this task's audit
        before this guard existed.
        """
        spec = st.current(self.state)
        if spec is None:
            return
        blocked = render.edit_block(spec, self.state.layer)
        if blocked is not None:
            self.notice(blocked, style="class:error")
            return
        if is_drilldown(spec):
            self.notice(f"`{spec.kind.value}` fields are not editable here.", style="class:error")
            return
        value = st.effective(self.state, spec.path)
        if spec.kind is FieldKind.STR_LIST:
            self._open_prompt(spec, values.list_to_text(value), multiline=True)
        elif spec.kind in _MAP_KINDS:
            self._open_prompt(spec, values.map_to_text(value), multiline=True)
        elif spec.kind is FieldKind.OPAQUE:
            self._open_prompt(spec, values.opaque_to_text(value), multiline=True)
        elif spec.kind in _TEXT_KINDS:
            self._open_prompt(spec, values.to_text(spec, value), multiline=False)
        elif spec.kind is FieldKind.BOOL:
            # Through `toggle`, not a bare `st.toggle_current` call, so `Enter`
            # on a bool field gets the same pending-reset-cancelled notice
            # `Space` does — the two are otherwise the same one keystroke to
            # the user, and `toggle` has already re-checked `edit_block`/kind,
            # both of which just passed above, so this is not a new failure
            # mode, only shared plumbing.
            self.toggle()
        else:
            self.notice(f"`{spec.kind.value}` fields are not editable here.", style="class:error")

    def open_search(self) -> None:
        """`/`: a modal line whose commit sets the search query."""
        self._open_prompt(None, self.state.query, multiline=False)

    def _open_prompt(
        self, spec: FieldSpec | None, text: str, *, multiline: bool, password: bool = False
    ) -> None:
        completer = None
        if spec is not None and spec.choices:
            completer = WordCompleter([str(c) for c in spec.choices], ignore_case=True)
        area = TextArea(
            text=text,
            multiline=multiline,
            completer=completer,
            complete_while_typing=completer is not None,
            height=6 if multiline else 1,
            password=password,
        )
        area.buffer.cursor_position = len(text)
        self.prompt = _Prompt(spec=spec, area=area, multiline=multiline, password=password)

    def cancel_prompt(self) -> None:
        """Close the modal, discarding anything typed into it.

        A secret prompt on a key `n` just created is the one case this
        undoes more than the keystroke: `n` on a secret map stages
        `{key: ""}` *before* the value is even typed (there is no form to
        hold it meanwhile), so an untouched entry abandoned here must not
        survive as an empty token rather than as if `n` had never been
        pressed.
        """
        prompt = self.prompt
        if (
            prompt is not None
            and prompt.secret_key is not None
            and prompt.spec is not None
            and self.new_entry == (*prompt.spec.path, prompt.secret_key)
        ):
            spec = prompt.spec
            self.state = st.delete_entry(self.state, spec, prompt.secret_key)
            self.new_entry = None
            # `delete_entry` stages whatever the map is left holding — `{}`
            # when the key just removed was the only one in it. If the map
            # was not on this layer's file at all before this session either,
            # a staged `{}` still writes an empty `api_tokens: {}` on save —
            # not "as if `n` had never been pressed". Drop the staged key
            # outright in that case so a layer with nothing to begin with
            # ends up with nothing staged, not an empty one.
            present, _ = lookup(self.state.layer_raw, spec.path)
            if not present and self.state.staged.get(spec.path) == {}:
                staged = dict(self.state.staged)
                del staged[spec.path]
                self.state = replace(self.state, staged=staged)
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
        # Checked first, before both branches below: a secret-key prompt has
        # `spec` set (the map itself) and `map_key_for` unset, so neither of
        # the next two checks would catch it — and if it fell through to the
        # generic `spec.kind` dispatch at the bottom, `_MAP_KINDS` would hand
        # a bare token to `values.parse_map`, which on anything without `=`
        # in it reports the error as `got {entry!r}` — quoting the token
        # right back onto the message line. Closing that off here, before
        # any other branch can partially match, is what the one rule (never
        # paint a token) actually rests on for this prompt.
        if prompt.secret_key is not None and prompt.spec is not None:
            token = text.strip()
            if not token:
                self.notice(
                    "A token is required — press Esc to leave it unchanged.",
                    style="class:error",
                )
                return
            # `own`, not `effective`: this map is about to be staged back
            # into the open layer, so it must start from that layer's own
            # value. Seeding it from the merged one would copy another
            # layer's tokens into this file.
            current = st.own(self.state, prompt.spec.path)
            updated = dict(current) if isinstance(current, dict) else {}
            updated[prompt.secret_key] = token
            self.state = st.stage(self.state, prompt.spec.path, updated)
            if self.new_entry == (*prompt.spec.path, prompt.secret_key):
                # The key `n` just created now has a real token, so it is no
                # longer "new" — an unrelated Esc elsewhere must not treat it
                # as abandoned and delete it (`cancel_prompt`'s own guard).
                # Anything else `new_entry` might be tracking (a MODEL_LIST
                # entry left mid-form) is untouched.
                self.new_entry = None
            self.prompt = None
            return
        # Checked before `prompt.spec is None` below: a map-key prompt also has
        # `spec is None` (there is no field yet to attach to), and the search
        # branch would otherwise read the typed key name as a search query and
        # create nothing.
        if prompt.map_key_for is not None:
            key = text.strip()
            if not key:
                self.notice("A name is required.", style="class:error")
                return
            spec = prompt.map_key_for
            if key in st.entries(self.state, spec):
                self.notice(f"`{key}` already exists.", style="class:error")
                return
            if spec.secret:
                # A secret map's entry is a string, not a model: stage the
                # key with an empty placeholder, then open a hidden prompt to
                # fill it in — there is no entry form to descend into, and
                # the placeholder is never shown (`cancel_prompt` removes it
                # again if that second prompt is abandoned).
                current = st.own(self.state, spec.path)
                updated = dict(current) if isinstance(current, dict) else {}
                updated[key] = ""
                self.state = st.stage(self.state, spec.path, updated)
                self.new_entry = (*spec.path, key)
                self._open_prompt(spec, "", multiline=False, password=True)
                if self.prompt is not None:
                    self.prompt.secret_key = key
                return
            self.state, crumb = st.add_entry(self.state, spec, key)
            self.new_entry = (*self.state.trail, crumb)
            self.state = st.enter_crumb(self.state, crumb)
            self.prompt = None
            return
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
        elif spec.kind is FieldKind.OPAQUE:
            parsed, error = values.parse_opaque(text)
        else:
            parsed, error = values.parse_value(spec, text)
        if error is not None:
            self.notice(error, style="class:error")
            return
        cancelled = self._pending_reset_of(spec.path)
        self.state = st.stage(self.state, spec.path, parsed)
        self.prompt = None
        if cancelled is not None:
            self.notice(f"This also cancels the pending reset of {dotted(cancelled)}.")

    # -- saving -----------------------------------------------------------

    def save(self) -> None:
        """Validate, then write — in that order, with nothing written on failure.

        Spec 3.5. Validation runs the *real* loader over the staged mapping,
        which is what makes it impossible for the editor to write a file the
        CLI would then reject: the retired-key check, the placement bans, the
        `container_prefix` regex and the cross-layer uniqueness rules are all
        loader-level and none of them is visible to pydantic alone.
        """
        plan = self._plan("Nothing to save.")
        if plan is None:
            return
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

    def _plan(self, nothing_staged: str) -> SavePlan | None:
        """The plan for the staged edits, or `None` with a notice explaining why.

        Shared by `save` and `show_diff` so the two cannot drift on which
        failures stop a save: nothing staged, a mapping the loader rejects, and
        a rendering this package cannot read back (`RenderedYamlError`, spec
        3.5's last line of defence — `validate` sees the mapping, never the
        text). Every one of them keeps the session and the staged edits alive.
        """
        from jailbee.config_edit.layers import validate
        from jailbee.config_edit.save import RenderedYamlError, build_plan

        edits = st.changes(self.state)
        if not edits:
            self.notice(nothing_staged)
            return None
        error = validate(self.layer_set, self.state.layer, edits)
        if error is not None:
            self.notice(error, style="class:error")
            return None
        try:
            return build_plan(
                self.layer_set, self.state.layer, edits, self.state.specs, self.policy
            )
        except RenderedYamlError as e:
            self.notice(str(e), style="class:error")
            return None

    def _write(self, plan: SavePlan) -> None:
        """Commit `plan`, then re-read what is now on disk.

        An `OSError` here is ordinary — a read-only file, a root-owned
        `global.yaml`, a full disk — and is reported rather than raised: the
        key handler that called this has no `except`, so letting it out would
        take the application down and throw away every staged edit with it.
        """
        from jailbee.config_edit.save import commit

        try:
            backup = commit(plan)
        except OSError as e:
            self.notice(f"Could not write {plan.path}: {e}", style="class:error")
            return
        self._reload()
        where = f" (backup: {backup.name})" if backup is not None else ""
        self.notice(f"Saved {plan.path}{where}")

    def _reload(self) -> None:
        """Re-read both layers and re-resolve origins, keeping the view put.

        Origins describe the layers as saved (spec 10.1 option b), and a save
        has just changed what "as saved" means — so they are rebuilt here, and
        only here. The cursor, the open section, the search and the show-all
        flag survive: the user's place in a tree of eighty-odd fields is
        expensive to find again.
        """
        from jailbee.config_edit.layers import raw_for, read_layers, resolve

        self.layer_set = read_layers(self.layer_set.repo_path, self.layer_set.global_path)
        fresh = st.open_editor(
            layer=self.state.layer,
            specs=self.state.specs,
            origins=resolve(self.state.specs, self.layer_set),
            layer_raw=raw_for(self.layer_set, self.state.layer),
        )
        self.state = replace(
            fresh,
            trail=self.state.trail,
            index=self.state.index,
            query=self.state.query,
            show_all=self.state.show_all,
        )

    def show_diff(self) -> None:
        """`d`: the same preview the mandatory confirmation shows, on demand."""
        plan = self._plan("Nothing staged — no diff to show.")
        if plan is None:
            return
        self.confirm = plan
        self.diff_open = True

    def close_diff(self) -> None:
        self.confirm = None
        self.diff_open = False

    def dirty(self) -> bool:
        return st.is_dirty(self.state)


def run_editor(
    *,
    layer: LayerName,
    layer_set: LayerSet,
    specs: Sequence[FieldSpec],
    policy: WritePolicy,
    input: Input | None = None,
    output: Output | None = None,
) -> int:
    """Run the editor until the user quits. Returns a process exit code.

    Origins and the open layer's raw mapping are both derived from `layer_set`
    here rather than taken as arguments, so a caller cannot hand the session an
    `origins` that describes one set of layers and a file that is another.

    `input`/`output` exist for the tests, which drive a real `Application`
    through `create_pipe_input()` and a `DummyOutput` — the same idiom
    `tests/test_tui.py` uses for the forked questionary checkbox.
    """
    from jailbee.config_edit.layers import raw_for, resolve

    editor = Editor(
        layer_set=layer_set,
        state=st.open_editor(
            layer=layer,
            specs=specs,
            origins=resolve(specs, layer_set),
            layer_raw=raw_for(layer_set, layer),
        ),
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
        return render.body_pane(editor.state, editor.layer_set).fragments

    def fields_cursor() -> Point:
        return Point(0, render.body_pane(editor.state, editor.layer_set).cursor_row)

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
            Window(FormattedTextControl(lambda: render.footer(editor.state)), height=1),
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
            shown = plan.dropped_comments[:_MAX_DROPPED_SHOWN]
            head.append(
                (
                    "class:error",
                    f"This rewrites the file and drops {len(plan.dropped_comments)} "
                    f"hand-written comment line(s):\n",
                )
            )
            # Verbatim, not just counted (spec 2.5): the lines are what is at
            # risk, they are short, and the diff below scrolls out of view — a
            # count alone asks the user to consent to a loss they cannot see.
            head.extend(("class:error", f"  {line}\n") for line in shown)
            if len(plan.dropped_comments) > len(shown):
                head.append(
                    ("class:error", f"  … and {len(plan.dropped_comments) - len(shown)} more\n")
                )
            head.append(("class:error", "Save anyway? [y/N]\n"))
        elif editor.diff_open:
            head.append(("class:dim", "Esc to close\n"))
        return [*head, ("", plan.diff or "(no textual change)\n")]

    return render_diff


def _multiline(editor: Editor) -> bool:
    return editor.prompt is not None and editor.prompt.multiline


def _bindings(editor: Editor, fields_window: Window) -> KeyBindings:
    """The editor's keys.

    Most handlers clear the message line first — via `_act`/`_act_focus`, or
    explicitly in `_commit`/`_cancel` — so a notice that outlived the keypress
    it answered doesn't read as a fresh complaint about the key just pressed.
    Four handlers deliberately do not, each for a different reason, and none
    of them should be "fixed" into consistency with the rest:

    * `_quit` skips it on purpose. It compares `editor.message` against
      `_UNSAVED` to tell a first press (while dirty) from a confirming
      second one. Clearing the message first would make that comparison
      always true, so the second `q` could never be told apart from the
      first — the double-press-to-quit-while-dirty behaviour would trap the
      user in the warning forever with no way out.
    * `_back` (browsing `escape`) is the same trick for the same reason:
      `Editor.back` compares `editor.message` against `_ENTRY_INVALID` to
      tell a first `Esc` out of a broken entry (which refuses and explains)
      from a confirming second one (which discards and leaves). `back`
      manages its own message on every path — clearing it on a plain ascend,
      leaving it set on the first refusal — so the binding must hand it the
      message untouched and never clear afterward either.
    * `_yes`/`_no` (the confirm modal's accept/decline) don't clear either,
      but harmlessly: `confirm_save` always sets its own fresh notice
      ("Saved ..." or "Not saved."), and `close_diff` (reached via `n` or
      `escape` while a *read-only* diff is open) sets none at all, leaving
      whatever notice was already on screen — neither path reads the old
      message back, so not clearing has no effect worth guarding against.
      `n` is filtered on `confirming` alone, unlike `y`: the `[y/N]` prompt
      trains the user to press it, and closing a diff they opened themselves
      is harmless. `y`'s extra `not diff_open` is what stops `d` from
      becoming a second, unannounced save key.
    * `_abandon` (`c-c`) doesn't clear either, but it exits the application
      immediately, so there is no next frame for a stale notice to appear in.
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
    kb.add("a", filter=browsing & ~confirming)(
        _act(lambda: setattr(editor, "state", st.toggle_show_all(editor.state)))
    )
    kb.add(" ", filter=browsing & ~confirming)(_act(editor.toggle))
    kb.add("r", filter=browsing & ~confirming)(_act(editor.reset))
    kb.add("/", filter=browsing & ~confirming)(_act_focus(editor.open_search))
    kb.add("c-s", filter=browsing & ~confirming)(_act(editor.save))
    kb.add("s", filter=browsing & ~confirming)(_act(editor.save))
    kb.add("d", filter=browsing & ~confirming)(_act(editor.show_diff))
    kb.add("n", filter=browsing & ~confirming)(_act_focus(editor.new_entry_here))
    kb.add("x", filter=browsing & ~confirming)(_act(editor.delete_entry_here))
    kb.add("J", filter=browsing & ~confirming)(_act(lambda: editor.move_entry_here(1)))
    kb.add("K", filter=browsing & ~confirming)(_act(lambda: editor.move_entry_here(-1)))

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

    @kb.add("n", filter=confirming)
    @kb.add("escape", filter=confirming, eager=True)
    def _no(_event: KeyPressEvent) -> None:
        if editor.diff_open:
            editor.close_diff()
        else:
            editor.confirm_save(accept=False)

    @kb.add("escape", filter=browsing & ~confirming, eager=True)
    def _back(_event: KeyPressEvent) -> None:
        editor.back()

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
