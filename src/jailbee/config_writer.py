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
import stat
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from jailbee.claude_pool import _fsync_dir


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


_DEFAULT_MODE = 0o600
"""Mode for a config file this module creates.

jailbee's global config can carry `github.api_tokens`, which
`load_config` refuses to read at a wider mode. Creating the file
world-readable and tightening it later would leave a window; creating it
at 0600 has none, and no config file jailbee writes wants to be wider.
"""


def patch_file(path: Path, changes: Sequence[YamlChange]) -> bool:
    """Apply `changes` to the YAML file at `path`. Returns whether it changed.

    Atomic and mode-preserving: the new content is written to a temporary
    file in the same directory, fsynced, given the original file's mode,
    and only then renamed over the target. A crash leaves either the old
    file or the new one, never a truncated mix, and never a file at a
    wider mode than it had.

    A missing file is created at `_DEFAULT_MODE`. An empty change list
    touches nothing at all — not even an mtime — so a caller can pass the
    result of a diff without special-casing "no change".
    """
    if not changes:
        return False
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    patched = patch_yaml(original, changes)
    if patched == original and path.exists():
        return False
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else _DEFAULT_MODE

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp = Path(name)
    try:
        with open(tmp, "wb") as handle:
            handle.write(patched.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(mode)
        tmp.replace(path)
        _fsync_dir(path.parent)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise
    return True
