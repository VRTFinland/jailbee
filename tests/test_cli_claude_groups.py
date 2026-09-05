"""`jailbee claude group` — the CLI surface."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


@pytest.fixture
def group_env(mocker, tmp_path, monkeypatch):
    """A repo in group `work` with two containers, one deviating."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared")
    from jailbee import claude_groups

    cfg = cfg.model_copy(update={"claude_credentials_dir": claude_groups.group_dir("work")})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": "myrepo-a", "status": "Running", "profiles": [], "config": {}, "state": None},
        {
            "name": "myrepo-b",
            "status": "Running",
            "profiles": [],
            "config": {claude_groups.GROUP_LABEL: "personal"},
            "state": None,
        },
    ]
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    return cfg, incus


def test_status_names_the_repo_group_and_the_deviating_container(group_env):
    result = runner.invoke(app, ["claude", "group"])
    assert result.exit_code == 0
    assert "work" in result.output
    assert "myrepo-b" in result.output
    assert "personal" in result.output


def test_use_applies_the_override(group_env, mocker):
    _, _incus = group_env
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    setter = mocker.patch("jailbee.claude_groups.set_container_group")
    result = runner.invoke(app, ["claude", "group", "use", "personal", "myrepo-a"])
    assert result.exit_code == 0
    assert setter.call_args.args[3] == "personal"


def test_use_refuses_while_claude_runs(group_env, mocker):
    mocker.patch("jailbee.claude_groups.claude_running", return_value=True)
    setter = mocker.patch("jailbee.claude_groups.set_container_group")
    result = runner.invoke(app, ["claude", "group", "use", "personal", "myrepo-a"])
    assert result.exit_code != 0
    assert "--force" in result.output
    setter.assert_not_called()


def test_use_force_overrides_the_refusal(group_env, mocker):
    mocker.patch("jailbee.claude_groups.claude_running", return_value=True)
    setter = mocker.patch("jailbee.claude_groups.set_container_group")
    result = runner.invoke(app, ["claude", "group", "use", "personal", "myrepo-a", "--force"])
    assert result.exit_code == 0
    setter.assert_called_once()


def test_use_proceeds_when_the_probe_cannot_tell(group_env, mocker):
    """`None` is "cannot tell" — it must not read as a refusal."""
    mocker.patch("jailbee.claude_groups.claude_running", return_value=None)
    setter = mocker.patch("jailbee.claude_groups.set_container_group")
    result = runner.invoke(app, ["claude", "group", "use", "personal", "myrepo-a"])
    assert result.exit_code == 0
    setter.assert_called_once()


def test_use_none_sets_no_group(group_env, mocker):
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    setter = mocker.patch("jailbee.claude_groups.set_container_group")
    runner.invoke(app, ["claude", "group", "use", "none", "myrepo-a"])
    assert setter.call_args.args[3] is None


def test_use_rejects_a_bad_group_name(group_env, mocker):
    setter = mocker.patch("jailbee.claude_groups.set_container_group")
    result = runner.invoke(app, ["claude", "group", "use", "Work", "myrepo-a"])
    assert result.exit_code != 0
    setter.assert_not_called()


def test_use_without_a_container_errors_without_a_tty(group_env, mocker):
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    result = runner.invoke(app, ["claude", "group", "use", "personal"])
    assert result.exit_code != 0
    assert "myrepo-a" in result.output
    assert "myrepo-b" in result.output


def test_reset_clears_the_override(group_env, mocker):
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    clearer = mocker.patch("jailbee.claude_groups.clear_container_group")
    result = runner.invoke(app, ["claude", "group", "reset", "myrepo-b"])
    assert result.exit_code == 0
    clearer.assert_called_once()


def test_use_invalidates_the_repos_recorded_account(group_env, mocker):
    """§7.2: a stale `oauthAccount` would make the repo authoritative for the wrong account."""
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    mocker.patch("jailbee.claude_groups.set_container_group")
    invalidate = mocker.patch("jailbee.claude_pool.invalidate_identity", return_value=True)
    runner.invoke(app, ["claude", "group", "use", "personal", "myrepo-a"])
    invalidate.assert_called_once()


def test_set_invalidates_the_repos_recorded_account(group_env, mocker, tmp_path):
    """§7.2: a stale `oauthAccount` would make the repo authoritative for the wrong account."""
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    invalidate = mocker.patch("jailbee.claude_pool.invalidate_identity", return_value=True)

    result = runner.invoke(app, ["claude", "group", "set", "personal"])

    assert result.exit_code == 0
    invalidate.assert_called_once()


