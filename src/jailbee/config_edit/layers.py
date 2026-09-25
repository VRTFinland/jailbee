"""The two raw config layers, and where each value actually comes from.

The editor never reads a merged `Config`. A merge answers "what is the
value"; the editor also has to answer "which file said so", because that
is what every row's origin marker shows and what decides whether `r`
deletes a key or does nothing (spec 3.3).

One of the package's two filesystem modules: this one *reads* — both raw
layers, and, through `validate`, the whole config-loading subsystem over a
staged one. `save` is the other, and the only one that writes. Everything
else in `config_edit` is pure (`schema`, `state`, `values`, `render`) or the
terminal driver (`app`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from jailbee.config import ConfigError, load_config_from_layers, resolve_browsers_raw

# `_HOST_LEVEL_KEYS` is the loader's own routing table: the keys
# `_split_host_keys` lifts out of `global.yaml` *before* `deep_merge` runs.
# This module reports what a save will do, so it has to agree with that
# routing exactly — a key on one side of the boundary merges by
# `deep_merge`'s rules, a key on the other by `Config._effective_columns`.
from jailbee.config.common import (
    _HOST_LEVEL_KEYS,
    _read_yaml_or_empty,
    normalize_credentials_key,
)
from jailbee.config_edit.schema import GLOBAL_ONLY_KEYS, FieldKind, dotted, entry_model
from jailbee.config_writer import DELETE, KeyPath, YamlChange, credential_key_migration
from jailbee.global_config import validate_global_raw

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config_edit.schema import FieldSpec

LayerName = Literal["repo", "global", "local"]


@dataclass(frozen=True)
class LayerSet:
    """Raw YAML mappings for repo, global and host-local `repos/<prefix>.yaml`.

    `raw` mappings are the plain `yaml.safe_load` result — never
    `model_dump()` output. `config_writer.render_documented` refuses the
    latter, and for a good reason: a dumped `SecretStr` would overwrite a
    real token with a mask on the next save.
    """

    repo_path: Path
    global_path: Path
    repo_raw: dict[str, object]
    global_raw: dict[str, object]
    local_path: Path
    local_raw: dict[str, object]


@dataclass(frozen=True)
class Origin:
    """Which layer supplies a path's current value, and what it is."""

    source: Literal["default", "global", "repo", "local"]
    value: object


def read_layers(
    repo_config_path: Path, global_path: Path, local_path: Path | None = None
) -> LayerSet:
    """Read both layers. A missing file reads as `{}`, not as an error.

    Both absences are ordinary states: `jb new` works in a directory with
    no `.jailbee/config.yaml`, and a host may have no `global.yaml` until
    `jb config init --global` runs. Invalid YAML still raises
    `ConfigError` from `_read_yaml_or_empty` — that is a real problem and
    the editor should report it rather than silently show defaults.
    """
    repo_raw = _read_yaml_or_empty(repo_config_path)
    global_raw = _read_yaml_or_empty(global_path)
    if local_path is None:
        from jailbee.config.common import deep_merge
        from jailbee.config.loader import derive_prefix
        from jailbee.config.local_layer import local_config_path

        prefix = derive_prefix(deep_merge(global_raw, repo_raw), repo_config_path)
        safe_prefix = prefix if re.fullmatch(r"[a-z0-9][a-z0-9-]*", prefix) else "invalid-prefix"
        local_path = local_config_path(safe_prefix)
    return LayerSet(
        repo_path=repo_config_path,
        global_path=global_path,
        repo_raw=repo_raw,
        global_raw=global_raw,
        local_path=local_path,
        local_raw=_read_yaml_or_empty(local_path),
    )


def raw_for(layer_set: LayerSet, layer: LayerName) -> dict[str, object]:
    """The raw mapping of the layer being edited."""
    return {
        "repo": layer_set.repo_raw,
        "global": layer_set.global_raw,
        "local": layer_set.local_raw,
    }[layer]


def path_for(layer_set: LayerSet, layer: LayerName) -> Path:
    """The file the layer being edited lives in."""
    return {
        "repo": layer_set.repo_path,
        "global": layer_set.global_path,
        "local": layer_set.local_path,
    }[layer]


