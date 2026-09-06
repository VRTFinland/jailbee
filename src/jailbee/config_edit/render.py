"""EditorState -> prompt_toolkit fragments.

Pure: every function here takes a state and returns text, so what the editor
draws is testable without a terminal — the same split
`dashboard_settings.render_settings` uses on the Rich side. `app.py` owns the
`Application`; this module owns every character it paints.

Origin markers follow spec 10.1 option (b): `state.origins` describes the
layers **as saved on disk** and is never recomputed while edits are staged, so
a row shows what the file says and then what the save will change it to
(`(repo) → reset`). The set of marked rows comes from `state.changes()`, not
from `state.staged`, so the marker and the `modified: N` counter cannot
disagree — an edit that restores a value to what the file already holds is no
edit at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from prompt_toolkit.styles import Style

from jailbee.config_edit.layers import disabled_reason, inherited_entries, lookup, raw_for
from jailbee.config_edit.schema import FieldKind, dotted, is_drilldown
from jailbee.config_edit.state import (
    UNSET,
    changes,
    current,
    effective,
    entries,
    entry_origin,
    screen,
    sections,
    visible_specs,
)
from jailbee.config_edit.values import format_value

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from jailbee.config_edit.layers import LayerName, LayerSet
    from jailbee.config_edit.schema import FieldSpec
    from jailbee.config_edit.state import Crumb, EditorState
    from jailbee.config_writer import KeyPath

_ORIGIN_LABEL = {
    "default": "(default)",
    "global": "(global)",
    "repo": "(repo)",
    "set": "(set)",
}

_VALUE_WIDTH = 22

_SECRET_MASK = "••••••"
"""A secret map entry's value column, inside its own drill-down screen.

