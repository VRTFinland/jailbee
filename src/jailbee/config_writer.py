"""YAML write policies for jailbee's config files.

Two renderers, one library:

* `patch_yaml` / `patch_file` touch only the keys that changed and leave
  everything else — comments, key order, formatting — byte-identical.
  Used for the repo's `.jailbee/config.yaml`, which is committed and read
  as PR diffs.
* `render_documented` rewrites a file from scratch with each key's
  `description=` above it as a comment. Used for `global.yaml`, which
  jailbee owns, and for `jailbee config init`. Safe to regenerate because
  the models are `extra="forbid"`, so an unknown key cannot exist in the
  file to be lost.

Both are pure string transformations so they can be tested without a
terminal or a real config file.
"""

from __future__ import annotations

import io
import os
import stat
import tempfile
import textwrap
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from jailbee.claude_pool import _fsync_dir

if TYPE_CHECKING:
    from pydantic import BaseModel


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


def write_text_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write `text` to `path` atomically, without ever widening its mode.

    The content goes to a temporary file in the same directory, is fsynced,
    given the target's mode, and only then renamed over it — so a crash leaves
    either the old file or the new one, never a truncated mix, and never a file
    readable by more people than it was a moment ago.

    `mode` defaults to the target's own mode when it exists and to
    `_DEFAULT_MODE` (0600) when it does not: jailbee's global config can carry
    `github.api_tokens`, and `load_config` refuses to read those at a wider
    mode. Creating the file world-readable and tightening it afterwards would
    leave a window; creating it at 0600 has none.
    """
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else _DEFAULT_MODE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp = Path(name)
    try:
        with open(tmp, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(mode)
        tmp.replace(path)
        _fsync_dir(path.parent)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


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
    write_text_atomic(path, patched)
    return True


DOCUMENTED_HEADER = (
    "# Written by `jailbee config init --global`. Comments in this file are\n"
    "# generated from jailbee's own schema and are replaced if you regenerate\n"
    "# it — put notes you want to keep elsewhere. Every key is optional;\n"
    "# delete one to fall back to the built-in default.\n"
)


def render_documented(
    values: dict[str, object],
    model: type[BaseModel],
    *,
    header: str = DOCUMENTED_HEADER,
) -> str:
    """Render `values` as YAML with each key's schema description above it.

    `values` must be the **raw YAML mapping** — plain scalars, lists and
    dicts, as `_read_yaml_or_empty` returns them. Never pass
    `model_dump()` output, in either mode: python-mode hands back live
    `SecretStr` objects, which `_reject_secrets` catches; json-mode hands
    back the masked string `"**********"` instead, which is indistinguishable
    from any other string and passes the guard uncaught. Either flavour
    reaching disk would overwrite a real secret with a placeholder on the
    next save — the guard only covers the first case, so the raw-mapping
    contract is load-bearing, not a suggestion.

    Only the keys present in `values` are written — an unset key stays
    absent so it keeps following jailbee's own default rather than
    freezing at today's value.

    Comments recurse into fields whose annotation *is* a model
    (`gpg`, `ssh`, `golden`, ...). Fields annotated as a collection of
    models (`agents: dict[str, AgentConfig]`, `host_mounts:
    list[HostMount]`) are written as plain data with only the outer key
    commented; per-entry documentation there would repeat the same text
    once per entry for no gain.
    """
    _reject_secrets(values)
    data = _documented_map(values, model, depth=0)
    stream = io.StringIO()
    _yaml().dump(data, stream)
    return header + stream.getvalue()


def _reject_secrets(value: object) -> None:
    """Raise if a raw `SecretStr` object is anywhere in the tree.

    `github.api_tokens` is `dict[str, SecretStr]`. A caller passing
    `model_dump()` output (python mode) would hand us live SecretStr
    objects, and rendering one would overwrite the user's real token with
    a placeholder on the next save — silent credential loss. This function
    catches that case.

    It does NOT catch `model_dump(mode="json")` output: pydantic renders a
    SecretStr there as the plain string `"**********"`, which is
    indistinguishable from any other string and passes this isinstance
    check uncaught. There is no reliable way to detect that case here — a
    legitimate config value could coincidentally *be* the string
    `"**********"` — so it isn't attempted. Callers must pass the raw YAML
    mapping, never either flavour of `model_dump()` output; see
    `render_documented`'s docstring.
    """
    from pydantic import SecretStr

    if isinstance(value, SecretStr):
        raise TypeError(
            "render_documented received a SecretStr. Pass the raw YAML mapping, "
            "not model_dump() output — rendering a masked value would destroy "
            "the stored secret."
        )
    if isinstance(value, dict):
        for item in value.values():
            _reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item)


_COMMENT_WIDTH = 100
"""Target column width for generated comment lines, matching `_yaml().width`.

