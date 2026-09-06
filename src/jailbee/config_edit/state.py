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
    FieldKind,
    build_specs,
    dotted,
    is_drilldown,
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

    `origins` and `layer_raw` are the two layer facts the session carries, both
    resolved once when the editor opens and rebuilt together after a save
    (`app.Editor._reload`). They answer different questions and are not
    interchangeable:

    * `origins` is the **resolved** view — repo, else global, else the
      default, whichever layer supplies the value — and is what a field row's
      value and origin marker show.
    * `layer_raw` is the **open layer's own file**, and is what every question
      about what this layer holds, and what a save would write to it, reads:
      `own`, `changes`, `reset_current`, and through `own` every collection
      screen and every structural edit on one (spec 11.2).

    Reaching for `origins` to answer the second question is the mistake that
    `own`'s docstring spells out: `layers.resolve` consults the repo layer
    first whichever layer is open, so it cannot say whether *this* layer has
    a key.
    """

    layer: LayerName
    specs: tuple[FieldSpec, ...]
    origins: Mapping[KeyPath, Origin]
    layer_raw: dict[str, object]
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
    layer_raw: dict[str, object],
) -> EditorState:
    """A fresh editor on the section list with nothing staged.

    `origins` and `layer_raw` must describe the same `LayerSet` —
    `layers.resolve(specs, layer_set)` and `layers.raw_for(layer_set, layer)`.
    They are taken separately rather than derived from a `LayerSet` here so
    this module keeps no opinion on how the layers were read, but a fixture
    that pairs a hand-built `origins` with an unrelated `layer_raw` is
    describing a session that cannot exist.
    """
    return EditorState(
        layer=layer, specs=tuple(specs), origins=origins, layer_raw=layer_raw, staged={}
    )


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
        if spec is not None and is_drilldown(spec):
            if i + 1 == len(state.trail):
                return Screen("collection", (), spec)
            if spec.item_model is None:
                # A secret map's "entry" is one string, edited in a hidden
                # prompt (`app.Editor.enter`) — there is no form to descend
                # into, so the trail cannot go deeper than the map itself.
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
    """The crumbs addressing `spec`'s entries **in the open layer**, in order.

    Indices for a `MODEL_LIST`, keys for a `MODEL_MAP`, nothing for a value
    that is neither — a hand-broken file must not crash the read path.

    Through `own`, not `effective`: these crumbs are what the cursor addresses
    and what `add_entry`/`delete_entry`/`move_entry` renumber, and none of that
    is expressible for an entry the open layer does not have. A repo layer with
    no `host_mounts:` of its own inherits the global list whole, and `deep_merge`
    appends — so there is no repo-layer way to say "those two, minus one" (spec
    11.2). The inherited entries are shown, read-only and unaddressable, by
    `render.collection_pane` from `layers.inherited_entries`.
    """
    value = own(state, spec.path)
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


def reanchor(state: EditorState) -> EditorState:
    """Trim the trail to a screen the layers still have, and re-clamp the cursor.

    A save can delete the very entry — or the whole collection — the trail is
    standing in: `r` on `host_mounts` from a search hit stages the collection's
    deletion, and the user can then walk into the entry list (which still
    renders: `own` falls through an `UNSET` to the file) and stand on entry 1
    before pressing `s`. `app.Editor._reload` rebuilds the session from the
    file that save just wrote, and without this the trail still points at an
    entry that no longer exists: the form paints from an absent list, and the
    next edit stages a leaf whose integer segment addresses nothing —
    `layers.apply_changes` raises `index out of range` out of the key handler
    on the following save.

    One crumb at a time, re-probing after each: an entry can contain a
    collection of its own, so the stale crumb need not be the last one.
    `move(..., 0)` at the end re-clamps `index` against whatever screen is left,
    which is a different length from the one the cursor was measured against.
    """
    trail = state.trail
    while trail:
        probe = replace(state, trail=trail)
        view = screen(probe)
        if view.kind != "entry" or view.collection is None:
            break
        if view.entry_path[-1] in entries(probe, view.collection):
            break
        trail = trail[:-1]
    return move(replace(state, trail=trail), 0)


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


def _staged_at(state: EditorState, path: KeyPath) -> tuple[bool, object]:
    """`(resolved, value)` for `path` from the nearest staged ancestor, if any.

    The half `effective` and `own` share: both resolve a staged value the same
    way and differ only in what they fall through to. `resolved` is `False`
    both when nothing at or above `path` is staged and when the nearest staged
    ancestor is `UNSET` — a pending deletion has no value to report, so the
    caller falls through to whatever it reads as "saved".
    """
    for i in range(len(path), 0, -1):
        if path[:i] not in state.staged:
            continue
        staged = state.staged[path[:i]]
        if staged is UNSET:
            return False, None
        present, value = _dig(staged, path[i:])
        return True, (value if present else None)
    return False, None


def effective(state: EditorState, path: KeyPath) -> object:
    """The value a field row shows for `path`, from whichever layer supplies it.

    Resolved from the nearest staged ancestor, then from the saved layers. The
    nearest-ancestor rule is what makes a field read correct in all three cases
    that can hold at once: the field itself staged, a structure staged around
    it, or neither.

    A staged `UNSET` at any level means "this will be deleted", so the reader
    falls through to what the layers say — the same fall-through the top-level
    case has always had.

    `state.origins` reports the layers **as saved** (spec 10.1 option b) and is
    resolved once in `open_editor`. That is unchanged: this function walks
    *into* an origin's value, it never adds a key to the map.

    Reading through to another layer is right here and only here: a repo-layer
    `host_mounts` row with no repo key of its own should say "2 entries
    (global)", the same way every other inherited field's row does. Every
    question about what *this layer* holds — which is every collection screen
    and every structural edit on one — goes through `own` instead.
    """
    resolved, value = _staged_at(state, path)
    if resolved:
        return value
    for i in range(len(path), 0, -1):
        origin = state.origins.get(path[:i])
        if origin is None:
            continue
        present, found = _dig(origin.value, path[i:])
        return found if present else None
    return None


def own(state: EditorState, path: KeyPath) -> object:
    """The value the **open layer** has at `path`, staged edits folded in.

    `effective`'s sibling; the whole difference is the fall-through. Where
    `effective` drops into `state.origins` — repo, else global, else the
    default — this drops into the open layer's own file and nothing else.
    They agree whenever the open layer supplies the value and differ exactly
    when it does not.

    That distinction is spec 11.2 in one function. `deep_merge` appends lists,
    so a repo layer with no `host_mounts:` of its own still *sees* global's
    entries — but it cannot address one, edit one, or delete one: there is no
    repo-layer expression for "global's list minus that entry". A collection
    screen that listed them as its own rows would be offering exactly the
    operations the merge cannot express, and `changes` would then emit paths
    into a list this layer does not have (`apply_changes` raises "index out of
    range" on the first save).

    `state.origins` cannot answer this question, however tempting the
    shortcut: `layers.resolve` consults the repo layer first whichever layer
    is open, so a global-layer session on a key the repo config also sets
    would be told `source == "repo"` and handed the repo's entries.
    """
    resolved, value = _staged_at(state, path)
    if resolved:
        return value
    present, found = lookup(state.layer_raw, path)
    return found if present else None


def entry_origin(state: EditorState, path: KeyPath) -> Literal["set", "default"]:
    """Whether an entry actually carries this key, or falls back to the model.

    The three-layer marker (`repo`/`global`/`default`) does not apply inside an
    entry: the entry as a whole came from one layer — the open one, since that
    is the only layer whose entries `entries()` reports — so the only question
    left is whether the key is written in it (spec 11.4).

    Which is why the saved-layer half reads `state.layer_raw` rather than
    `state.origins`: an entry that exists on this screen is this layer's, and
    `origins` would answer for the repo layer even in a global-layer session.
    """
    for i in range(len(path), 0, -1):
        if path[:i] not in state.staged:
            continue
        if state.staged[path[:i]] is UNSET:
            break
        present, _ = _dig(state.staged[path[:i]], path[i:])
        return "set" if present else "default"
    present, _ = lookup(state.layer_raw, path)
    return "set" if present else "default"


def _staged_ancestor(state: EditorState, path: KeyPath) -> KeyPath | None:
    """The nearest strict ancestor of `path` that is staged at all, if any.

    **`UNSET` counts.** It used to be skipped, on the reasoning that a pending
    deletion has no structure to fold an edit into — but that left the exact
    pair the invariant forbids reachable from the shipped UI: search
    `host_mounts`, press `r` (staging `UNSET` at the collection), walk into the
    entry list (which still renders, because `own` falls through an `UNSET`
    to what the open layer's file says) and type into a field. The leaf and the
    `UNSET` collection then sat in `staged` together, the screen showed the
    typed value, and `changes` emitted the delete alone.

    Skipping it here is what made that possible, so it no longer does; `stage`
    handles the `UNSET` case by materialising the ancestor.
    """
    for i in range(len(path) - 1, 0, -1):
        if path[:i] in state.staged:
            return path[:i]
    return None


def _under(path: KeyPath, prefix: KeyPath) -> bool:
    """Whether `path` is a strict descendant of `prefix`."""
    return len(path) > len(prefix) and path[: len(prefix)] == prefix


def _sort_key(path: KeyPath) -> tuple[tuple[int, str | int], ...]:
    """A total order over paths that mix mapping keys with list indices.

    Plain `sorted` raises `TypeError` the moment one path has an `int` where
    another has a `str` in the same position — a shape clash under one prefix,
    which a hand-broken file can produce. Indices sort before keys at the same
    depth and among themselves numerically, so `host_mounts.10` follows
    `host_mounts.2` rather than preceding it the way a stringified sort would.
    """
    return tuple((0, seg) if isinstance(seg, int) else (1, seg) for seg in path)


def _materialised(state: EditorState, prefix: KeyPath) -> list[object] | dict[str, object] | None:
    """The structure at `prefix` as the open layer would save it: copied, whole.

    Starts from `own`, which resolves the nearest staged ancestor and falls
    through an `UNSET` to what the **open layer's file** says. Then every
    staged path strictly under `prefix` is replayed onto it, because `own`
    resolves from the nearest staged *ancestor* and so never sees the leaves
    below one.

    `own` rather than `effective`, and that is load-bearing: every caller is
    about to stage the result back into this layer, so an inherited entry
    folded in here would be written into the file as if the user had typed it
    (spec 11.2). It is also the value a collection screen draws, so the two
    cannot disagree.

    `None` when the value is neither a list nor a mapping: there is nothing to
    plant into, and inventing a shape would overwrite a hand-broken file's
    content with a guess.

    Copied because the caller mutates it, and `state.staged` and
    `state.layer_raw` both hold structures shared with every other state.
    """
    value = own(state, prefix)
    if not isinstance(value, (list, dict)):
        return None
    out: list[object] | dict[str, object] = deepcopy(value)
    for path in sorted((p for p in state.staged if _under(p, prefix)), key=_sort_key):
        rest = path[len(prefix) :]
        if state.staged[path] is UNSET:
            _uproot(out, rest)
        else:
            _plant(out, rest, state.staged[path])
    return out


def _stage_structure(state: EditorState, path: KeyPath, value: object) -> EditorState:
    """Stage a value at `path`, dropping every staged path inside it.

    `stage`'s private tail, and with `_stage_reset` the only place a key is
    ever added to `staged` — so the invariant is enforced in two small
    functions rather than at each of the six transitions. The guiding rule,
    which both directions of it follow from:

        The later, more explicit instruction wins. Editing inside a collection
        cancels a pending reset of it; resetting a collection discards pending
        edits inside it. Either way, a leaf and its own staged ancestor never
        coexist.

    Dropping the descendants is not a detail. Left behind, they would hand
    `changes` paths whose integer segments address the list that just stopped
    existing — `_superseded` would drop them, losing edits the user can still
    read back out of the structure. Anything already at `path`, `UNSET`
    included, is overwritten: that is what makes an edit inside a collection
    cancel a pending reset of it.

    Callers that want the old descendants *preserved* rather than discarded
    fold them into `value` first, with `_materialised`.
    """
    staged = {key: val for key, val in state.staged.items() if not _under(key, path)}
    staged[path] = value
    return replace(state, staged=staged)


def _stage_reset(state: EditorState, path: KeyPath, *, present: bool) -> EditorState:
    """Stage a reset of `path`, discarding every staged edit inside it.

    The mirror of the `UNSET`-ancestor case in `stage`, and the other half of
    the rule quoted in `_stage_structure`. The user has just said "delete this
    whole key from this layer"; edits inside it are then meaningless, so
    discarding them honours the instruction rather than losing work. Keeping
    them would be the forbidden pair — and `changes` would emit the delete
    alone, so they would be discarded anyway, just silently and one layer
    further down where nothing could explain it.

    `present` is whether the open layer's file actually has the key. When it
    does not there is nothing to delete, so no `UNSET` is staged and any edit
    to the key is simply dropped (spec 4.3) — the descendants go all the same,
    because "reset this collection" cannot sensibly leave edits pending inside
    the collection it just reset.
    """
    staged = {key: val for key, val in state.staged.items() if not _under(key, path)}
    if present:
        staged[path] = UNSET
    else:
        staged.pop(path, None)
    return replace(state, staged=staged)


def stage(state: EditorState, path: KeyPath, value: object) -> EditorState:
    """Record an edit to `path`. Does not validate — that happens at save.

    **A leaf and a staged ancestor of it never coexist in `staged`.** `changes`
    drops a leaf that sits under a staged ancestor (`_superseded`), so keeping
    both would throw the edit away at save time while the screen still shows
    it — `effective` reads the leaf back happily. That is not hypothetical: `n`
    on a collection stages the *whole* collection, so every field the user then
    fills in on the new entry would be a leaf under it.

    So when anything above is staged, the edit is folded into a materialised
    copy of that ancestor and the ancestor is re-staged; only when nothing
    above is staged does `path` get a key of its own.

    When the ancestor is staged `UNSET`, folding **materialises** it: the
    pending reset is dropped and the collection the screen is showing is staged
    with the edit in it. Editing inside a collection cancels a pending reset of
    that collection — the least surprising reading, because the reset is
    invisible at that depth: the user is looking at the entries and typing into
    one, which is the more recent and the more specific instruction. Whether to
    *say* so belongs to `app.py`; this module is pure.
    """
    ancestor = _staged_ancestor(state, path)
    if ancestor is None:
        # Through `_stage_structure`, not a bare dict write: `path` may itself
        # have staged descendants — write the whole of a collection whose entry
        # you edited a moment ago and they would be the forbidden pair the
        # other way up. A leaf has no descendants, so for the common case this
        # is the same dict write it always was.
        return _stage_structure(state, path, value)
    folded = _materialised(state, ancestor)
    if folded is None or not _plant(folded, path[len(ancestor) :], value):
        # Loud, not silent. Falling back to staging the bare leaf would rebuild
        # the forbidden pair and lose this edit at save time with nothing on
        # screen to say so — the very bug this function exists to prevent. No
        # spec the editor builds today reaches here; a future one that does
        # should find out at once.
        raise ValueError(
            f"cannot fold an edit to {dotted(path)} into the staged "
            f"{dotted(ancestor)}: it has no such place to write to"
        )
    return _stage_structure(state, ancestor, folded)


def current_value(state: EditorState, path: KeyPath) -> object:
    """The value the screen the cursor is on shows for `path`: `own` or `effective`.

    The single place that choice is made for a path the **user is pointing
    at**, so a keystroke that reads a value (`toggle_current`,
    `app.edit_current`'s seed) and the row that paints it cannot answer
    differently. They did once: `render._now` was converted to `own` for an
    entry screen while these two still read `effective`, so on a global-layer
    session whose repo config also set the collection, the row displayed
    global's value and `Enter` pre-filled the repo's — committing wrote one
    layer's value into the other's file.

    Inside an entry the answer is `own`: the entry belongs to the open layer,
    because `entries` reports no other layer's. Everywhere else it is
    `effective`, which is right for a field row — an inherited value is what
    that field *is*, and editing it is how you create a key of your own.
    """
    return own(state, path) if screen(state).kind == "entry" else effective(state, path)


def toggle_current(state: EditorState) -> EditorState:
    """Flip the boolean under the cursor. A no-op on anything else."""
    spec = current(state)
    if spec is None or spec.kind is not FieldKind.BOOL:
        return state
    return stage(state, spec.path, not bool(current_value(state, spec.path)))


def reset_current(state: EditorState) -> EditorState:
    """Reset the field under the cursor to whatever it inherits.

    Stages a deletion rather than writing the default out (spec 4.3): a
    written-out default freezes at today's value, while an inherited one
    keeps following jailbee's own. When the key is not in this layer at
    all there is nothing to delete, so any staged edit to it is simply
    discarded.

    Resetting a *collection* discards the pending edits inside it as well —
    see `_stage_reset` for why that is honouring the instruction rather than
    losing work. The cursor reaches a collection's own row from the section
    list and from a search hit, neither of which needs a collection screen, so
    this is not a corner case.
    """
    spec = current(state)
    if spec is None:
        return state
    ancestor = _staged_ancestor(state, spec.path)
    if ancestor is not None:
        # The key does not live in the file, it lives inside a structure that
        # is already staged — a collection staged by `n`/`d`/reorder, or one
        # staged `UNSET` by an `r` further up. So "reset" here is removing the
        # key from that structure, which drops the entry back to the item
        # model's default. Staging `UNSET` at the leaf instead would break the
        # invariant `stage` maintains and be dropped as superseded, making `r`
        # silently do nothing.
        #
        # `_materialised` resolves an `UNSET` ancestor the same way `stage`
        # does: a reset of one field *inside* a collection is still an edit
        # inside it, so it cancels the collection's own pending reset rather
        # than being swallowed by it.
        pruned = _materialised(state, ancestor)
        if pruned is None or not _uproot(pruned, spec.path[len(ancestor) :]):
            # A no-op rather than `stage`'s raise: this is a keystroke on
            # whatever the cursor happens to be on, and a hand-broken file must
            # not crash the TUI. Nothing is staged, so the invariant holds.
            return state
        return stage(state, ancestor, pruned)
    present, _ = lookup(state.layer_raw, spec.path)
    return _stage_reset(state, spec.path, present=present)


def _collection_value(state: EditorState, spec: FieldSpec) -> list[object] | dict[str, object]:
    """This collection as it stands now: staged paths folded in, and copied.

    `_materialised` does the work — the same value `stage` folds an edit into,
    which is the value the screen is showing. The only thing added here is the
    empty fallback for a collection the open layer's own value of which is
    neither a list nor a mapping — absent (the repo layer inherits global's
    entries whole and has none of its own), or a hand-broken file:
    `spec.kind` says which empty.

    The fold happens **before** the caller's structural change, so an edit
    travels with its entry: editing entry 1 and then deleting entry 0 saves
    that edit on what is now entry 0, rather than on whichever entry inherited
    index 1. Without it, `changes` would drop the leaf as superseded and the
    edit would vanish while the screen went on showing it.
    """
    out = _materialised(state, spec.path)
    if out is None:
        return [] if spec.kind is FieldKind.MODEL_LIST else {}
    return out


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

    Staged through `stage` rather than written straight into `staged`, so that
    a collection which is *itself* nested inside an already-staged structure
    gets folded into it instead of becoming its descendant. `delete_entry` and
    `move_entry` go the same way, for the same reason.
    """
    value = _collection_value(state, spec)
    if isinstance(value, dict):
        if key is None:
            raise ValueError("a model-map entry needs a key name")
        value[key] = {}
        return stage(state, spec.path, value), key
    value.append({})
    return stage(state, spec.path, value), len(value) - 1


def delete_entry(state: EditorState, spec: FieldSpec, crumb: Crumb) -> EditorState:
    """Remove one entry, staging the collection that remains.

    A crumb that addresses nothing is a no-op: staging a collection identical
    to the one already there would light up the `modified` counter for an edit
    that does not exist.

    Note that this does **not** move the trail. A caller that deletes the entry
    the trail is standing in must walk out of it first: `stage` now raises
    rather than silently losing an edit written to an index that no longer
    exists.
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
    return stage(state, spec.path, value)


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
    return stage(state, spec.path, value)


def discard_under(state: EditorState, prefix: KeyPath) -> EditorState:
    """Drop every staged edit at or below `prefix`.

    `app.py`'s second `Esc` out of an entry that already exists (as opposed
    to one `n` created this session, which is removed outright by
    `delete_entry`): the entry stays, but nothing typed into it this session
    is kept. `_under` alone would miss `prefix` itself — a whole-entry
    `UNSET` or a materialised replacement staged directly at `prefix` — so
    this checks the prefix match directly rather than reusing it.
    """
    kept = {p: v for p, v in state.staged.items() if p[: len(prefix)] != prefix}
    return replace(state, staged=kept)


def _superseded(path: KeyPath, staged: Mapping[KeyPath, object]) -> bool:
    """Whether a strict ancestor of `path` is staged, making `path` moot.

    Belt and braces: `stage`, `reset_current`, `_collection_value` and
    `_stage_structure` between them uphold the invariant that a leaf never
    shares `staged` with an ancestor of itself, so no transition the editor
    offers *should* reach this. That claim was already wrong once — while
    `_staged_ancestor` skipped `UNSET`, `r` on a collection followed by typing
    into one of its entries produced the pair, and this line is what silently
    threw the typing away. So it is a symptom, never a fix: if it fires, the
    transition that staged the pair is the bug.

    It stays because the alternative failure is worse than a lost edit. The
    leaf's integer segments address the list that stopped existing, so letting
    it through would write the edit onto whichever entry happened to inherit
    that index — a wrong write rather than a missing one.
    """
    return any(path[:i] in staged for i in range(1, len(path)))


def changes(state: EditorState) -> tuple[YamlChange, ...]:
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
        present, existing = lookup(state.layer_raw, path)
        if value is UNSET:
            if present:
                out.append(YamlChange(path, DELETE))
            continue
        if present and existing == value:
            continue
        out.append(YamlChange(path, value))
    return tuple(out)


def is_dirty(state: EditorState) -> bool:
    """Whether saving would write anything. Drives the quit confirmation."""
    return bool(changes(state))