Fixed, not sized to the value: `format_value`'s "N entries (hidden)" is right
for the field's own row (a count is safe to reveal), but here each row is one
key's token, and the length of a token is information too — a mask that grew
or shrank with the real value would leak exactly the length it is supposed to
hide.
"""

_NOT_STAGED: Final = object()
"""`state.staged.get` default: `None` and `UNSET` are both real staged values,
so absence needs a sentinel of its own (the same reason `state.effective` uses
`in` rather than a default)."""

EDITOR_STYLE = Style(
    [
        ("title", "reverse bold"),
        ("footer", "reverse"),
        ("cursor", "bold"),
        ("section-open", "bold"),
        ("staged", "ansiyellow bold"),
        ("disabled", "ansibrightblack"),
        ("dim", "ansibrightblack"),
        ("error", "ansired bold"),
        ("notice", "ansigreen"),
    ]
)


@dataclass(frozen=True)
class Pane:
    """One scrollable list: what to draw, and which line the cursor is on.

    `cursor_row` is what `app.py` feeds to `FormattedTextControl`'s
    `get_cursor_position`, which is how prompt_toolkit decides how far to
    scroll. Returning it alongside the fragments keeps the two from drifting —
    a header line added here without adjusting the offset would otherwise
    scroll to the wrong row.
    """

    fragments: StyleAndTextTuples
    cursor_row: int


def edit_block(spec: FieldSpec, layer: LayerName) -> str | None:
    """Why `spec` cannot be edited here, or `None` when it can.

    The editor's single gate: `app.py` refuses to open an editor when this
    returns a string, and `field_pane` greys the row and shows the reason.
    Two causes: a config rule (`layers.disabled_reason`: an `OPAQUE` free-form
    block, a key the loader bans from a repo config) and a deliberate refusal
    (a secret with no drill-down screen of its own — a scalar secret has
    nowhere safe to be edited, so it stays refused). A collection of models
    used to be a third cause; it now has its own drill-down screen
    (`collection_pane`/`body_pane`, spec 11.2/11.7), so it is no longer
    refused here. A secret **map** (`github.api_tokens`) follows the same
    path since Task 9: `is_drilldown` is true for it too, so this falls
    through to `None` and `app.Editor.enter` opens the map's own screen
    instead — masked keys, a hidden-input prompt, never a value on screen.
    """
    reason = disabled_reason(spec, layer)
    if reason is not None:
        return reason
    if spec.secret and not is_drilldown(spec):
        return (
            "Secrets are not editable here — the editor will not paint a token on a "
            "terminal. Edit the file by hand and keep it at mode 0600."
        )
    return None


def _pending(state: EditorState, layer_set: LayerSet) -> frozenset[KeyPath]:
    """Paths whose staged value would actually alter the file."""
    return frozenset(c.path for c in changes(state, raw_for(layer_set, state.layer)))


def _entry_pending(state: EditorState, layer_set: LayerSet, spec: FieldSpec) -> bool:
    """Whether one entry field differs from what the open layer's file holds.

    `_pending` is `changes()`'s own paths, and `changes()` folds a leaf to its
    nearest staged ancestor (state's "a leaf and its own staged ancestor never
    coexist" invariant, task 5) before it ever produces a `YamlChange` — a
    field edited inside an already-staged collection (after `n`, or after any
    edit that made the collection itself staged) is folded *into* that
    collection and never gets a `YamlChange`, hence never a path, of its own.
    `spec.path in pending` can therefore never mark such a row, even though
    the field plainly changed.

    This asks the same question directly, at leaf granularity, against the
    open layer's raw file rather than against `changes()`'s folded output —
    `effective` already resolves the nearest staged ancestor (materialised or
    not) the same way the screen does, so comparing it to what is actually on
    disk at this exact path is enough; no separate "is this row inside a
    staged collection" case is needed.
    """
    raw = raw_for(layer_set, state.layer)
    present, saved = lookup(raw, spec.path)
    return effective(state, spec.path) != (saved if present else None)


def _row_name(state: EditorState, spec: FieldSpec) -> str:
    """The label a row shows: dotted while searching, bare inside a section.

    Search spans every section, so a bare `enabled` there would name four
    different fields identically.
    """
    return dotted(spec.path) if state.query else spec.label


def _staged_suffix(state: EditorState, spec: FieldSpec) -> str:
    """What the save will do to this row, or `""` when nothing will."""
    value = state.staged.get(spec.path)
    if value is UNSET:
        return " → reset"
    return f" → {format_value(spec, value)}"


def title_bar(state: EditorState, layer_set: LayerSet) -> StyleAndTextTuples:
    """Which file is open, how deep the trail has gone, and what is pending."""
    path = layer_set.repo_path if state.layer == "repo" else layer_set.global_path
    count = len(changes(state, raw_for(layer_set, state.layer)))
    trail = " ▸ ".join(str(crumb) for crumb in state.trail[1:])
    where = f"   {state.trail[0]} ▸ {trail}" if len(state.trail) > 1 else ""
    return [
        ("class:title", f" jailbee config — {state.layer} ({path}){where}   modified: {count} ")
    ]


def section_pane(state: EditorState) -> Pane:
    """Top-level keys, with the open one marked and the cursor on its row.

    Each row carries an inline glyph — "▸ " on the cursor row while the
    section list has focus, "· " on the currently open section, "  "
    otherwise — matching the design's layout mockup (`▸ jetbrains` in the
    sections pane) and `field_pane`'s own `▸`/`●` markers. The two panes'
    visual vocabulary has to agree; a glyph-free section list next to a
    glyph-carrying field list would read as two different UIs stitched
    together. The style classes (`class:cursor`, `class:section-open`)
    still carry the highlight; the glyph is what makes the state legible
    even where no styling reaches (a plain-text terminal, a copy-paste).
    """
    names = sections(state)
    focused = state.section is None and not state.query
    fragments: StyleAndTextTuples = []
    for i, name in enumerate(names):
        if focused and i == state.index:
            fragments.append(("class:cursor", f"▸ {name}\n"))
        elif name == state.section:
            fragments.append(("class:section-open", f"· {name}\n"))
        else:
            fragments.append(("", f"  {name}\n"))
    if focused:
        row = state.index
    elif state.section in names:
        row = names.index(state.section)
    else:
        row = 0
    return Pane(fragments, row)


def field_pane(state: EditorState, layer_set: LayerSet) -> Pane:
    """The fields on screen: value as saved, origin, and any staged change.

    An entry screen's row reads its value and its modified-ness from
    different places than a section's row does — see `_entry_pending` and
    `_origin_source` — but the row it builds is otherwise identical, so this
    stays one loop with a per-row branch rather than two copies of it.
    """
    rows = visible_specs(state)
    if not rows:
        note = (
            "  no matches\n"
            if state.query
            else (
                "  nothing in the basic set — press `a` to show all\n"
                if state.section
                else "  pick a section, or press `/` to search every field\n"
            )
        )
        return Pane([("class:dim", note)], 0)
    is_entry = screen(state).kind == "entry"
    pending = _pending(state, layer_set)
    width = max(len(_row_name(state, spec)) for spec in rows)
    fragments: StyleAndTextTuples = []
    for i, spec in enumerate(rows):
        cursor = "▸" if i == state.index else " "
        modified = _entry_pending(state, layer_set, spec) if is_entry else spec.path in pending
        mark = "●" if modified else " "
        if is_entry:
            # No separate "as saved" to contrast against here (an entry's own
            # row is the collection's — see `collection_pane`), so the value
            # column is simply the current one.
            value = format_value(spec, effective(state, spec.path))
            suffix = ""
        else:
            origin = state.origins.get(spec.path)
            value = format_value(spec, origin.value if origin is not None else spec.default)
            suffix = _staged_suffix(state, spec) if modified else ""
        line = (
            f"{cursor}{mark} {_row_name(state, spec):<{width}}  "
            f"{value:<{_VALUE_WIDTH}}  {_ORIGIN_LABEL[_origin_source(state, spec)]}{suffix}\n"
        )
        if edit_block(spec, state.layer) is not None:
            style = "class:disabled"
        elif modified:
            style = "class:staged"
        elif i == state.index:
            style = "class:cursor"
        else:
            style = ""
        fragments.append((style, line))
    return Pane(fragments, state.index)


def _origin_source(state: EditorState, spec: FieldSpec) -> str:
    """Which layer a row's value comes from — an entry has only "set"/"default"."""
    if screen(state).kind == "entry":
        return entry_origin(state, spec.path)
    origin = state.origins.get(spec.path)
    return origin.source if origin is not None else "default"