def test_unset_invalidates_the_repos_recorded_account(group_env, mocker, tmp_path):
    """§7.2: same rule as `set` — the recorded account can go stale either way."""
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n  repos:\n    myrepo: personal\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    invalidate = mocker.patch("jailbee.claude_pool.invalidate_identity", return_value=True)

    result = runner.invoke(app, ["claude", "group", "unset"])

    assert result.exit_code == 0
    invalidate.assert_called_once()


def test_set_writes_the_repo_group_to_global_yaml(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)

    result = runner.invoke(app, ["claude", "group", "set", "personal"])
    assert result.exit_code == 0

    import yaml

    loaded = yaml.safe_load(global_yaml.read_text())
    assert loaded["claude_credentials"]["repos"]["myrepo"] == "personal"
    # The host default is untouched — `set` is repo-scoped.
    assert loaded["claude_credentials"]["group"] == "work"


def test_set_none_writes_an_explicit_null(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)

    runner.invoke(app, ["claude", "group", "set", "none"])

    import yaml

    repos = yaml.safe_load(global_yaml.read_text())["claude_credentials"]["repos"]
    assert "myrepo" in repos and repos["myrepo"] is None


def test_unset_removes_the_entry(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n  repos:\n    myrepo: personal\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)

    runner.invoke(app, ["claude", "group", "unset"])

    import yaml

    assert "myrepo" not in yaml.safe_load(global_yaml.read_text())["claude_credentials"]["repos"]


def test_set_rejects_the_reserved_name_before_writing(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    before = global_yaml.read_bytes()
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)

    # `none` is the CLI's word for "no group", so it can never be written as a
    # group name; the refusal must come from a name that only *looks* usable.
    result = runner.invoke(app, ["claude", "group", "set", "Work"])
    assert result.exit_code != 0
    assert global_yaml.read_bytes() == before


def test_set_takes_no_container_argument(group_env):
    """Scope separation: the permanent verb must not accept a container."""
    result = runner.invoke(app, ["claude", "group", "set", "personal", "myrepo-a"])
    assert result.exit_code != 0


