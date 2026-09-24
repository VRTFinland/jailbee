"""Tests for `jailbee config migrate`'s planner and writer."""

import stat
from pathlib import Path

import pytest
import yaml

from jailbee.config.local_layer import local_config_path
from jailbee.config_migrate import MigrationInputs, apply_plan, plan_migrations, render_diff
from jailbee.global_config import default_global_config_path


def _inputs(global_data: dict | None, locals_: dict[str, dict] | None = None, rows=None):
    gpath = default_global_config_path()
    texts: dict[Path, str] = {}
    if global_data is not None:
        texts[gpath] = yaml.safe_dump(global_data, sort_keys=False)
    for prefix, data in (locals_ or {}).items():
        texts[local_config_path(prefix)] = yaml.safe_dump(data, sort_keys=False)
    return MigrationInputs(global_path=gpath, texts=texts, egress_rows=rows or {})


def _load(plan, path, inputs=None):
    text = plan.new_texts.get(path, inputs.texts.get(path, "") if inputs else "")
    return yaml.safe_load(text) or {}


def test_nothing_to_migrate():
    plan = plan_migrations(_inputs({"egress_allow": ["x.org"]}))
    assert not plan.pending
    assert plan.steps == ()


def test_api_tokens_move_to_local_files():
    inputs = _inputs({"github": {"enabled": True, "api_tokens": {"a": "ghp_a", "b": "ghp_b"}}})
    plan = plan_migrations(inputs)
    assert _load(plan, local_config_path("a")) == {"github": {"token": "ghp_a"}}
    assert _load(plan, local_config_path("b")) == {"github": {"token": "ghp_b"}}
    assert _load(plan, inputs.global_path) == {"github": {"enabled": True}}


def test_conflicting_local_token_is_skipped_and_reported():
    inputs = _inputs(
        {"github": {"api_tokens": {"a": "ghp_old"}}},
        {"a": {"github": {"token": "ghp_new"}}},
    )
    plan = plan_migrations(inputs)
    assert any("github.api_tokens.a" in c for c in plan.conflicts)
    assert "api_tokens" in _load(plan, inputs.global_path, inputs)["github"]
    assert local_config_path("a") not in plan.new_texts or _load(plan, local_config_path("a")) == {
        "github": {"token": "ghp_new"}
    }


def test_identical_local_token_just_drops_the_legacy_entry():
    inputs = _inputs(
        {"github": {"api_tokens": {"a": "ghp_x"}}}, {"a": {"github": {"token": "ghp_x"}}}
    )
    plan = plan_migrations(inputs)
    assert plan.conflicts == ()
    assert "api_tokens" not in (_load(plan, inputs.global_path).get("github") or {})


def test_credentials_repos_move_including_null():
    inputs = _inputs({"credentials": {"group": "shared", "repos": {"a": "team", "b": None}}})
    plan = plan_migrations(inputs)
    assert _load(plan, local_config_path("a")) == {"credentials": {"group": "team"}}
    assert _load(plan, local_config_path("b")) == {"credentials": {"group": None}}
    assert _load(plan, inputs.global_path) == {"credentials": {"group": "shared"}}


def test_legacy_claude_credentials_is_renamed_before_its_repos_move():
    inputs = _inputs({"claude_credentials": {"repos": {"a": "team"}}})
    plan = plan_migrations(inputs)
    ids = [s.migration_id for s in plan.steps]
    assert ids.index("claude-credentials-key") < ids.index("credentials-repos")
    assert _load(plan, local_config_path("a")) == {"credentials": {"group": "team"}}
    assert "claude_credentials" not in _load(plan, inputs.global_path)


def test_chrome_block_folds_into_browsers():
    inputs = _inputs({"chrome": {"enabled": True}})
    plan = plan_migrations(inputs)
    migrated = _load(plan, inputs.global_path)
    assert "chrome" not in migrated
    assert migrated["browsers"]["chrome"]["enabled"] is True
    assert migrated["browsers"]["chrome"]["source"] == "host"


def test_egress_rows_append_without_duplicates():
    inputs = _inputs({}, {"a": {"egress_allow": ["x.org"]}}, rows={"a": ["x.org", "y.org"]})
    plan = plan_migrations(inputs)
    assert _load(plan, local_config_path("a"))["egress_allow"] == ["x.org", "y.org"]
    assert set(plan.rows_to_delete) == {("a", "x.org"), ("a", "y.org")}


def test_diff_never_shows_a_token():
    inputs = _inputs({"github": {"api_tokens": {"a": "ghp_secret"}}})
    assert "ghp_secret" not in render_diff(inputs, plan_migrations(inputs))


def test_apply_writes_private_files_backs_up_and_deletes_rows(db_session, frozen_now):
    from jailbee.db.models import EgressOverride

    gpath = default_global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text("github:\n  api_tokens:\n    a: ghp_a\n")
    gpath.chmod(0o600)
    db_session.add(EgressOverride(container_prefix="a", entry="y.org", added_at=frozen_now))
    db_session.commit()

    from jailbee.config_migrate import gather_inputs

    inputs = gather_inputs(db_session)
    backups = apply_plan(inputs, plan_migrations(inputs), db_session)

    local = local_config_path("a")
    assert stat.S_IMODE(local.stat().st_mode) == 0o600
    assert yaml.safe_load(local.read_text()) == {
        "github": {"token": "ghp_a"},
        "egress_allow": ["y.org"],
    }
    assert gpath.with_name("global.yaml.bak") in backups
    assert stat.S_IMODE(gpath.with_name("global.yaml.bak").stat().st_mode) == 0o600
    assert db_session.get(EgressOverride, ("a", "y.org")) is None
    assert not plan_migrations(gather_inputs(db_session)).pending


def test_apply_writes_nothing_when_a_result_fails_validation(db_session):
    gpath = default_global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text("credentials:\n  repos:\n    a: team\n")
    bad = local_config_path("a")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("egress_allow: not-a-list\n")

    from jailbee.config import ConfigError
    from jailbee.config_migrate import gather_inputs

    inputs = gather_inputs(db_session)
    before = {p: p.read_text() for p in (gpath, bad)}
    with pytest.raises(ConfigError, match=str(bad)):
        apply_plan(inputs, plan_migrations(inputs), db_session)
    assert {p: p.read_text() for p in (gpath, bad)} == before
