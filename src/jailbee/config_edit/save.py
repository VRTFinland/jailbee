"""What a save would do, and then doing it.

Spec 3.5's save path, minus the validation step (that is `layers.validate`,
which runs the real loader and belongs next to the layers it reads). What is
left is: pick a write policy, render the file the policy asks for, show what
that changes, and write it atomically behind a backup.

Everything up to the write is a string transformation, so the whole policy and
diff story is testable without a terminal or a real config file.
"""

from __future__ import annotations

import difflib
import stat
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jailbee.config_edit.layers import LayerName, apply_changes, lookup, raw_for
from jailbee.config_writer import (
    patch_yaml,
    render_documented,
    render_global_yaml,
    write_text_atomic,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from jailbee.config_edit.layers import LayerSet
    from jailbee.config_edit.schema import FieldSpec
    from jailbee.config_writer import YamlChange

WritePolicy = Literal["patch", "regenerate"]

_DEFAULT_POLICY: dict[LayerName, WritePolicy] = {"global": "regenerate", "repo": "patch"}
"""The per-layer default `auto` resolves to (spec 2.4).

The two files have genuinely different ownership: `global.yaml` is jailbee's,
nobody reviews it, and its generated documentation is the product; a repo's
`.jailbee/config.yaml` is committed, team-owned, and read as a PR diff, where a
whole-file rewrite hides the one line that actually changed.
"""

_REPO_HEADER = (
    "# Written by `jailbee config edit --write regenerate`. Comments in this\n"
    "# file are generated from jailbee's own schema and are replaced whenever\n"
    "# it is regenerated — put notes you want to keep elsewhere. Every key is\n"
    "# optional; delete one to fall back to the built-in default.\n"
)

_MASK = "********"


def resolve_policy(
    layer: LayerName, *, flag: WritePolicy | None = None, configured: str = "auto"
) -> WritePolicy:
    """Which write policy this save uses.

    Three sources, in order: the `--write` flag (this invocation only), the
    `config_edit.write_policy` key, and the per-layer default. The flag beats
    the key deliberately — the key records a habit, the flag an intention about
    one file.
    """
    if flag is not None:
        return flag
    if configured == "patch":
        return "patch"
    if configured == "regenerate":
        return "regenerate"
    return _DEFAULT_POLICY[layer]


def configured_policy(global_path: Path) -> str:
    """`config_edit.write_policy` from `global.yaml`, or `"auto"`.

    Falls back rather than raising when the file will not load: the editor may
    be opened on a *repo* layer while `global.yaml` is broken, and refusing to
    start over a file this session is not touching would be the wrong trade
    (spec 10.6). The broken file still fails loudly at save time, through
    `layers.validate`, where it can actually be acted on.
    """
    from jailbee.config import ConfigError
    from jailbee.global_config import load_global_config

    try:
        gcfg, _ = load_global_config(global_path)
    except ConfigError:
        return "auto"
    return gcfg.config_edit.write_policy


def secret_values(raw: dict[str, object], specs: Sequence[FieldSpec]) -> tuple[str, ...]:
    """Every secret string stored in `raw`, longest first.

    Used to redact the diff preview. Reading the values out of the file rather
    than pattern-matching the diff text is what makes the redaction exact: the
    strings to hide are known, so a token cannot be missed because it happened
    to be quoted or folded differently.

    Longest first so a token that contains a shorter one is masked whole rather
    than leaving a tail behind.
    """
    out: set[str] = set()
    for spec in specs:
        if not spec.secret:
            continue
        present, value = lookup(raw, spec.path)
        if not present:
            continue
        if isinstance(value, dict):
            out.update(v for v in value.values() if isinstance(v, str) and v)
        elif isinstance(value, str) and value:
            out.add(value)
    return tuple(sorted(out, key=len, reverse=True))


def redact(text: str, secrets: Sequence[str]) -> str:
    """`text` with every string in `secrets` replaced by a fixed mask."""
    for secret in secrets:
        text = text.replace(secret, _MASK)
    return text


def render_layer(raw: dict[str, object], layer: LayerName) -> str:
    """A whole layer rendered from scratch, with generated comments.

    The `regenerate` policy's renderer. `global.yaml` needs the two-model split
    (`render_global_yaml`); a repo config is `Config` throughout.
    """
    from jailbee.config import Config

    if layer == "global":
        return render_global_yaml(raw)
    return render_documented(raw, Config, header=_REPO_HEADER)


@dataclass(frozen=True)
class SavePlan:
    """Everything a save would do, decided before anything is written."""

    path: Path
    policy: WritePolicy
    old_text: str
    new_text: str
    diff: str
    dropped_comments: tuple[str, ...]

    @property
    def must_confirm(self) -> bool:
        """Whether the user has to see the diff before this is written.

        Spec 2.5: a `regenerate` that drops hand-written comments is not a
        dismissible warning. Nothing else needs confirming — a `patch` changes
        only the keys it was given, and a `regenerate` over a generated file
        writes the same comments back.
        """
        return bool(self.dropped_comments)


def build_plan(
    layer_set: LayerSet,
    layer: LayerName,
    changes: Sequence[YamlChange],
    specs: Sequence[FieldSpec],
    policy: WritePolicy,
) -> SavePlan:
    """What saving `changes` into `layer` would produce. Writes nothing.

    The diff is redacted against the secrets *already on disk*. Staged secrets
    cannot exist: `render.edit_block` refuses to open an editor on a secret
    field, so nothing in `changes` can carry one.
    """
    path = layer_set.repo_path if layer == "repo" else layer_set.global_path
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    raw = raw_for(layer_set, layer)
    if policy == "patch":
        new_text = patch_yaml(old_text, changes)
    else:
        new_text = render_layer(apply_changes(raw, changes), layer)
    diff = redact(
        "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"{path} (on disk)",
                tofile=f"{path} (after save)",
            )
        ),
        secret_values(raw, specs),
    )
    dropped = _dropped_comments(old_text, new_text) if policy == "regenerate" else ()
    return SavePlan(
        path=path,
        policy=policy,
        old_text=old_text,
        new_text=new_text,
        diff=diff,
        dropped_comments=dropped,
    )


