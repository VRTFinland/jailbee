"""The two raw config layers, and where each value actually comes from.

The editor never reads a merged `Config`. A merge answers "what is the
value"; the editor also has to answer "which file said so", because that
is what every row's origin marker shows and what decides whether `r`
deletes a key or does nothing (spec 3.3).

This is the only module in `config_edit` that touches the filesystem.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jailbee.config.common import _read_yaml_or_empty

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