def lookup(raw: dict[str, object], path: KeyPath) -> tuple[bool, object]:
    """`(present, value)` for `path` in a raw mapping.

    `present` and `value` are separate because `None` is a legitimate
    stored value — `chrome.url: null` and an unset `chrome.url` are
    different states, and collapsing them would make the origin marker
    lie. Walking into a non-mapping returns "absent" rather than raising:
    a hand-broken file must not crash the editor's read path.

    That includes an out-of-range integer segment: `apply_changes` and
    `config_writer._apply` raise on one, because there an index is always
    produced by code that just read the same list, so a bad one is a
    programming error. Here it can also come from a hand-edited file the
    editor is merely displaying, so it reads as "absent" instead — the
    read path must never crash on a document it didn't write.
    """
    node: object = raw
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


def resolve(specs: Sequence[FieldSpec], layer_set: LayerSet) -> dict[KeyPath, Origin]:
    """Where each spec's value comes from: local, else repo, else global, else default.

    Independent of which layer is open. A repo-layer editor still marks an
    inherited value `(global)`, so the user can see that editing it will
    create a repo-layer key rather than change the one they are looking at.

    Looks up paths through `resolve_browsers_raw`'s fold rather than
    `layer_set.repo_raw`/`global_raw` directly, so a legacy top-level
    `chrome:` block still reports a real origin for `browsers.chrome.*`
    instead of lying and saying "default". The same goes for a legacy
    `claude_credentials:` block, folded to `credentials` on a copy of the
    global layer so its rows report the real origin. Both folds are silent
    by construction — the deprecation notices live in
    `loader.load_config_from_layers`, not in the folds — which is what this
    reload path needs: it runs on every reload, including while the editor
    `Application` is live, where a notice written to the terminal
    mid-session would corrupt the display. The stored
    `layer_set.repo_raw`/`global_raw` are left untouched: they are also the
    write path's base mapping (`raw_for`), and folding there would rewrite a
    user's legacy block as a side effect of an unrelated save.
    """
    repo_raw = resolve_browsers_raw(layer_set.repo_raw)
    global_raw = resolve_browsers_raw(
        normalize_credentials_key(layer_set.global_raw, str(layer_set.global_path))[0]
    )
    local_raw = resolve_browsers_raw(layer_set.local_raw)
    out: dict[KeyPath, Origin] = {}
    for spec in specs:
        for source, raw in (("local", local_raw), ("repo", repo_raw), ("global", global_raw)):
            present, value = lookup(raw, spec.path)
            if present:
                out[spec.path] = Origin(source, value)  # type: ignore[arg-type] # source is the literal tuple member
                break
        else:
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

    `GLOBAL_ONLY_KEYS` is checked against `spec.path[0]`. Of its five
    members, only `github` appears as a top-level key in `repo_specs()`;
    the other four (`credentials`, `claude_credentials`, `credential_group`
    and `claude_credentials_dir`) are either not `Config` fields at all or
    are in `COMPUTED_FIELDS` so `build_specs` skips them. The set is kept
    complete to mirror the ban list in `config/loader.py` exactly — a future
    maintainer should not expect a sixth key to silence a repo-layer setting
    that has no visible `repo_specs()` entry.
    """
    if layer == "repo" and spec.path[0] in GLOBAL_ONLY_KEYS:
        return (
            f"`{spec.path[0]}` is host-local and is rejected in a repo config — "
            f"set it in ~/.config/jailbee/global.yaml."
        )
    if layer == "global" and spec.path == ("github", "token"):
        return (
            "`github.token` is per-repo — set it in the repo's host-local file "
            "(`jailbee config edit --local`)."
        )
    if layer == "local" and spec.path[0] == "container_prefix":
        return "`container_prefix` names this file; set it in the repo config."
    if layer == "local" and spec.path == ("github", "token"):
        return (
            "Secrets are not editable here — the editor will not paint a token on a "
            "terminal. Edit the file by hand and keep it at mode 0600."
        )
    if layer == "local" and spec.path[:2] == ("github", "api_tokens"):
        return "This file is already per-repo — use `github.token` instead."
    return None


def inherited_entries(spec: FieldSpec, layer_set: LayerSet, layer: LayerName) -> tuple[object, ...]:
    """Global list entries the repo layer's own entries will be appended to.

    `deep_merge` appends lists, so a repo-level `egress_allow` adds to the
    global one instead of replacing it. Showing only the repo's own
    entries would let the user believe they had removed the rest; these
    are rendered read-only above the editable ones.

    Empty for the global layer (nothing above it to inherit from) and for
    every non-list kind (those override, so there is no context to show).

    Two further rules, both of which say "nothing is inherited":

    * **Host-level paths never reach `deep_merge` at all.** The keys
      in `_HOST_LEVEL_KEYS` are split off `global.yaml` into a separate
      `GlobalConfig` object, and `ls`/`dashboard` are then merged
      field-by-field by `Config._effective_columns`, which *replaces*.
      A repo `ls.hide` throws the global one away rather than adding to it.
    * **Only a non-empty repo list appends.** `[]` is `deep_merge`'s
      explicit reset; `null` and any non-list value take its
      overlay-wins branch. All three discard the global entries.

    This function reports on layers as they are saved to disk, not on
    staged edits; if the user then adds entries, the list becomes
    non-empty and inheritance reappears after a save. Recomputing against
    staged edits belongs to the UI plan.
    """
    if layer == "global" or spec.kind not in _APPENDING_KINDS:
        return ()
    if spec.path[0] in _HOST_LEVEL_KEYS:
        # Split out before deep_merge ever runs; global.yaml's block is a
        # separate object merged field-wise by Config._effective_columns.
        return ()
    own_present, own_value = lookup(raw_for(layer_set, layer), spec.path)
    if own_present and not (isinstance(own_value, list) and own_value):
        # [] resets, null and any non-list hit deep_merge's overlay-wins
        # branch; only a non-empty repo list appends.
        return ()
    below = (
        [layer_set.global_raw] if layer == "repo" else [layer_set.global_raw, layer_set.repo_raw]
    )
    inherited: list[object] = []
    for raw in below:
        present, value = lookup(raw, spec.path)
        if not present:
            continue
        if not isinstance(value, list) or not value:
            inherited = []
        else:
            inherited.extend(value)
    return tuple(inherited)


def apply_changes(raw: dict[str, object], changes: Sequence[YamlChange]) -> dict[str, object]:
    """`raw` with `changes` applied, as a new mapping.

    The in-memory twin of `config_writer._apply`, for the dry run: the
    validator needs the resulting mapping, not the resulting YAML text.
    Deep-copies first, because `raw` is the editor's live view of the file
    and a rejected validation must leave it untouched.

    An integer path segment addresses an entry of a list already present
    in `raw` (the caller just read it from this same document), so an
    out-of-range index raises rather than being silently absorbed — unlike
    `lookup`, which reads a possibly hand-broken file and must not crash.
    """
    out = deepcopy(raw)
    for change in changes:
        *parents, leaf = change.path
        node: object = out
        for key in parents:
            node = _descend_raw(node, key, change.path)
        if isinstance(leaf, int):
            if not isinstance(node, list) or not 0 <= leaf < len(node):
                raise ValueError(f"{dotted(change.path)}: index out of range")
            if change.value is DELETE:
                del node[leaf]
            else:
                node[leaf] = change.value
            continue
        if not isinstance(node, dict):
            raise ValueError(f"{dotted(change.path)}: expected a mapping")
        if change.value is DELETE:
            node.pop(leaf, None)
        else:
            node[leaf] = change.value
    return out


def _descend_raw(node: object, key: str | int, path: KeyPath) -> object:
    """One step down a plain `dict`/`list` tree, matching `config_writer._descend`.

    Creates a missing mapping but never a missing list entry, and keeps an
    existing `list` in place rather than replacing it — otherwise a parent
    segment pointing at a list (`host_mounts` before its index) would be
    clobbered with a fresh `dict` and the entry beneath it unreachable.
    """
    if isinstance(key, int):
        if not isinstance(node, list) or not 0 <= key < len(node):
            raise ValueError(f"{dotted(path)}: index out of range")
        return node[key]
    if not isinstance(node, dict):
        raise ValueError(f"{dotted(path)}: expected a mapping")
    child = node.get(key)
    if not isinstance(child, (dict, list)):
        child = {}
        node[key] = child
    return child


_PREFIX_PATH = ("container_prefix",)
_PLACEHOLDER_PREFIX = "jailbee-config-edit"
"""Stands in for the directory-derived `container_prefix` during a global save.

