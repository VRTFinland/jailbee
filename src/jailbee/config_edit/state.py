"""The editor's state machine: what is on screen and what has been staged.

Pure, following `dashboard_settings.py`. Every transition takes a state
and returns a new one; nothing here reads a key, draws a cell or touches
a file. That is what lets the whole interaction model — navigation,
search, staging, reset — be tested without a terminal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
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


def _dig(value: object, path: KeyPath) -> tuple[bool, object]:
    """`(present, value)` for `path` inside an already-loaded value.

    The in-memory twin of `layers.lookup`, but rooted at any value rather than
    at a mapping: a staged collection is a *list*, and `effective` has to be
    able to walk into one.
    """
    node = value
    for key in path:
        if isinstance(key, int):
            if not isinstance(node, list) or not 0 <= key < len(node):
                return False, None
            node = node[key]
            continue
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _plant(root: object, path: KeyPath, value: object) -> bool:
    """Write `value` at `path` inside an already-loaded structure, in place.

    The write twin of `_dig`, and the half of the "a leaf and a staged ancestor
    of it never coexist" invariant that `stage` and `_collection_value` lean
    on: an edit under a staged structure is folded *into* that structure rather
    than kept as a staged key of its own.

    Missing mapping levels are created on the way down. A missing **list
    index** is not — inventing one would shift every entry after it — so the
    walk gives up and returns `False`, leaving the caller to fall back. `root`
    is mutated, so hand it a copy.
    """
    if not path:
        return False
    node: object = root
    for depth, key in enumerate(path[:-1]):
        fresh: dict[str, object] | None = None if isinstance(path[depth + 1], int) else {}
        child: object
        if isinstance(key, int):
            if not isinstance(node, list) or not 0 <= key < len(node):
                return False
            child = node[key]
            if not isinstance(child, (dict, list)):
                if fresh is None:
                    return False
                node[key] = child = fresh
        else:
            if not isinstance(node, dict):
                return False
            child = node.get(key)
            if not isinstance(child, (dict, list)):
                if fresh is None:
                    return False
                node[key] = child = fresh
        node = child
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(node, list) or not 0 <= last < len(node):
            return False
        node[last] = value
        return True
    if not isinstance(node, dict):
        return False
    node[last] = value
    return True


def _uproot(root: object, path: KeyPath) -> bool:
    """Remove `path` from an already-loaded structure, in place.

    Only a mapping key is removable. Dropping a **list index** renumbers every
    entry after it — that is `delete_entry`'s job, not a field reset's — so a
    path ending in an index is refused rather than quietly reinterpreted.
    """
    if not path or isinstance(path[-1], int):
        return False
    present, holder = _dig(root, path[:-1])
    if not present or not isinstance(holder, dict):
        return False
    holder.pop(path[-1], None)
    return True


def effective(state: EditorState, path: KeyPath) -> object:
    """The value the user currently sees for `path`.

    Resolved from the nearest staged ancestor, then from the saved layers. The
    nearest-ancestor rule is what makes an entry read correct in all three
    cases that can hold at once: the field itself staged, the whole collection
    staged around it, or neither.

    A staged `UNSET` at any level means "this will be deleted", so the reader
    falls through to what the layers say — the same fall-through the top-level
    case has always had.

    `state.origins` reports the layers **as saved** (spec 10.1 option b) and is
    resolved once in `open_editor`. That is unchanged: this function walks
    *into* an origin's value, it never adds a key to the map.
    """
    for i in range(len(path), 0, -1):
        if path[:i] not in state.staged:
            continue
        staged = state.staged[path[:i]]
        if staged is UNSET:
            break
        present, value = _dig(staged, path[i:])
        return value if present else None
    for i in range(len(path), 0, -1):
        origin = state.origins.get(path[:i])
        if origin is None:
            continue
        present, value = _dig(origin.value, path[i:])
        return value if present else None
    return None


def entry_origin(state: EditorState, path: KeyPath) -> Literal["set", "default"]:
    """Whether an entry actually carries this key, or falls back to the model.

    The three-layer marker (`repo`/`global`/`default`) does not apply inside an
    entry: the entry as a whole came from one layer, so the only question left
    is whether the key is written in it (spec 11.4).
    """
    for i in range(len(path) - 1, 0, -1):
        if path[:i] in state.staged and state.staged[path[:i]] is not UNSET:
            present, _ = _dig(state.staged[path[:i]], path[i:])
            return "set" if present else "default"
    if path in state.staged and state.staged[path] is not UNSET:
        return "set"
    for i in range(len(path) - 1, 0, -1):
        origin = state.origins.get(path[:i])
        if origin is None:
            continue
        present, _ = _dig(origin.value, path[i:])
        return "set" if present else "default"
    return "default"


def _staged_ancestor(state: EditorState, path: KeyPath) -> KeyPath | None:
    """The nearest strict ancestor of `path` staged as a real value, if any.

    `UNSET` does not count: it means "delete this key", so there is no
    structure under it to fold an edit into.
    """
    for i in range(len(path) - 1, 0, -1):
        if path[:i] in state.staged and state.staged[path[:i]] is not UNSET:
            return path[:i]
    return None


def stage(state: EditorState, path: KeyPath, value: object) -> EditorState:
    """Record an edit to `path`. Does not validate — that happens at save.

    **A leaf and a staged ancestor of it never coexist in `staged`.** `changes`
    drops a leaf that sits under a staged ancestor (`_superseded`), so keeping
    both would throw the edit away at save time while the screen still shows
    it — `effective` reads the leaf back happily. That is not hypothetical: `n`
    on a collection stages the *whole* collection, so every field the user then
    fills in on the new entry would be a leaf under it.

    So when an ancestor is already staged the value is written into a copy of
    it and the ancestor is re-staged; only when nothing above is staged does
    `path` get a key of its own. The copy is not optional: `replace` hands the
    same staged objects to every state derived from this one, so mutating one
    in place would rewrite the history the app still holds.
    """
    ancestor = _staged_ancestor(state, path)
    if ancestor is not None:
        folded = deepcopy(state.staged[ancestor])
        if _plant(folded, path[len(ancestor) :], value):
            return replace(state, staged={**state.staged, ancestor: folded})
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
    ancestor = _staged_ancestor(state, spec.path)
    if ancestor is not None:
        # The key does not live in the file, it lives inside a structure that
        # is already staged — a collection staged by `n`/`d`/reorder, say. So
        # "reset" here is removing it from that structure, which drops the
        # entry back to the item model's default. Staging `UNSET` at the leaf
        # would break the invariant `stage` maintains and be dropped as
        # superseded, making `r` silently do nothing.
        pruned = deepcopy(state.staged[ancestor])
        if not _uproot(pruned, spec.path[len(ancestor) :]):
            return state
        return replace(state, staged={**state.staged, ancestor: pruned})
    present, _ = lookup(layer_raw, spec.path)
    staged: dict[KeyPath, object] = dict(state.staged)
    if present:
        staged[spec.path] = UNSET
    else:
        staged.pop(spec.path, None)
    return replace(state, staged=staged)


def _sort_key(path: KeyPath) -> tuple[tuple[int, str | int], ...]:
    """A total order over paths that mix mapping keys with list indices.

    Plain `sorted` raises `TypeError` the moment one path has an `int` where
    another has a `str`. Indices sort before keys at the same depth and among
    themselves numerically, so `host_mounts.10` follows `host_mounts.2` rather
    than preceding it the way a stringified sort would.
    """
    return tuple((0, seg) if isinstance(seg, int) else (1, seg) for seg in path)


def _under(path: KeyPath, prefix: KeyPath) -> bool:
    """Whether `path` is a strict descendant of `prefix`."""
    return len(path) > len(prefix) and path[: len(prefix)] == prefix


def _collection_value(state: EditorState, spec: FieldSpec) -> list[object] | dict[str, object]:
    """This collection as it stands now: staged leaves folded in, and copied.

    **Copied** because the caller mutates it, and both `state.staged` and
    `state.origins` hold structures shared with every other state.

    **Folded** because the caller is about to stage the whole collection, and
    `changes` treats a leaf under a staged ancestor as superseded: a leaf edit
    left outside would be silently dropped from the save while the screen went
    on showing it. `effective` cannot do this for us — it resolves from the
    nearest staged *ancestor* and so never sees the leaves below one. Together
    with `_stage_collection` (which drops those leaves) and `stage` (which
    never creates one under a staged ancestor) this is the invariant that keeps
    a user's typing alive across a structural edit.

    The fold happens **before** the structural change, so an edit travels with
    its entry: editing entry 1 and then deleting entry 0 saves that edit on
    what is now entry 0, rather than on whichever entry inherited index 1.
    """
    value = effective(state, spec.path)
    out: list[object] | dict[str, object]
    if isinstance(value, list):
        out = deepcopy(value)
    elif isinstance(value, dict):
        out = deepcopy(value)
    else:
        out = [] if spec.kind is FieldKind.MODEL_LIST else {}
    for path in sorted((p for p in state.staged if _under(p, spec.path)), key=_sort_key):
        rest = path[len(spec.path) :]
        if state.staged[path] is UNSET:
            _uproot(out, rest)
        else:
            _plant(out, rest, state.staged[path])
    return out


def _stage_collection(state: EditorState, path: KeyPath, value: object) -> EditorState:
    """Stage a whole collection, dropping the staged leaves now folded into it.

    The other half of the invariant `stage` documents. Leaving the leaves
    behind would hand `changes` paths whose integer segments address the list
    that just stopped existing — `_superseded` would drop them, losing edits
    the user can still read back out of the collection.
    """
    staged = {key: val for key, val in state.staged.items() if not _under(key, path)}
    staged[path] = value
    return replace(state, staged=staged)


def add_entry(
    state: EditorState, spec: FieldSpec, key: str | None = None
) -> tuple[EditorState, Crumb]:
    """Append an empty entry and return the crumb that addresses it.

    Empty rather than the item model's defaults written out: a written-out
    default freezes at today's value while an absent key keeps following
    jailbee's own (spec 4.3), and the entry form shows every default anyway,
    marked `(default)`. Required fields therefore start empty, and `Esc`
    refuses to leave until they are filled (spec 11.8).

    The whole collection is staged, not just the new entry: adding is
    structural, so the integer segments of any path under it move.
    """
    value = _collection_value(state, spec)
    if isinstance(value, dict):
        if key is None:
            raise ValueError("a model-map entry needs a key name")
        value[key] = {}
        return _stage_collection(state, spec.path, value), key
    value.append({})
    return _stage_collection(state, spec.path, value), len(value) - 1


def delete_entry(state: EditorState, spec: FieldSpec, crumb: Crumb) -> EditorState:
    """Remove one entry, staging the collection that remains.

    A crumb that addresses nothing is a no-op: staging a collection identical
    to the one already there would light up the `modified` counter for an edit
    that does not exist.
    """
    value = _collection_value(state, spec)
    if isinstance(value, dict):
        if str(crumb) not in value:
            return state
        del value[str(crumb)]
    elif isinstance(crumb, int) and 0 <= crumb < len(value):
        del value[crumb]
    else:
        return state
    return _stage_collection(state, spec.path, value)


def move_entry(state: EditorState, spec: FieldSpec, index: int, delta: int) -> EditorState:
    """Swap a list entry with its neighbour. A no-op past either end.

    Offered on every list, not only `autostart` (spec 11.7): a YAML list is
    ordered whether or not the loader cares, and it is the same three lines.
    A model map has no order to change, so this returns the state untouched.
    """
    value = _collection_value(state, spec)
    if not isinstance(value, list):
        return state
    target = index + delta
    if not (0 <= index < len(value) and 0 <= target < len(value)):
        return state
    value[index], value[target] = value[target], value[index]
    return _stage_collection(state, spec.path, value)


def _superseded(path: KeyPath, staged: Mapping[KeyPath, object]) -> bool:
    """Whether a strict ancestor of `path` is staged, making `path` moot.

    Belt and braces: with `stage`, `_collection_value` and `_stage_collection`
    all upholding the invariant that a leaf never shares `staged` with an
    ancestor of itself, this can no longer fire from any transition the editor
    offers. It stays because the consequence of a future transition forgetting
    the invariant is a wrong write, not a crash: the leaf's integer segments
    would address the list that stopped existing, and the edit would land on
    whichever entry happened to inherit that index. Dropping it here is the
    safe failure. If this ever does fire, the transition that staged the pair
    is the bug — not this line.
    """
    return any(path[:i] in staged for i in range(1, len(path)))


def changes(state: EditorState, layer_raw: dict[str, object]) -> tuple[YamlChange, ...]:
    """The staged edits that would actually alter the open layer's file.

    Two kinds of no-op are dropped here rather than left for the writer: a
    value equal to what the file already holds, and a reset of a key the
    file does not have. Without that, opening a file, toggling a setting
    twice and saving would produce a diff — and `patch_yaml`'s
    byte-identical guarantee would be unreachable in practice.

    Sorted by path so two identical edit sessions write identical files —
    through `_sort_key`, because a path into a collection mixes indices with
    keys and plain `sorted` cannot compare those.
    """
    out: list[YamlChange] = []
    for path in sorted(state.staged, key=_sort_key):
        if _superseded(path, state.staged):
            continue
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
