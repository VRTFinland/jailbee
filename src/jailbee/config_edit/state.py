"""The editor's state machine: what is on screen and what has been staged.

Pure, following `dashboard_settings.py`. Every transition takes a state
and returns a new one; nothing here reads a key, draws a cell or touches
a file. That is what lets the whole interaction model — navigation,
search, staging, reset — be tested without a terminal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Literal

from jailbee.config_edit.layers import lookup
from jailbee.config_edit.schema import (
    COLLECTION_KINDS,
    FieldKind,
    build_specs,
    dotted,
    rebase,
)
from jailbee.config_writer import DELETE, KeyPath, YamlChange

if TYPE_CHECKING:
    from jailbee.config_edit.layers import LayerName, Origin
    from jailbee.config_edit.schema import FieldSpec


Crumb = str | int
"""One step of the editor's trail: a section name, a map key, or a list index."""


@dataclass(frozen=True)
class EditorState:
    """One open editor.

    `trail` is where the cursor is in the config tree: empty on the section
    list, `("ssh",)` inside a section, `("host_mounts", 1)` on one entry's
    form. It is the config path, so it doubles as the prefix every spec on
    screen is addressed by — see `screen`, the one place depth is
    interpreted.

    `staged` holds only the paths the user has changed, keyed the same way
    `FieldSpec.path` and `YamlChange.path` are, so the three never need
    translating between each other.
    """

    layer: LayerName
    specs: tuple[FieldSpec, ...]
    origins: Mapping[KeyPath, Origin]
    staged: Mapping[KeyPath, object]
    trail: tuple[Crumb, ...] = ()
    index: int = 0
    query: str = ""
    show_all: bool = False

    @property
    def section(self) -> str | None:
        """The open top-level key, or `None` on the section list.

        Kept as a property because `render.section_pane`, `render.field_pane`
        and `title_bar` ask exactly this question and should not learn about
        depth to answer it. Not a field: `dataclasses.replace` would then need
        it kept in step with `trail` at every call site.
        """
        if not self.trail:
            return None
        first = self.trail[0]
        return first if isinstance(first, str) else None


@dataclass(frozen=True)
class Screen:
    """What the field pane is showing, resolved from the trail.

    `specs` is the rows to draw for a `fields` or `entry` screen and is empty
    for the other two. `collection` is the collection spec on a `collection`
    screen and, on an `entry` screen, the collection the entry belongs to —
    `entry_path` then addresses the entry itself. Both are what lets `app.py`
    validate an entry against its item model without re-deriving the trail.

    Resolved rather than stored, so the trail stays the single source of truth
    and no transition can leave a cached screen behind.
    """

    kind: Literal["sections", "fields", "collection", "entry"]
    specs: tuple[FieldSpec, ...] = ()
    collection: FieldSpec | None = None
    entry_path: KeyPath = ()


