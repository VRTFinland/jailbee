"""Column-selection config model and its `ls:` / `dashboard:` block helpers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

# Columns the dashboard drops from the `ls` field set by default: REPO is
# redundant under per-repo grouping, the wide GIT STATUS combo and the
# JSON-only full_name add noise, and TTL is folded into the NETWORK cell.
# Lives here rather than in `dashboard.py` because it is a config default
# and `config.py` cannot import `dashboard` (that module imports this one).
DASHBOARD_DEFAULT_HIDE: tuple[str, ...] = (
    "repo",
    "full_name",
    "git_status",
    "created",
    "ttl",
)


class ColumnConfig(BaseModel):
    """Which columns a table shows.

    ``fields`` is an explicit ordered list and wins outright when set: naming
    a column is a request for that exact column, so it also renders even if
    it would otherwise be hidden by a dynamic ``show_if`` (e.g. ``pr`` with
    nothing open) — see ``table_format.apply_column_config``, the one place
    that rule is implemented, shared by ``jailbee ls`` and the dashboard alike.
    ``hide`` is subtractive and applies only to the built-in default set,
    where ``show_if`` still applies unchanged. A ``--fields`` flag on the
    command line beats both — this is the remembered preference, not a lock.

    Applies to table output only. ``--format json`` keeps its own
    ``default_json`` field set regardless of ``fields``/``hide`` here: this
    is a personal display preference, and a script depending on the default
    JSON shape must not have it silently narrowed by someone's ``global.yaml``.
    An explicit ``--fields`` flag still wins in every format.

    Column choice is a personal preference, so the normal home is
    ``global.yaml``; the same block in a repo's ``.jailbee/config.yaml``
    overrides it for everyone working in that repo, which is deliberate
    and rare.
    """

    model_config = ConfigDict(extra="forbid")
    fields: list[str] | None = None
    hide: list[str] = []


# Repo-layer default for both `Config.ls` and `Config.dashboard`. Unlike the
# global layer — where `GlobalConfig.dashboard`'s default already carries
# `DASHBOARD_DEFAULT_HIDE` (see `global_config._DASHBOARD_DEFAULT`) — a
# repo's own block defaults to a plain, unset `ColumnConfig`; the
# dashboard-hide default is applied later, when `dashboard.seed_view_state`
# reads the global block into a front-end's `view_prefs` row. So both repo
# fields share one default here. Used by `load_config`'s sanitize
# short-circuit.
_COLUMN_DEFAULT = ColumnConfig()


def _known_ls_field_names() -> set[str]:
    """Real `jailbee ls` / dashboard column names, including the LOCAL ones.

    Shared by ``validate_column_blocks`` and ``sanitize_column_blocks``, its
    recovery-flavoured counterpart. The canonical names come from
    ``lifecycle.ls_field_specs``, which ``config.py`` cannot import at
    module level (``lifecycle`` imports ``config`` — a cycle), hence the
    function-local import.
    """
    from jailbee.lifecycle import ls_field_specs

    return {f.name for f in ls_field_specs(now=datetime.now(UTC), all_repos=True)}


def validate_column_blocks(blocks: Sequence[tuple[str, ColumnConfig]]) -> list[str]:
    """Return human-readable problems in `ls:` / `dashboard:` column blocks.

    Used wherever a column typo should be *reported as an error*:
    ``Config.validate_runtime`` for a repo's ``.jailbee/config.yaml``, and
    ``jailbee config validate``'s own check of ``~/.config/jailbee/global.yaml`` (see
    ``global_config.global_config_issues``). Both are advisory-reporting
    paths — neither is on the hot path that actually renders a table, which
    is why raising is fine here but not in ``global_config.load_global_config``
    (see ``sanitize_column_blocks``, its recovery-flavoured counterpart used
    there: a personal display preference must never break unrelated work).

    Rejects three things: an unknown column name, ``fields: []`` (a table
    with no columns at all — ``fields: null`` is how you ask for the
    built-in default set), and a repeated name in ``fields`` (which would
    render that column twice).
    """
    known = _known_ls_field_names()
    allowed = ", ".join(sorted(known))
    issues: list[str] = []
    for block_name, block in blocks:
        if block.fields is not None and not block.fields:
            issues.append(
                f"{block_name}: fields is empty, which would render a table with "
                f"no columns; use `fields: null` for the built-in default set or "
                f"name at least one column"
            )
        seen: set[str] = set()
        for name in block.fields or []:
            if name in seen:
                issues.append(
                    f"{block_name}: duplicate field {name!r} in fields; "
                    f"each column may be named once"
                )
            seen.add(name)
        for name in list(block.fields or []) + list(block.hide):
            if name not in known:
                issues.append(f"{block_name}: unknown field {name!r}; allowed: {allowed}")
    return issues


def sanitize_column_blocks(
    blocks: Sequence[tuple[str, ColumnConfig]],
) -> tuple[dict[str, ColumnConfig], list[str]]:
    """Recover from problems in `ls:` / `dashboard:` blocks instead of rejecting them.

    Companion to ``validate_column_blocks``: same three problems, same
    "which names are real" data, opposite remedy. Used both by
    ``global_config.load_global_config`` (for ``global.yaml``) and by
    ``load_config`` (for a repo's ``.jailbee/config.yaml``) so that a typo'd
    column name — a purely cosmetic, personal display preference — never
    breaks an unrelated command in either file (``jailbee config validate`` is
    where a typo in either is still reported as an error, via
    ``validate_column_blocks``).

    * An unknown name is dropped (from ``fields`` or ``hide``).
    * A duplicate name in ``fields`` is dropped, keeping the first
      occurrence.
    * ``fields: []`` — explicit, or reduced to it by dropping every name as
      unknown/duplicate — falls back to ``fields: None`` (the built-in
      default set). There is no such thing as a table with zero columns, so
      unlike an unknown name (drop it, the rest of the list still means
      something) there is nothing sensible to recover *to* except the
      default; an explicit empty list is presumed to be a mistake rather
      than a real request for no columns.

    Returns the corrected blocks by name, plus one human-readable warning
    per fix made (empty when the input was already valid) for the caller to
    surface however it surfaces warnings — this function, like the rest of
    `config.py`, never prints.

    Each corrected block is produced with ``block.model_copy(update=...)``,
    touching only the sub-field(s) that actually needed a fix, rather than
    reconstructing a fresh ``ColumnConfig``. This matters for the repo layer:
    ``Config._effective_columns`` merges over the global block field-by-field
    keyed on ``ColumnConfig.model_fields_set`` (see its docstring), so a
    reconstruction that always passes both ``fields`` and ``hide`` would mark
    a field the repo never mentioned as "explicitly set" and make it
    unconditionally override the global value — corrupting the merge for
    every repo, not just the ones with a typo. A no-op ``model_copy()`` (or
    one that only updates the field(s) actually being fixed) leaves
    ``model_fields_set`` exactly as the caller set it.
    """
    known = _known_ls_field_names()
    allowed = ", ".join(sorted(known))
    warnings: list[str] = []
    fixed: dict[str, ColumnConfig] = {}

    for block_name, block in blocks:
        updates: dict[str, object] = {}

        hide: list[str] = []
        for name in block.hide:
            if name in known:
                hide.append(name)
            else:
                warnings.append(
                    f"{block_name}.hide: unknown field {name!r} ignored; allowed: {allowed}"
                )
        if hide != block.hide:
            updates["hide"] = hide

        if block.fields is None:
            pass  # nothing to fix: unset or explicit `null` both mean "no override"

        elif not block.fields:
            warnings.append(
                f"{block_name}.fields: empty, which would render a table with "
                f"no columns; using the built-in default set"
            )
            updates["fields"] = None

        else:
            seen: set[str] = set()
            cleaned: list[str] = []
            for name in block.fields:
                if name not in known:
                    warnings.append(
                        f"{block_name}.fields: unknown field {name!r} ignored; allowed: {allowed}"
                    )
                    continue
                if name in seen:
                    warnings.append(f"{block_name}.fields: duplicate field {name!r} ignored")
                    continue
                seen.add(name)
                cleaned.append(name)

            if not cleaned:
                warnings.append(
                    f"{block_name}.fields: no valid column names remained; "
                    f"using the built-in default set"
                )
                updates["fields"] = None
            elif cleaned != block.fields:
                updates["fields"] = cleaned

        fixed[block_name] = block.model_copy(update=updates) if updates else block

    return fixed, warnings


def _columns_already_sanitized(pairs: Sequence[tuple[ColumnConfig, ColumnConfig]]) -> bool:
    """True when every ``(block, that block's default)`` pair is equal by value.

    Lets a loader skip ``sanitize_column_blocks`` (and the `lifecycle` import
    and ``ls_field_specs()`` rebuild it needs) when there is provably nothing
    to sanitize — shared by ``load_global_config`` (global.yaml layer) and
    ``load_config`` (repo layer), since both run on the dashboard's
    refresh-cadence hot path and both have "no block set at all" as the
    overwhelmingly common case.

    Comparing by *value*, not ``model_fields_set``, is deliberate and safe:
    a default ``ColumnConfig`` is ``fields=None`` plus an already-valid
    ``hide`` list, so a block equal to it cannot contain an unknown name, a
    non-null empty ``fields``, or a duplicate — the three things
    ``sanitize_column_blocks`` recovers from. That holds even when the block
    was *explicitly* set to a value that happens to equal the default (e.g.
    an explicit ``hide: []`` matching a default of ``hide: []``):
    ``sanitize_column_blocks`` only ever inspects a block's ``fields``/``hide``
    values, never its ``model_fields_set``, so a value-equal block sanitizes
    to itself unconditionally regardless of how it got set. This is exactly
    the property the repo-vs-global merge (``Config._effective_columns``)
    depends on being preserved — see the real-load-path tests named
    `..._beats_a_nonempty_global` in ``test_config.py``.
    """
    return all(block == default for block, default in pairs)