A field's raw `description=` is one long sentence with no line breaks, so
without wrapping it becomes a single comment line as wide as the prose —
some run past 300 columns. `_yaml().width` only governs YAML scalar
folding, not comment text, so wrapping is done by hand before handing the
text to ruamel.
"""


def _wrap_description(description: str, *, indent: int) -> str:
    """Wrap `description` to `_COMMENT_WIDTH`, accounting for `indent` and
    the `# ` prefix ruamel adds to each line.

    Returns a `\\n`-joined string: `yaml_set_comment_before_after_key`
    splits its `before` argument on `\\n` and prefixes each resulting line
    with `#` on its own, so a multi-line `before` becomes one `#` comment
    line per line here, not a folded block.
    """
    width = max(20, _COMMENT_WIDTH - indent - len("# "))
    return "\n".join(textwrap.wrap(description, width=width))


def _documented_map(
    values: dict[str, object],
    model: type[BaseModel],
    *,
    depth: int,
) -> CommentedMap:
    out = CommentedMap()
    for key, value in values.items():
        info = model.model_fields.get(key)
        sub = _sub_model(info.annotation) if info is not None else None
        if sub is not None and isinstance(value, dict):
            out[key] = _documented_map(value, sub, depth=depth + 1)
        else:
            out[key] = value
        description = (info.description or "").strip() if info is not None else ""
        if description:
            # `indent` aligns the comment with its key's column; without it
            # ruamel emits a nested key's comment at column 0.
            indent = 2 * depth
            out.yaml_set_comment_before_after_key(
                key, before=_wrap_description(description, indent=indent), indent=indent
            )
    return out


def _sub_model(annotation: object) -> type[BaseModel] | None:
    """The BaseModel a field's annotation resolves to, if it is one."""
    from pydantic import BaseModel as _BaseModel

    if isinstance(annotation, type) and issubclass(annotation, _BaseModel):
        return annotation
    return None


def render_global_yaml(raw: dict[str, object], *, header: str = DOCUMENTED_HEADER) -> str:
    """Render a whole `global.yaml` mapping with generated comments.

    `global.yaml` is a two-model file: the keys in `_HOST_LEVEL_KEYS` are split
    out by `_split_host_keys` at load time and validated against
    `GlobalConfig`, while everything else overlays `Config`. Three keys are
    declared on both models with different shapes, so rendering the whole file
    against either model alone would document half of it wrongly. Two passes,
    in the same order the file has always had them: the Config overlay, then
    the host-level block.

    `raw` must be the raw YAML mapping, never `model_dump()` output — see
    `render_documented`, whose contract this inherits.
    """
    # Local imports: `jailbee.config` imports `ConfigError` from
    # `global_config`, so pulling either in at module top would close a cycle
    # through this module's own importers.
    from jailbee.config import Config
    from jailbee.config.common import _HOST_LEVEL_KEYS
    from jailbee.global_config import GlobalConfig

    host = {k: v for k, v in raw.items() if k in _HOST_LEVEL_KEYS}
    overlay = {k: v for k, v in raw.items() if k not in _HOST_LEVEL_KEYS}
    text = render_documented(overlay, Config, header=header)
    if host:
        text += "\n" + render_documented(host, GlobalConfig, header="")
    return text