def collection_pane(state: EditorState, layer_set: LayerSet) -> Pane:
    """One row per entry, with the inherited ones read-only above them.

    The inherited block is not addressable: `entries()` does not report it,
    so the cursor cannot reach a row the repo layer has no way to change (spec
    11.2). It is drawn all the same, because a list that silently showed half
    its effective contents would be worse than one that explains the rule.
    """
    view = screen(state)
    spec = view.collection
    if spec is None:
        return Pane([], 0)
    fragments: StyleAndTextTuples = []
    inherited = inherited_entries(spec, layer_set, state.layer)
    offset = 0
    if inherited:
        fragments.append(("class:dim", "  Inherited from global — added to, not replaced:\n"))
        fragments.extend(("class:dim", f"    {_entry_summary(entry)}\n") for entry in inherited)
        fragments.append(("class:dim", "\n"))
        offset = len(inherited) + 2
    crumbs = entries(state, spec)
    if not crumbs:
        fragments.append(("class:dim", "  no entries — press `n` to add one\n"))
        return Pane(fragments, offset)
    for i, crumb in enumerate(crumbs):
        cursor = "▸" if i == state.index else " "
        label = f"[{crumb}]" if isinstance(crumb, int) else str(crumb)
        # A secret map's value is never read here at all, let alone painted:
        # the mask is fixed text, not derived from the entry, so there is
        # nothing for `_entry_summary` to do with it.
        summary = _SECRET_MASK if spec.secret else _entry_summary(_dig_entry(state, spec, crumb))
        style = "class:cursor" if i == state.index else ""
        fragments.append((style, f"{cursor} {label}  {summary}\n"))
    return Pane(fragments, offset + state.index)


def _dig_entry(state: EditorState, spec: FieldSpec, crumb: Crumb) -> object:
    """One entry's own value, resolved the same way its fields are: through `effective`."""
    return effective(state, (*spec.path, crumb))


def _entry_summary(entry: object) -> str:
    """One line describing an entry, for the list a user scans.

    The first three set keys, in declaration order, which for every item model
    in the schema puts the identifying ones first (`host`/`container`,
    `name`/`port`, `name`/`run`). An empty entry says so rather than rendering
    as `{}`, which reads like a bug on a row the user just created.
    """
    if not isinstance(entry, dict) or not entry:
        return "(empty — press Enter to fill it in)"
    shown = list(entry.items())[:3]
    return "  ".join(f"{k}={v}" for k, v in shown)