def open_editor(
    *,
    layer: LayerName,
    specs: Sequence[FieldSpec],
    origins: Mapping[KeyPath, Origin],
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
        head = spec.path[0]
        # `build_specs` only ever emits string segments, so every top-level
        # key is a `str`; the `isinstance` is what tells mypy so now that
        # `FieldSpec.path` is a `KeyPath`.
        if isinstance(head, str) and head not in out:
            out.append(head)
    return tuple(out)


def _matches(spec: FieldSpec, query: str) -> bool:
    needle = query.casefold()
    haystack = (dotted(spec.path), spec.label, spec.description)
    return any(needle in field.casefold() for field in haystack)


def _spec_at(specs: Sequence[FieldSpec], path: KeyPath) -> FieldSpec | None:
    return next((s for s in specs if s.path == path), None)


def screen(state: EditorState) -> Screen:
    """What the trail points at. The one place depth is interpreted."""
    if state.query:
        return Screen("fields", tuple(s for s in state.specs if _matches(s, state.query)))
    if not state.trail:
        return Screen("sections")
    pool = state.specs
    prefix: KeyPath = ()
    holder: FieldSpec | None = None
    entry_path: KeyPath = ()
    i = 0
    while i < len(state.trail):
        prefix = (*prefix, state.trail[i])
        spec = _spec_at(pool, prefix)
        if spec is not None and spec.kind in COLLECTION_KINDS and spec.item_model is not None:
            if i + 1 == len(state.trail):
                return Screen("collection", (), spec)
            prefix = (*prefix, state.trail[i + 1])
            pool = rebase(build_specs(spec.item_model), prefix)
            holder, entry_path = spec, prefix
            i += 2
            continue
        i += 1
    rows = tuple(s for s in pool if s.path[: len(prefix)] == prefix)
    if holder is not None:
        return Screen("entry", rows, holder, entry_path)
    return Screen("fields", tuple(s for s in rows if state.show_all or not s.advanced))


def entries(state: EditorState, spec: FieldSpec) -> tuple[Crumb, ...]:
    """The crumbs addressing `spec`'s own entries, in document order.

    Indices for a `MODEL_LIST`, keys for a `MODEL_MAP`, nothing for a value
    that is neither — a hand-broken file must not crash the read path.
    """
    value = effective(state, spec.path)
    if isinstance(value, list):
        return tuple(range(len(value)))
    if isinstance(value, dict):
        return tuple(value)
    return ()


def visible_specs(state: EditorState) -> tuple[FieldSpec, ...]:
    """The fields the field pane shows right now.

    Search wins over everything: it spans all sections and **ignores the
    basic/advanced filter** (spec 4.3). At this schema size search is how
    a field actually gets found, and filtering its results would hide
    exactly what was being looked for.

    Empty on the two screens that do not draw fields at all: the section
    list, and a collection's entry list.
    """
    return screen(state).specs


def current(state: EditorState) -> FieldSpec | None:
    """The field under the cursor, or `None` while the section list has focus."""
    rows = visible_specs(state)
    if not rows or state.index >= len(rows):
        return None
    return rows[state.index]


def move(state: EditorState, delta: int) -> EditorState:
    """Move the cursor within the current list, clamped at both ends."""
    view = screen(state)
    if view.kind == "sections":
        total = len(sections(state))
    elif view.kind == "collection":
        total = len(entries(state, view.collection)) if view.collection is not None else 0
    else:
        total = len(view.specs)
    last = max(0, total - 1)
    return replace(state, index=max(0, min(last, state.index + delta)))


def enter_crumb(state: EditorState, crumb: Crumb) -> EditorState:
    """Descend one level, cursor at the top, clearing any active search.

    The cursor resets because two screens rarely have the same length:
    carrying an index across could leave it past the end of a shorter one.
    """
    return replace(state, trail=(*state.trail, crumb), index=0, query="")


def leave_crumb(state: EditorState) -> EditorState:
    """Ascend one level. On the section list this is already the top."""
    return replace(state, trail=state.trail[:-1], index=0, query="")


def set_query(state: EditorState, query: str) -> EditorState:
    """Set the search string. Empty restores the section list.

    The trail is cleared: search spans the top-level specs only, so a query
    run from inside an entry form would otherwise leave the trail pointing at
    a screen the results cannot describe (spec 11.3 rule 3).
    """
    return replace(state, query=query, trail=(), index=0)


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


def effective(state: EditorState, path: KeyPath) -> object:
    """The value the user currently sees for `path`.

    A staged edit wins over the resolved origin; a staged reset falls back
    to the origin.

    `state.origins` reports the layers **as saved**: it is resolved once
    in `open_editor` against the files on disk and never recomputed. So
    immediately after `reset_current` on a key the open layer holds, this
    returns the value being deleted, not the value the key will inherit
    once the delete is written. Recomputing origins against staged edits
    belongs to the UI plan, the same limitation `inherited_entries`
    carries for the same reason.

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


def stage(state: EditorState, path: KeyPath, value: object) -> EditorState:
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
    staged: dict[KeyPath, object] = dict(state.staged)
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