def test_set_refuses_while_claude_runs_anywhere_in_the_repo(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    before = global_yaml.read_bytes()
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    writer = mocker.patch("jailbee.cli._write_repo_group")
    # myrepo-b, not myrepo-a, is the one Claude is running in.
    mocker.patch(
        "jailbee.claude_groups.claude_running",
        side_effect=lambda cfg, incus, container: container == "myrepo-b",
    )

    result = runner.invoke(app, ["claude", "group", "set", "personal"])

    assert result.exit_code != 0
    assert "--force" in result.output
    assert "myrepo-b" in result.output
    writer.assert_not_called()
    assert global_yaml.read_bytes() == before


def test_set_force_overrides_the_refusal(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=True)

    result = runner.invoke(app, ["claude", "group", "set", "personal", "--force"])

    assert result.exit_code == 0
    import yaml

    loaded = yaml.safe_load(global_yaml.read_text())
    assert loaded["claude_credentials"]["repos"]["myrepo"] == "personal"


def test_unset_refuses_while_claude_runs_anywhere_in_the_repo(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n  repos:\n    myrepo: personal\n")
    before = global_yaml.read_bytes()
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    writer = mocker.patch("jailbee.cli._write_repo_group")
    mocker.patch(
        "jailbee.claude_groups.claude_running",
        side_effect=lambda cfg, incus, container: container == "myrepo-a",
    )

    result = runner.invoke(app, ["claude", "group", "unset"])

    assert result.exit_code != 0
    assert "--force" in result.output
    assert "myrepo-a" in result.output
    writer.assert_not_called()
    assert global_yaml.read_bytes() == before


def test_unset_force_overrides_the_refusal(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n  repos:\n    myrepo: personal\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=True)

    result = runner.invoke(app, ["claude", "group", "unset", "--force"])

    assert result.exit_code == 0
    import yaml

    assert "myrepo" not in yaml.safe_load(global_yaml.read_text())["claude_credentials"]["repos"]


def test_claude_ls_group_flag_points_at_another_holder(group_env, mocker):
    from jailbee import claude_groups

    captured = {}

    def fake_list_slots(cfg, gcfg, *, authoritative):
        captured["holder"] = cfg.claude_credentials_dir
        return []

    mocker.patch("jailbee.claude_pool.list_slots", side_effect=fake_list_slots)
    mocker.patch("jailbee.claude_pool.members", return_value=([], []))
    runner.invoke(app, ["claude", "ls", "-g", "personal"])
    assert captured["holder"] == claude_groups.group_dir("personal")


def test_claude_ls_without_the_flag_uses_the_repo_group(group_env, mocker):
    from jailbee import claude_groups

    captured = {}

    def fake_list_slots(cfg, gcfg, *, authoritative):
        captured["holder"] = cfg.claude_credentials_dir
        return []

    mocker.patch("jailbee.claude_pool.list_slots", side_effect=fake_list_slots)
    mocker.patch("jailbee.claude_pool.members", return_value=([], []))
    runner.invoke(app, ["claude", "ls"])
    assert captured["holder"] == claude_groups.group_dir("work")


@pytest.fixture
def holder_view_env(mocker, tmp_path, monkeypatch):
    """A repo in group `work`, registered, with one container that inherits.

    Deliberately *not* `group_env`: every container inherits, so the repo is
    authoritative for its own group — the state in which a poisoned
    `oauthAccount` turns into a wrongly named park.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from jailbee import claude_groups
    from jailbee.global_config import GlobalConfig
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared")
    cfg = cfg.model_copy(update={"claude_credentials_dir": claude_groups.group_dir("work")})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig.model_validate({"claude_credentials": {"group": "work"}}),
    )
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": "myrepo-a", "status": "Running", "profiles": [], "config": {}, "state": None},
    ]
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.claude_pool.registered_repos", return_value=[("myrepo", cfg.repo_root)])
    return cfg


def _write_json(path, payload):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_park_on_another_group_leaves_this_repos_recorded_account_alone(holder_view_env, mocker):
    """`-g` acts on a holder this repo is not a member of, so the repo's own
    `oauthAccount` — which describes *its* group's login — must not be touched.
    Clearing it there is how a later `jailbee claude park` loses its name."""
    import json

    from jailbee import claude_groups, claude_pool

    cfg = holder_view_env
    home = claude_pool.config_home(cfg)
    _write_json(home / ".claude.json", {"oauthAccount": {"emailAddress": "work@example.com"}})
    _write_json(
        claude_groups.group_dir("personal") / ".credentials.json",
        {"claudeAiOauth": {"refreshToken": "rt-personal"}},
    )

    result = runner.invoke(app, ["claude", "park", "-g", "personal"])

    assert result.exit_code == 0
    assert "myrepo" not in result.output.replace("myrepo-a", "")
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"] == {
        "emailAddress": "work@example.com"
    }


def test_use_on_another_group_cannot_rename_this_repos_next_park(holder_view_env):
    """The severe half: `use -g` wrote the other group's account into this
    repo's config home, and the next `park` of the repo's *own* holder then
    named that file after the wrong account — name and record agreeing, both
    wrong, which is the failure the authoritative-member rule exists to
    prevent."""
    import json

    from jailbee import claude_groups, claude_pool

    cfg = holder_view_env
    home = claude_pool.config_home(cfg)
    _write_json(home / ".claude.json", {"oauthAccount": {"emailAddress": "work@example.com"}})
    _write_json(
        claude_pool.store_dir() / "personal@example.com.json",
        {
            "claudeAiOauth": {"refreshToken": "rt-personal"},
            claude_pool.ACCOUNT_RECORD_KEY: {"emailAddress": "personal@example.com"},
        },
    )
    _write_json(
        claude_groups.group_dir("work") / ".credentials.json",
        {"claudeAiOauth": {"refreshToken": "rt-work"}},
    )
    claude_groups.group_dir("personal").mkdir(parents=True, exist_ok=True)

    assert (
        runner.invoke(app, ["claude", "use", "personal@example.com", "-g", "personal"]).exit_code
        == 0
    )
    assert runner.invoke(app, ["claude", "park"]).exit_code == 0

    stored = {
        p.name: json.loads(p.read_text())["claudeAiOauth"]["refreshToken"]
        for p in claude_pool.store_dir().glob("*.json")
    }
    assert stored.get("personal@example.com.json") != "rt-work"
    assert "rt-work" in stored.values()


def test_park_on_another_group_keeps_the_name_jailbee_activated(holder_view_env):
    """The reported bug, end to end: `-g` acts on a holder no repo resolves to,
    so no config home describes it, and a park of the login jailbee had just
    activated there could only be named `unknown-<timestamp>`."""
    import json

    from jailbee import claude_groups, claude_pool

    _write_json(
        claude_pool.store_dir() / "personal@example.com.json",
        {
            "claudeAiOauth": {"refreshToken": "rt-personal"},
            claude_pool.ACCOUNT_RECORD_KEY: {"emailAddress": "personal@example.com"},
        },
    )
    claude_groups.group_dir("personal").mkdir(parents=True, exist_ok=True)

    assert (
        runner.invoke(app, ["claude", "use", "personal@example.com", "-g", "personal"]).exit_code
        == 0
    )
    result = runner.invoke(app, ["claude", "park", "-g", "personal"])

    assert result.exit_code == 0
    assert "unknown-" not in result.output
    assert "personal@example.com" in result.output
    stored = claude_pool.store_dir() / "personal@example.com.json"
    assert json.loads(stored.read_text())["claudeAiOauth"]["refreshToken"] == "rt-personal"
