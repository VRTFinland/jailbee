"""The two raw config layers, and where each value actually comes from.

The editor never reads a merged `Config`. A merge answers "what is the
value"; the editor also has to answer "which file said so", because that
is what every row's origin marker shows and what decides whether `r`
deletes a key or does nothing (spec 3.3).

This is the only module in `config_edit` that touches the filesystem.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jailbee.config import ConfigError
from jailbee.config.common import _read_yaml_or_empty
from jailbee.config.loader import load_config_from_layers
from jailbee.config_edit.schema import GLOBAL_ONLY_KEYS, FieldKind
from jailbee.config_writer import DELETE, YamlChange

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config_edit.schema import FieldSpec

LayerName = Literal["repo", "global"]


@dataclass(frozen=True)
class LayerSet:
    """Both raw YAML mappings, plus where they came from.

    `raw` mappings are the plain `yaml.safe_load` result — never
    `model_dump()` output. `config_writer.render_documented` refuses the
    latter, and for a good reason: a dumped `SecretStr` would overwrite a
    real token with a mask on the next save.
    """

    repo_path: Path
    global_path: Path
    repo_raw: dict[str, object]
    global_raw: dict[str, object]


@dataclass(frozen=True)
class Origin:
    """Which layer supplies a path's current value, and what it is."""

    source: Literal["default", "global", "repo"]
    value: object


def read_layers(repo_config_path: Path, global_path: Path) -> LayerSet:
    """Read both layers. A missing file reads as `{}`, not as an error.

    Both absences are ordinary states: `jb new` works in a directory with
    no `.jailbee/config.yaml`, and a host may have no `global.yaml` until
    `jb config init --global` runs. Invalid YAML still raises
    `ConfigError` from `_read_yaml_or_empty` — that is a real problem and
    the editor should report it rather than silently show defaults.
    """
    return LayerSet(
        repo_path=repo_config_path,
        global_path=global_path,
        repo_raw=_read_yaml_or_empty(repo_config_path),
        global_raw=_read_yaml_or_empty(global_path),
    )


def raw_for(layers: LayerSet, layer: LayerName) -> dict[str, object]:
    """The raw mapping of the layer being edited."""
    return layers.repo_raw if layer == "repo" else layers.global_raw


def lookup(raw: dict[str, object], path: tuple[str, ...]) -> tuple[bool, object]:
    """`(present, value)` for `path` in a raw mapping.

    `present` and `value` are separate because `None` is a legitimate
    stored value — `chrome.url: null` and an unset `chrome.url` are
    different states, and collapsing them would make the origin marker
    lie. Walking into a non-mapping returns "absent" rather than raising:
    a hand-broken file must not crash the editor's read path.
    """
    node: object = raw
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def resolve(specs: Sequence[FieldSpec], layers: LayerSet) -> dict[tuple[str, ...], Origin]:
    """Where each spec's value comes from: repo, else global, else the default.

    Independent of which layer is open. A repo-layer editor still marks an
    inherited value `(global)`, so the user can see that editing it will
    create a repo-layer key rather than change the one they are looking at.
    """
    out: dict[tuple[str, ...], Origin] = {}
    for spec in specs:
        present, value = lookup(layers.repo_raw, spec.path)
        if present:
            out[spec.path] = Origin("repo", value)
            continue
        present, value = lookup(layers.global_raw, spec.path)
        if present:
            out[spec.path] = Origin("global", value)
            continue
        out[spec.path] = Origin("default", spec.default)
    return out


_APPENDING_KINDS = frozenset({FieldKind.STR_LIST, FieldKind.MODEL_LIST})
"""Kinds `deep_merge` appends rather than replaces (`config/common.py`).

Only lists append. A dict deep-merges per key and a scalar is overridden,
so for those the repo layer's value is the whole story and showing
inherited context would misrepresent what saving does.
"""