def body_pane(state: EditorState, layer_set: LayerSet) -> Pane:
    """The middle pane: entries when a collection is open, fields otherwise."""
    if screen(state).kind == "collection":
        return collection_pane(state, layer_set)
    return field_pane(state, layer_set)


def help_pane(state: EditorState, layer_set: LayerSet) -> StyleAndTextTuples:
    """The selected field's own documentation, and its context.

    Four things, in the order someone reads them: what the field is, what it
    does, what it is set to now against what it defaults to, and anything that
    changes what editing it means — a reason it cannot be edited here, or the
    inherited list entries a repo-level list will be *added to* rather than
    replace (`layers.inherited_entries`, spec 3.3).
    """
    spec = current(state)
    if spec is None:
        return [("class:dim", "Pick a section, or press `/` to search every field.")]
    origin = state.origins.get(spec.path)
    source = origin.source if origin is not None else "default"
    saved = format_value(spec, origin.value if origin is not None else spec.default)
    out: StyleAndTextTuples = [
        ("class:cursor", dotted(spec.path)),
        ("class:dim", f"   [{spec.kind.value}]\n"),
        ("", f"{spec.description or 'No description.'}\n"),
        ("class:dim", f"Default: {format_value(spec, spec.default)} · Now: {saved} ({source})\n"),
    ]
    if spec.kind is FieldKind.CHOICE and spec.choices:
        out.append(("class:dim", f"One of: {', '.join(str(c) for c in spec.choices)}\n"))
    elif spec.kind is FieldKind.SCALAR_UNION and spec.choices:
        # A hint, not a closed set (spec 10.3) — say so, or the free-text arm
        # looks like a bug the first time someone needs it.
        out.append(
            (
                "class:dim",
                f"Suggestions: {', '.join(str(c) for c in spec.choices)} (or free text)\n",
            )
        )
    blocked = edit_block(spec, state.layer)
    if blocked is not None:
        out.append(("class:disabled", f"{blocked}\n"))
    inherited = inherited_entries(spec, layer_set, state.layer)
    if inherited:
        out.extend(_inherited_block(state, spec, inherited))
    return out


def _inherited_block(
    state: EditorState, spec: FieldSpec, inherited: tuple[object, ...]
) -> StyleAndTextTuples:
    """The inherited-context lines, or the warning that replaces them.

    `inherited_entries` answers against the layers **as saved** (spec 10.1
    option b), which is right for an origin marker but not for a sentence about
    what a save will do. A user who opens the list editor on
    `egress_allow: [a.example]`, deletes every line and commits has staged `[]`
    — `deep_merge`'s explicit reset — and saving it empties the allowlist
    outright. Repeating "your entries are added to these" underneath that row
    would state the exact inverse.

    Two staged shapes keep the context, because for them it stays true: a
    non-empty list still appends, and `UNSET` (`r`) deletes the repo key so the
    global entries are inherited whole. Everything else — `[]`, `null`, a
    scalar — takes `deep_merge`'s overlay-wins branch and discards them.
    """
    staged_value = state.staged.get(spec.path, _NOT_STAGED)
    discards = (
        staged_value is not _NOT_STAGED
        and staged_value is not UNSET
        and not (isinstance(staged_value, list) and staged_value)
    )
    if discards:
        return [
            (
                "class:error",
                f"\nSaving this discards the {len(inherited)} entr"
                f"{'y' if len(inherited) == 1 else 'ies'} inherited from global — "
                f"press `r` to inherit them instead.\n",
            )
        ]
    out: StyleAndTextTuples = [
        ("class:dim", "\nInherited from global (your entries are added to these):\n")
    ]
    out.extend(("class:dim", f"  · {entry}\n") for entry in inherited)
    return out


_FOOTER = (
    " / search   Space toggle   Enter edit   r reset   a show all   "
    "d diff   s save   Esc back   q quit "
)
_COLLECTION_FOOTER = (
    " n new   x delete   J/K move   Enter open   d diff   s save   Esc back   q quit "
)


def footer(state: EditorState) -> StyleAndTextTuples:
    """The always-visible key hints for the screen that is open."""
    text = _COLLECTION_FOOTER if screen(state).kind == "collection" else _FOOTER
    return [("class:footer", text)]