Never written anywhere: it exists only so `_build_config_from_dict`'s fallback
(`repo_root.name`, which need not be a legal prefix) cannot decide whether a
change to `global.yaml` is valid. Matches the loader's own `[a-z0-9][a-z0-9-]*`
by construction.
"""


def validate(layer_set: LayerSet, layer: LayerName, changes: Sequence[YamlChange]) -> str | None:
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

    When that repo config does not exist, one half of the merged result is
    not the user's config at all but the loader's own fallbacks, and one of
    them can fail: `container_prefix` defaults to the *directory name*, so
    `jailbee config edit --global` in a directory named `Tutkimus_A` would
    refuse every global save with a message naming a file the user is not
    editing and which does not exist. A placeholder prefix stands in for that
    one derivation, and only when neither layer sets the key — so a genuinely
    invalid `container_prefix:` staged into `global.yaml` is still caught,
    and so is every cross-layer collision, which is what this check is for.

    A staged *global* layer gets a second pass, `validate_global_raw`,
    because the loader splits the `_HOST_LEVEL_KEYS` off and uses them for
    one thing only (`credentials`); the rest of `global.yaml`'s
    host-level half — `docker_registry_mirror`, `ls`, `dashboard`,
    `scratch` — reaches no schema at all on that path. The loader runs
    first: it is the broader check (it scans the whole global mapping for
    retired keys and sees both layers for the cross-layer rules), so its
    diagnosis is the more general one when both would fire.

    `emit_hint=False`: this runs synchronously from the editor's save
    handler while the full-screen `Application` is live. Without it, a
    legacy top-level `chrome:` block would print the loader's deprecation
    notice straight to the terminal on every save — the same hazard
    `resolve()` already guards against on reload.
    """
    global_raw = layer_set.global_raw
    repo_raw = layer_set.repo_raw
    local_raw = layer_set.local_raw
    # A write that touches a legacy `claude_credentials:` block must migrate it
    # in the same write (copy to `credentials`, delete the old key) or the
    # staged mapping would carry both spellings and `normalize_credentials_key`
    # would refuse it. The editor's staged changes name `credentials.*`, so the
    # migration has to run *before* `apply_changes`, exactly as it does in
    # `save.build_plan` and the account-group writer.
    if layer == "repo":
        # No migration on the repo layer: `credentials:` is host-level, so both
        # spellings are banned there and `load_config_from_layers` refuses the
        # file either way. Migrating first would make the refusal name
        # `credentials` — a key that is not in the user's file — instead of the
        # `claude_credentials` they actually wrote.
        repo_raw = apply_changes(repo_raw, changes)
    elif layer == "global":
        changes = credential_key_migration(global_raw, changes)
        global_raw = apply_changes(global_raw, changes)
        if not layer_set.repo_path.exists() and not (
            lookup(global_raw, _PREFIX_PATH)[0] or lookup(repo_raw, _PREFIX_PATH)[0]
        ):
            repo_raw = {**repo_raw, "container_prefix": _PLACEHOLDER_PREFIX}
    else:
        local_raw = apply_changes(layer_set.local_raw, changes)
    try:
        load_config_from_layers(
            global_raw,
            repo_raw,
            layer_set.repo_path,
            origin=str(layer_set.repo_path),
            global_origin=str(layer_set.global_path),
            emit_hint=False,
            local_raw=local_raw,
            local_origin=str(layer_set.local_path),
        )
        if layer == "global":
            validate_global_raw(global_raw, layer_set.global_path, emit_hint=False)
    except ConfigError as e:
        return str(e)
    return None


def validate_entry(spec: FieldSpec, value: object, collection: object = None) -> str | None:
    """The first thing wrong with one collection entry, or `None`.

    Early feedback, not a second gate: `validate` still runs the real loader
    over the whole staged mapping at save time and remains authoritative. This
    only spares the user a save that fails with a path they then have to hunt
    for (spec 11.8) — which is why it reports one field, the way a form does,
    rather than the whole error tree.

    Kept here rather than in a new `validation.py`: it is six lines and pulls
    in nothing `layers` does not already reach. Spec 10.7's suggested split
    still stands for the day `validate` itself grows.

    `collection` is the list the entry lives in, needed only for a
    `list[A] | list[B]` field, where an entry too empty to identify itself
    is identified by its siblings — see `schema.entry_model`. Optional, so
    the single-model callers stay as they were.
    """
    model = entry_model(spec, value, collection)
    if model is None:
        return None
    try:
        model.model_validate(value)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or spec.label
        return f"{where}: {first['msg']}"
    return None
