"""The editor's state machine: what is on screen and what has been staged.

Pure, following `dashboard_settings.py`. Every transition takes a state
and returns a new one; nothing here reads a key, draws a cell or touches
a file. That is what lets the whole interaction model — navigation,
search, staging, reset — be tested without a terminal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from jailbee.config_edit.layers import lookup
from jailbee.config_edit.schema import FieldKind
from jailbee.config_writer import DELETE, YamlChange

if TYPE_CHECKING:
    from jailbee.config_edit.layers import LayerName, Origin
    from jailbee.config_edit.schema import FieldSpec


@dataclass(frozen=True)
class EditorState:
    """One open editor.

    `section` is `None` while the section list has focus and the section's
    top-level key while its fields do. `staged` holds only the paths the
    user has changed, keyed the same way `FieldSpec.path` and
    `YamlChange.path` are, so the three never need translating between
    each other.
    """

    layer: LayerName
    specs: tuple[FieldSpec, ...]
    origins: Mapping[tuple[str, ...], Origin]
    staged: Mapping[tuple[str, ...], object]
    section: str | None = None
    index: int = 0
    query: str = ""
    show_all: bool = False


def open_editor(
    *,
    layer: LayerName,
    specs: Sequence[FieldSpec],
    origins: Mapping[tuple[str, ...], Origin],
) -> EditorState:
    """A fresh editor on the section list with nothing staged."""
    return EditorState(layer=layer, specs=tuple(specs), origins=origins, staged={})


def sections(state: EditorState) -> tuple[str, ...]:
    """Top-level keys, in declaration order, deduplicated.

    A top-level *leaf* (`container_prefix`, `egress_allow`) is its own
    section of one. Giving it a section rather than a special "top level"
    bucket keeps every field reachable by the same two keystrokes and
    keeps the section list in schema order.
    """
    out: list[str] = []
    for spec in state.specs:
        if spec.path[0] not in out:
            out.append(spec.path[0])
    return tuple(out)


def _matches(spec: FieldSpec, query: str) -> bool:
    needle = query.casefold()
    haystack = (".".join(spec.path), spec.label, spec.description)
    return any(needle in field.casefold() for field in haystack)


def visible_specs(state: EditorState) -> tuple[FieldSpec, ...]:
    """The fields the field pane shows right now.

    Search wins over everything: it spans all sections and **ignores the
    basic/advanced filter** (spec 4.3). At this schema size search is how
    a field actually gets found, and filtering its results would hide
    exactly what was being looked for.
    """
    if state.query:
        return tuple(s for s in state.specs if _matches(s, state.query))
    if state.section is None:
        return ()
    in_section = tuple(s for s in state.specs if s.path[0] == state.section)
    if state.show_all:
        return in_section
    return tuple(s for s in in_section if not s.advanced)


def current(state: EditorState) -> FieldSpec | None:
    """The field under the cursor, or `None` while the section list has focus."""
    rows = visible_specs(state)
    if not rows or state.index >= len(rows):
        return None
    return rows[state.index]


def move(state: EditorState, delta: int) -> EditorState:
    """Move the cursor within the current list, clamped at both ends."""
    total = len(visible_specs(state)) if (state.section or state.query) else len(sections(state))
    last = max(0, total - 1)
    return replace(state, index=max(0, min(last, state.index + delta)))


def enter_section(state: EditorState, name: str) -> EditorState:
    """Focus `name`'s fields, cursor at the top.

    The cursor resets because sections differ in length: carrying an index
    across could leave it past the end of a shorter one.
    """
    return replace(state, section=name, index=0, query="")


def leave_section(state: EditorState) -> EditorState:
    """Return to the section list, clearing any active search."""
    return replace(state, section=None, index=0, query="")


def set_query(state: EditorState, query: str) -> EditorState:
    """Set the search string. Empty restores the section view."""
    return replace(state, query=query, index=0)


def toggle_show_all(state: EditorState) -> EditorState:
    """Flip between the curated set and every field in the section."""
    return replace(state, show_all=not state.show_all, index=0)


class _Unset:
    """Type of the `UNSET` sentinel; exists so mypy can name it."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()
"""`staged` value meaning "reset this field": delete the key from this layer.

A sentinel rather than `None`, for the same reason `config_writer.DELETE`
is one — `None` is a legitimate staged value (`chrome.url: null`), so
reusing it would make a deliberate null indistinguishable from a reset.
`DELETE` itself is not reused here because it is the *writer's*
vocabulary: `changes()` translates `UNSET` into it only after deciding
the key is actually present, and a reset of an inherited key produces no
`YamlChange` at all.
"""


def effective(state: EditorState, path: tuple[str, ...]) -> object:
    """The value the user currently sees for `path`.

    A staged edit wins over the resolved origin; a staged reset falls back
    to whatever the origin says, which after a save will be the inherited
    or default value.

    Membership is tested before reading, not folded into a
    `state.staged.get(path, ...)` default: `None` is a legitimate staged
    value (`chrome.url: null`), and a `get` default fires only on a
    missing key, so the two would become indistinguishable.
    """
    if path in state.staged:
        staged = state.staged[path]
        if staged is not UNSET:
            return staged
    origin = state.origins.get(path)
    return origin.value if origin is not None else None


def stage(state: EditorState, path: tuple[str, ...], value: object) -> EditorState:
    """Record an edit to `path`. Does not validate — that happens at save."""
    return replace(state, staged={**state.staged, path: value})


def toggle_current(state: EditorState) -> EditorState:
    """Flip the boolean under the cursor. A no-op on anything else."""
    spec = current(state)
    if spec is None or spec.kind is not FieldKind.BOOL:
        return state
    return stage(state, spec.path, not bool(effective(state, spec.path)))


def reset_current(state: EditorState, layer_raw: dict[str, object]) -> EditorState:
    """Reset the field under the cursor to whatever it inherits.

    Stages a deletion rather than writing the default out (spec 4.3): a
    written-out default freezes at today's value, while an inherited one
    keeps following jailbee's own. When the key is not in this layer at
    all there is nothing to delete, so any staged edit to it is simply
    discarded.
    """
    spec = current(state)
    if spec is None:
        return state
    present, _ = lookup(layer_raw, spec.path)
    staged = dict(state.staged)
    if present:
        staged[spec.path] = UNSET
    else:
        staged.pop(spec.path, None)
    return replace(state, staged=staged)


def changes(state: EditorState, layer_raw: dict[str, object]) -> tuple[YamlChange, ...]:
    """The staged edits that would actually alter the open layer's file.

    Two kinds of no-op are dropped here rather than left for the writer: a
    value equal to what the file already holds, and a reset of a key the
    file does not have. Without that, opening a file, toggling a setting
    twice and saving would produce a diff — and `patch_yaml`'s
    byte-identical guarantee would be unreachable in practice.

    Sorted by path so two identical edit sessions write identical files.
    """
    out: list[YamlChange] = []
    for path in sorted(state.staged):
        value = state.staged[path]
        present, existing = lookup(layer_raw, path)
        if value is UNSET:
            if present:
                out.append(YamlChange(path, DELETE))
            continue
        if present and existing == value:
            continue
        out.append(YamlChange(path, value))
    return tuple(out)


def is_dirty(state: EditorState, layer_raw: dict[str, object]) -> bool:
    """Whether saving would write anything. Drives the quit confirmation."""
    return bool(changes(state, layer_raw))
