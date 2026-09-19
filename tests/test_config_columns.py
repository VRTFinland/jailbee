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
