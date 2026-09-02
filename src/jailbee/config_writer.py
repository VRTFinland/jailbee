"""Round-trip editing of jailbee's YAML config files.

`global.yaml` is a user-authored file that is almost entirely comments,
so editing it by re-serialising a PyYAML parse would destroy the very
thing that makes it usable. ruamel's round-trip loader carries comments,
key order and quoting on the parsed structure, so a patch touches only
what changed.

This module knows nothing about jailbee's schema: it takes paths and
values. Schema-aware rendering (`render_documented`) belongs to the
config editor and lands in this same module later.
"""

from __future__ import annotations

import io
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


class _Delete:
    """Type of the `DELETE` sentinel; exists so mypy can name it."""

    def __repr__(self) -> str:
        return "DELETE"


DELETE: Final = _Delete()
"""Sentinel `YamlChange.value` meaning "remove this key".

A sentinel and not `None`, because `None` is a legitimate value: an
explicit YAML `null` under `claude_credentials.repos.<prefix>` is how a
repo opts out of every credential group.
"""


@dataclass(frozen=True)
class YamlChange:
    """One edit to apply. `value is DELETE` removes the key."""

    path: tuple[str, ...]
    value: object


def _yaml() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.width = 100
    y.preserve_quotes = True
    return y


def patch_yaml(text: str, changes: Sequence[YamlChange]) -> str:
    """Apply `changes` to `text`, leaving everything else untouched.

    Comments, key order and formatting survive. With no changes the
    output is byte-identical to the input — the property callers depend
    on, so a save that changed nothing produces no diff.
    """
    if not changes:
        return text
    y = _yaml()
    data = y.load(text) if text.strip() else CommentedMap()
    if data is None:
        data = CommentedMap()
    for change in changes:
        _apply(data, change)
    stream = io.StringIO()
    y.dump(data, stream)
    return stream.getvalue()


def _apply(data: CommentedMap, change: YamlChange) -> None:
    *parents, leaf = change.path
    node: Any = data
    for key in parents:
        child = node.get(key)
        if not isinstance(child, dict):
            child = CommentedMap()
            node[key] = child
        node = child
    if change.value is DELETE:
        node.pop(leaf, None)
    else:
        node[leaf] = change.value
