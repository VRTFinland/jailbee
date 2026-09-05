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

from jailbee.config_edit.layers import disabled_reason, inherited_entries, raw_for
from jailbee.config_edit.schema import FieldKind
from jailbee.config_edit.state import (
    UNSET,
    changes,
    current,
    sections,
    visible_specs,
)
from jailbee.config_edit.values import format_value

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from jailbee.config_edit.layers import LayerName, LayerSet
    from jailbee.config_edit.schema import FieldSpec
    from jailbee.config_edit.state import EditorState

_COLLECTION_KINDS = frozenset({FieldKind.MODEL_LIST, FieldKind.MODEL_MAP})
"""Kinds whose editor is a drill-down screen this release does not have yet.

They render read-only with a reason rather than vanishing: a setting that is
simply missing reads as a bug, one that explains itself reads as a boundary.
"""

_ORIGIN_LABEL = {"default": "(default)", "global": "(global)", "repo": "(repo)"}

_VALUE_WIDTH = 22

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
    Three causes, in order of how permanent they are — a config rule
    (`layers.disabled_reason`: an `OPAQUE` free-form block, a key the loader
    bans from a repo config), a deliberate refusal (a secret, which the editor
    will not paint on a terminal), and one that is only true for now (a
    collection of models, whose drill-down screen is spec 4.4 and is not built
    yet).
    """
    reason = disabled_reason(spec, layer)
    if reason is not None:
        return reason
    if spec.secret:
        return (
            "Secrets are not editable here — the editor will not paint a token on a "
            "terminal. Edit the file by hand and keep it at mode 0600."
        )
    if spec.kind in _COLLECTION_KINDS:
        return "Lists of structured entries are not editable here yet — edit this key by hand."
    return None


def _pending(state: EditorState, layer_set: LayerSet) -> frozenset[tuple[str, ...]]:
    """Paths whose staged value would actually alter the file."""
    return frozenset(c.path for c in changes(state, raw_for(layer_set, state.layer)))


def _row_name(state: EditorState, spec: FieldSpec) -> str:
    """The label a row shows: dotted while searching, bare inside a section.

    Search spans every section, so a bare `enabled` there would name four
    different fields identically.
    """
    return ".".join(spec.path) if state.query else spec.label


def _staged_suffix(state: EditorState, spec: FieldSpec) -> str:
    """What the save will do to this row, or `""` when nothing will."""
    value = state.staged.get(spec.path)
    if value is UNSET:
        return " → reset"
    return f" → {format_value(spec, value)}"


def title_bar(state: EditorState, layer_set: LayerSet) -> StyleAndTextTuples:
    """Which file is open, and how much is waiting to be written to it."""
    path = layer_set.repo_path if state.layer == "repo" else layer_set.global_path
    count = len(changes(state, raw_for(layer_set, state.layer)))
    return [("class:title", f" jailbee config — {state.layer} ({path})   modified: {count} ")]


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
    """The fields on screen: value as saved, origin, and any staged change."""
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
    pending = _pending(state, layer_set)
    width = max(len(_row_name(state, spec)) for spec in rows)
    fragments: StyleAndTextTuples = []
    for i, spec in enumerate(rows):
        cursor = "▸" if i == state.index else " "
        mark = "●" if spec.path in pending else " "
        origin = state.origins.get(spec.path)
        value = format_value(spec, origin.value if origin is not None else spec.default)
        suffix = _staged_suffix(state, spec) if spec.path in pending else ""
        line = (
            f"{cursor}{mark} {_row_name(state, spec):<{width}}  "
            f"{value:<{_VALUE_WIDTH}}  {_ORIGIN_LABEL[_origin_source(state, spec)]}{suffix}\n"
        )
        if edit_block(spec, state.layer) is not None:
            style = "class:disabled"
        elif spec.path in pending:
            style = "class:staged"
        elif i == state.index:
            style = "class:cursor"
        else:
            style = ""
        fragments.append((style, line))
    return Pane(fragments, state.index)


def _origin_source(state: EditorState, spec: FieldSpec) -> str:
    origin = state.origins.get(spec.path)
    return origin.source if origin is not None else "default"


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
        ("class:cursor", ".".join(spec.path)),
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


def footer() -> StyleAndTextTuples:
    """The always-visible key hints."""
    return [("class:footer", _FOOTER)]
