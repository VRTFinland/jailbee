"""Canonical column-name normalization for `ls:` / `dashboard:` blocks.

The credential-group column was renamed from the Claude-specific
``claude_group`` to the agent-agnostic ``group``. Configured blocks (global
and repo) and an explicit ``--fields`` string still spelling either alias must
resolve to the canonical column, not be dropped as an unknown name.
"""

from __future__ import annotations

from jailbee.config import ColumnConfig, models_columns


def test_canonical_ls_field_maps_the_shipped_alias() -> None:
    assert models_columns.canonical_ls_field("claude_group") == "group"


def test_canonical_ls_field_maps_the_short_alias() -> None:
    assert models_columns.canonical_ls_field("claude") == "group"


def test_canonical_ls_field_leaves_a_canonical_name_alone() -> None:
    assert models_columns.canonical_ls_field("group") == "group"
    assert models_columns.canonical_ls_field("name") == "name"


def test_canonical_ls_fields_spec_normalizes_each_name() -> None:
    assert models_columns.canonical_ls_fields_spec("name, claude_group") == "name,group"
    assert models_columns.canonical_ls_fields_spec("name,claude,bogus") == "name,group,bogus"


def test_canonical_ls_fields_spec_passes_none_through() -> None:
    assert models_columns.canonical_ls_fields_spec(None) is None


def test_validate_column_blocks_accepts_both_aliases() -> None:
    issues = models_columns.validate_column_blocks(
        [("ls", ColumnConfig(fields=["name", "claude"], hide=["claude_group"]))]
    )
    assert issues == []


def test_validate_column_blocks_flags_a_duplicate_the_aliases_collapse_into() -> None:
    issues = models_columns.validate_column_blocks(
        [("ls", ColumnConfig(fields=["group", "claude"]))]
    )
    assert any("duplicate field 'group'" in issue for issue in issues)


def test_sanitize_column_blocks_canonicalizes_fields_and_hide() -> None:
    fixed, warnings = models_columns.sanitize_column_blocks(
        [("ls", ColumnConfig(fields=["name", "claude"], hide=["claude_group"]))]
    )
    assert fixed["ls"].fields == ["name", "group"]
    assert fixed["ls"].hide == ["group"]
    assert warnings == []


def test_sanitize_column_blocks_keeps_fields_explicitly_set_when_rewriting() -> None:
    """The repo-vs-global merge keys on `model_fields_set`; a rewrite must not
    make a field the repo never mentioned look explicitly set (or vice versa)."""
    fixed, _ = models_columns.sanitize_column_blocks([("ls", ColumnConfig(fields=["claude"]))])
    assert "fields" in fixed["ls"].model_fields_set


def test_sanitize_column_blocks_does_not_invent_fields_when_rewriting_hide() -> None:
    """The direction that can actually regress: a block naming only `hide` must
    not come back with `fields` in `model_fields_set`, or the repo layer would
    start overriding the global `fields` it never mentioned."""
    fixed, _ = models_columns.sanitize_column_blocks([("ls", ColumnConfig(hide=["claude_group"]))])
    assert fixed["ls"].hide == ["group"]
    assert "fields" not in fixed["ls"].model_fields_set


def test_canonicalize_column_config_rewrites_both_lists() -> None:
    block = models_columns.canonicalize_column_config(
        ColumnConfig(fields=["name", "claude"], hide=["claude_group"])
    )

    assert block.fields == ["name", "group"]
    assert block.hide == ["group"]


def test_canonicalize_column_config_returns_the_same_block_when_nothing_changes() -> None:
    """Identity, not just equality: the caller's `model_fields_set` has to
    survive untouched for the repo-vs-global merge in `_effective_columns`."""
    block = ColumnConfig(hide=["ip"])

    assert models_columns.canonicalize_column_config(block) is block


def test_load_global_config_canonicalizes_an_alias_without_warning(tmp_path) -> None:
    """A `dashboard:`/`ls:` block spelling the pre-rename column name must reach
    the loaded config canonicalized. An alias rewrite produces no warning, so a
    loader that only applied the sanitized blocks when something warned used to
    drop it — and the dashboard then persisted the un-hidden column for good.
    """
    from jailbee.global_config import load_global_config

    path = tmp_path / "global.yaml"
    path.write_text("dashboard:\n  hide: [claude_group]\nls:\n  fields: [name, claude]\n")

    gcfg, warnings = load_global_config(path)

    assert warnings == []
    assert gcfg.dashboard.hide == ["group"]
    assert gcfg.ls.fields == ["name", "group"]


def test_load_config_canonicalizes_an_alias_in_the_repo_layer(tmp_path) -> None:
    """The repo layer's twin of the check above."""
    from jailbee.config import load_config

    repo = tmp_path / "repo"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".jailbee" / "config.yaml").write_text(
        "container_prefix: demo\nls:\n  hide: [claude_group]\n"
    )

    cfg = load_config(repo / ".jailbee" / "config.yaml")

    assert cfg.ls.hide == ["group"]


def test_the_documented_field_vocabularies_match_the_real_one() -> None:
    """Both places that spell out the `--fields` vocabulary must list every
    real column.

    These lists have drifted twice already — `group` was added to
    `docs/config.md` and not to the in-container skill reference, and `cpu` /
    `doing` reached neither. The skill reference is what the agent inside a
    container answers `jailbee ls --fields` questions from, so a stale list
    there is a wrong answer to a user, not just a docs nit.
    """
    import re
    from datetime import UTC, datetime
    from pathlib import Path

    from jailbee.lifecycle import ls_field_specs

    real = {f.name for f in ls_field_specs(now=datetime.now(UTC))}
    root = Path(__file__).resolve().parent.parent
    sources = {
        "docs/config.md": r"Allowed names \(also the `jailbee ls --fields` vocabulary\):(.+?)\n\n",
        "docs/skills/jailbee-usage/references/commands.md": r"Allowed: `([^`]+)`",
    }

    for rel, pattern in sources.items():
        text = (root / rel).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.DOTALL)
        assert match is not None, f"{rel}: could not find the allowed-names list"
        documented = set(re.findall(r"[a-z_]+", match.group(1).replace("`", " ")))
        assert real - documented == set(), f"{rel} omits: {sorted(real - documented)}"