def disabled_reason(spec: FieldSpec, layer: LayerName) -> str | None:
    """Why `spec` cannot be edited in `layer`, or `None` if it can.

    Returned rather than filtered so the field still renders, greyed, with
    the reason next to it (spec 3.3). A silently missing setting reads as
    a bug; a disabled one with a reason reads as a rule.

    `GLOBAL_ONLY_KEYS` is checked against `spec.path[0]`. Of its three
    members, only `github` appears as a top-level key in `repo_specs()`;
    the other two (`claude_credentials` and `claude_credentials_dir`) are
    either not `Config` fields at all or are in `COMPUTED_FIELDS` so
    `build_specs` skips them. The set is kept complete to mirror the ban
    list in `config/loader.py` exactly — a future maintainer should not
    expect a fourth key to silence a repo-layer setting that has no visible
    `repo_specs()` entry.
    """
    if spec.kind is FieldKind.OPAQUE:
        return (
            "Free-form overlay with no schema — edit it by hand in ~/.config/jailbee/global.yaml."
        )
    if layer == "repo" and spec.path[0] in GLOBAL_ONLY_KEYS:
        return (
            f"`{spec.path[0]}` is host-local and is rejected in a repo config — "
            f"set it in ~/.config/jailbee/global.yaml."
        )
    return None


def inherited_entries(spec: FieldSpec, layers: LayerSet, layer: LayerName) -> tuple[object, ...]:
    """Global list entries the repo layer's own entries will be appended to.

    `deep_merge` appends lists, so a repo-level `egress_allow` adds to the
    global one instead of replacing it. Showing only the repo's own
    entries would let the user believe they had removed the rest; these
    are rendered read-only above the editable ones.

    Empty for the global layer (nothing above it to inherit from) and for
    every non-list kind (those override, so there is no context to show).

    If the repo layer has an explicit empty list (`egress_allow: []`),
    `deep_merge` treats it as a reset that discards the global entries
    entirely, so this returns `()` — nothing is inherited when the list
    is explicitly empty. This function reports on layers as they are
    saved to disk, not on staged edits; if the user then adds entries,
    the list becomes non-empty and inheritance reappears after a save.
    Recomputing against staged edits belongs to the UI plan.
    """
    if layer != "repo" or spec.kind not in _APPENDING_KINDS:
        return ()
    # If the repo layer has an explicit empty list, it resets (discards)
    # the global entries rather than appending to them.
    repo_present, repo_value = lookup(layers.repo_raw, spec.path)
    if repo_present and repo_value == []:
        return ()
    present, value = lookup(layers.global_raw, spec.path)
    if not present or not isinstance(value, list):
        return ()
    return tuple(value)


def apply_changes(
    raw: dict[str, object], changes: Sequence[YamlChange]
) -> dict[str, object]:
    """`raw` with `changes` applied, as a new mapping.

    The in-memory twin of `config_writer._apply`, for the dry run: the
    validator needs the resulting mapping, not the resulting YAML text.
    Deep-copies first, because `raw` is the editor's live view of the file
    and a rejected validation must leave it untouched.
    """
    out = deepcopy(raw)
    for change in changes:
        *parents, leaf = change.path
        node: dict[str, object] = out
        for key in parents:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        if change.value is DELETE:
            node.pop(leaf, None)
        else:
            node[leaf] = change.value
    return out


def validate(
    layers: LayerSet, layer: LayerName, changes: Sequence[YamlChange]
) -> str | None:
    """The error a save would produce, or `None` if the staged layer loads.

    Runs the *real* loader over the staged mapping (spec 3.5 step 2), so
    the editor cannot write a file the CLI would then reject. That catches
    more than pydantic does: the retired-key check, the placement bans,
    the container_prefix regex, the shared-cache and autostart uniqueness
    rules, and `github.enabled` with no tokens are all loader-level.

    Nothing is written — this is the check that runs *before* the backup
    and the write.

    Deliberately loads the *repo* config even when the global layer is the
    one being edited. A global-layer change is only meaningful through its
    effect on some repo's merged config, and validating it in isolation
    would miss exactly the cross-layer failures worth catching — a global
    `autostart` step colliding with a repo one, for instance. The repo
    path used is whichever config the editor was opened against.
    """
    global_raw = layers.global_raw
    repo_raw = layers.repo_raw
    if layer == "repo":
        repo_raw = apply_changes(repo_raw, changes)
    else:
        global_raw = apply_changes(global_raw, changes)
    try:
        load_config_from_layers(
            global_raw, repo_raw, layers.repo_path, origin=str(layers.repo_path)
        )
    except ConfigError as e:
        return str(e)
    return None