def _dropped_comments(old_text: str, new_text: str) -> tuple[str, ...]:
    """Comment lines the regenerated file does not carry, counting duplicates.

    A regenerate keeps the keys and re-derives every comment from the schema,
    so jailbee's own comments come back byte-identical and drop out of this
    comparison — what is left is exactly what a human wrote in the file. That
    is the set worth putting in front of someone before it disappears.

    Compared as a multiset (`Counter`), not a set: two hand-written comments
    that happen to strip to the same text — a repeated one-line divider, the
    same short note written twice — are two lines, and only one of them
    reappears when just one survives regeneration. A plain set comparison
    would see that surviving text as "kept" and silently clear the other
    occurrence too, which is exactly the case spec 2.5's confirmation exists
    to catch. Returned lines keep old_text's order of first appearance, since
    this order is what the confirmation screen shows.
    """
    old_lines = [line.strip() for line in old_text.splitlines() if line.strip().startswith("#")]
    new_counts = Counter(
        line.strip() for line in new_text.splitlines() if line.strip().startswith("#")
    )
    old_counts = Counter(old_lines)
    # Every comment text present in both keeps up to min(old, new) copies as
    # "kept"; anything beyond that count, per distinct text, is dropped.
    kept_budget = {text: min(count, new_counts[text]) for text, count in old_counts.items()}
    dropped: list[str] = []
    for line in old_lines:
        if kept_budget[line] > 0:
            kept_budget[line] -= 1
        else:
            dropped.append(line)
    return tuple(dropped)


def commit(plan: SavePlan) -> Path | None:
    """Write `plan`, keeping the previous contents in a `.bak` sibling.

    Returns the backup path, or `None` when there was no file to back up.

    The backup inherits the original's mode rather than the module default:
    `global.yaml` can carry `github.api_tokens`, and a backup of a 0600 file
    that lands at 0644 leaks exactly what the original's mode exists to
    protect. Both writes are atomic (`write_text_atomic`), so an interrupted
    save leaves the old file intact rather than a truncated one.

    Nothing is validated here — `layers.validate` has already run against the
    staged mapping by the time a plan is committed (spec 3.5 step 2), which is
    what makes it impossible for the editor to write a file the CLI would
    reject. This is a caller contract, not something this function checks or
    enforces: until the CLI wiring (Task 9) actually sequences validate then
    commit, nothing here stops `commit` from being called on an unvalidated
    plan.
    """
    backup: Path | None = None
    if plan.path.exists():
        mode = stat.S_IMODE(plan.path.stat().st_mode)
        backup = plan.path.with_name(plan.path.name + ".bak")
        write_text_atomic(backup, plan.old_text, mode=mode)
    write_text_atomic(plan.path, plan.new_text)
    return backup
