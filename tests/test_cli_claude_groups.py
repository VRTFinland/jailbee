"""`jailbee claude group` — the CLI surface."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import claude_overview_of, claude_row
from tests.conftest import flat_output as _flat

runner = CliRunner()


@pytest.fixture
def group_env(mocker, tmp_path, monkeypatch):
    """A repo in group `work` with two containers, one deviating."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared", claude={"enabled": True})
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
    # No label unless a test says otherwise: `container_override` feeds this
    # straight into a regex, and a bare MagicMock raises there.
    incus.config_get.return_value = None
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    return cfg, incus


def test_bare_group_is_a_command_group_not_a_status_command(group_env):
    """`jailbee claude ls` states which holder this repo reads, `jailbee ls`'s
    CLAUDE column the per-container labels, and `jailbee doctor` the overrides
    that only repeat the repo — so a fourth, partial view here was just one
    more place to disagree with them."""
    result = runner.invoke(app, ["claude", "group"])

    assert "create" in result.output
    assert "rm" in result.output
    assert "Usage" in result.output
    # The facts the old status command printed are not printed here any more.
    assert "Containers with a temporary override" not in result.output
    assert "myrepo-b" not in result.output


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


def test_claude_ls_never_hands_the_overview_a_holder_view(group_env, mocker):
    """`-g` narrows the host-wide table; it must not point the *config* at
    another group. A holder view keeps the calling repo's config home while
    naming another group's directory (see `cli._holder_view`), and
    `claude_overview.build` reads both."""
    from jailbee import claude_groups, claude_overview

    captured = {}

    def fake_build(cfg, gcfg, incus):
        captured["holder"] = cfg.claude_credentials_dir
        return claude_overview.Overview(rows=(), unreachable=(), containers_known=True)

    mocker.patch("jailbee.claude_overview.build", side_effect=fake_build)

    runner.invoke(app, ["claude", "ls", "-g", "personal"])

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


# --- An override that would only repeat the repo's group ----------------------


def _labels(mocker, **labels: str):
    """Point `container_override`'s `config_get` at a per-container label."""
    from jailbee import claude_groups

    def fake(container: str, key: str) -> str | None:
        assert key == claude_groups.GROUP_LABEL
        return labels.get(container)

    return fake


def test_use_of_the_repos_own_group_drops_the_override_instead(group_env, mocker):
    """Moving a container back onto the repo's own group must leave no override
    behind: the label outranks the profile, so the container would otherwise
    stay on `work` the next time the repo's group changed."""
    _, incus = group_env
    incus.config_get.side_effect = _labels(mocker, **{"myrepo-b": "personal"})
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    setter = mocker.patch("jailbee.claude_groups.set_container_group")
    clearer = mocker.patch("jailbee.claude_groups.clear_container_group")

    result = runner.invoke(app, ["claude", "group", "use", "work", "myrepo-b"])

    assert result.exit_code == 0, result.output
    clearer.assert_called_once_with(incus, "myrepo-b")
    setter.assert_not_called()


def test_use_of_the_repos_own_group_says_no_override_was_written(group_env, mocker):
    _, incus = group_env
    incus.config_get.side_effect = _labels(mocker, **{"myrepo-b": "personal"})
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    mocker.patch("jailbee.claude_groups.clear_container_group")

    result = runner.invoke(app, ["claude", "group", "use", "work", "myrepo-b"])

    assert "override" in result.output
    assert "work" in result.output


def test_use_of_the_repos_own_group_keeps_the_account_when_nothing_changes(group_env, mocker):
    """A container that already inherits `work` does not change holder, so
    invalidating the repo's `oauthAccount` would throw away a valid name for
    nothing — the repo then cannot name its login until a container runs
    Claude again."""
    _, incus = group_env
    incus.config_get.side_effect = _labels(mocker)  # no labels: everything inherits
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    mocker.patch("jailbee.claude_groups.clear_container_group")
    invalidate = mocker.patch("jailbee.claude_pool.invalidate_identity", return_value=True)

    result = runner.invoke(app, ["claude", "group", "use", "work", "myrepo-a"])

    assert result.exit_code == 0, result.output
    invalidate.assert_not_called()


def test_use_of_the_repos_own_group_invalidates_when_the_holder_changes(group_env, mocker):
    """The other half: `myrepo-b` really moves from `personal` to `work`, so
    the recorded account does go stale."""
    _, incus = group_env
    incus.config_get.side_effect = _labels(mocker, **{"myrepo-b": "personal"})
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    mocker.patch("jailbee.claude_groups.clear_container_group")
    invalidate = mocker.patch("jailbee.claude_pool.invalidate_identity", return_value=True)

    runner.invoke(app, ["claude", "group", "use", "work", "myrepo-b"])

    invalidate.assert_called_once()


def test_reset_keeps_the_account_when_the_override_was_redundant(group_env, mocker):
    _, incus = group_env
    incus.config_get.side_effect = _labels(mocker, **{"myrepo-b": "work"})
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    mocker.patch("jailbee.claude_groups.clear_container_group")
    invalidate = mocker.patch("jailbee.claude_pool.invalidate_identity", return_value=True)

    result = runner.invoke(app, ["claude", "group", "reset", "myrepo-b"])

    assert result.exit_code == 0, result.output
    invalidate.assert_not_called()


def test_set_drops_an_override_the_change_made_redundant(group_env, mocker, tmp_path):
    """Y→X while a container is overridden to X: that container's override is
    now leftover state, and the whole point of `set` is that every container
    of the repo follows it."""
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    clearer = mocker.patch("jailbee.claude_groups.clear_container_group")

    result = runner.invoke(app, ["claude", "group", "set", "personal"])

    assert result.exit_code == 0, result.output
    _, incus = group_env
    clearer.assert_called_once_with(incus, "myrepo-b")
    assert "myrepo-b" in result.output


def test_set_keeps_an_override_that_still_deviates(group_env, mocker, tmp_path):
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    clearer = mocker.patch("jailbee.claude_groups.clear_container_group")

    result = runner.invoke(app, ["claude", "group", "set", "third"])

    assert result.exit_code == 0, result.output
    clearer.assert_not_called()


def test_set_re_renders_the_profile_before_dropping_an_override(group_env, mocker, tmp_path):
    """Order matters: the local device is what mounts the credential until the
    profile carries the new one, so clearing first leaves the container
    without a credential in between."""
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: work\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    order: list[str] = []
    mocker.patch(
        "jailbee.cli._reapply_binds_profile", side_effect=lambda *a, **k: order.append("profile")
    )
    mocker.patch(
        "jailbee.claude_groups.clear_container_group",
        side_effect=lambda *a, **k: order.append("clear"),
    )

    runner.invoke(app, ["claude", "group", "set", "personal"])

    assert order == ["profile", "clear"]


def test_unset_drops_an_override_the_host_default_made_redundant(group_env, mocker, tmp_path):
    """After the entry is gone the repo follows the host default `personal`,
    which is exactly what `myrepo-b` was overridden to."""
    global_yaml = tmp_path / "global.yaml"
    global_yaml.write_text("claude_credentials:\n  group: personal\n  repos:\n    myrepo: work\n")
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=global_yaml)
    mocker.patch("jailbee.cli._reapply_binds_profile")
    mocker.patch("jailbee.claude_groups.claude_running", return_value=False)
    clearer = mocker.patch("jailbee.claude_groups.clear_container_group")

    result = runner.invoke(app, ["claude", "group", "unset"])

    assert result.exit_code == 0, result.output
    _, incus = group_env
    clearer.assert_called_once_with(incus, "myrepo-b")


# --- Creating and removing a credential group --------------------------------


def test_create_makes_the_directory_0700(group_env):
    import stat

    from jailbee import claude_groups

    result = runner.invoke(app, ["claude", "group", "create", "fresh"])

    assert result.exit_code == 0, result.output
    created = claude_groups.group_dir("fresh")
    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_create_names_what_to_do_with_the_new_group(group_env):
    """An empty group does nothing on its own, and the three ways to put it to
    use are not guessable from `--help` alone."""
    result = runner.invoke(app, ["claude", "group", "create", "fresh"])

    assert "group set" in result.output
    assert "claude use -g" in result.output


def test_create_is_idempotent(group_env):
    from jailbee import claude_groups

    claude_groups.group_dir("fresh").mkdir(parents=True)

    result = runner.invoke(app, ["claude", "group", "create", "fresh"])

    assert result.exit_code == 0, result.output
    assert "already" in result.output


@pytest.mark.parametrize("bad", ["none", "Fresh", "_fresh", "../etc"])
def test_create_refuses_a_name_it_could_not_address(group_env, bad):
    """`none` is the CLI's word for "no group", and the rest are outside the
    grammar that makes a group name safe as a path component."""
    result = runner.invoke(app, ["claude", "group", "create", bad])

    assert result.exit_code == 2
    # The refusal has to be *this* command's, not typer's "no such command":
    # the latter would make this test pass before the command existed.
    assert bad in result.output
    assert "No such command" not in result.output


def test_rm_removes_an_unused_empty_group(group_env):
    from jailbee import claude_groups

    claude_groups.group_dir("demo").mkdir(parents=True)

    result = runner.invoke(app, ["claude", "group", "rm", "demo"])

    assert result.exit_code == 0, result.output
    assert not claude_groups.group_dir("demo").exists()


def test_rm_of_a_group_that_does_not_exist_is_not_an_error(group_env):
    """The end state the caller asked for already holds."""
    result = runner.invoke(app, ["claude", "group", "rm", "gone"])

    assert result.exit_code == 0, result.output


def test_rm_refuses_while_a_repo_resolves_to_the_group(holder_view_env, mocker):
    """`jailbee apply` would recreate the directory, and until it ran those
    repos' containers would mount an empty one.

    The group is reached through a `repos:` entry rather than the host
    default, so it is the *member* refusal being tested and not the
    host-default one, which fires first and says something else.
    """
    from jailbee import claude_groups
    from jailbee.global_config import GlobalConfig

    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig.model_validate(
            {"claude_credentials": {"repos": {"myrepo": "work"}}}
        ),
    )
    claude_groups.group_dir("work").mkdir(parents=True)

    result = runner.invoke(app, ["claude", "group", "rm", "work"])

    assert result.exit_code == 2
    assert "myrepo" in result.output
    assert claude_groups.group_dir("work").exists()


def test_rm_refuses_the_host_default_even_with_no_repos(group_env, mocker):
    """The repo check reads the registry; an empty one must not make the host's
    own default look unused."""
    from jailbee import claude_groups
    from jailbee.global_config import GlobalConfig

    mocker.patch("jailbee.claude_pool.registered_repos", return_value=[])
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig.model_validate({"claude_credentials": {"group": "demo"}}),
    )
    claude_groups.group_dir("demo").mkdir(parents=True)

    result = runner.invoke(app, ["claude", "group", "rm", "demo"])

    assert result.exit_code == 2
    assert "global.yaml" in result.output
    assert claude_groups.group_dir("demo").exists()


def test_rm_refuses_while_a_container_is_overridden_to_the_group(group_env, mocker):
    """`myrepo-b` reads `personal` through its own label, and removing the
    directory under it would leave it mounting nothing."""
    from jailbee import claude_groups

    mocker.patch("jailbee.claude_pool.registered_repos", return_value=[])
    claude_groups.group_dir("personal").mkdir(parents=True)

    result = runner.invoke(app, ["claude", "group", "rm", "personal"])

    assert result.exit_code == 2
    assert "myrepo-b" in result.output
    assert "group reset" in result.output
    assert claude_groups.group_dir("personal").exists()


def test_rm_parks_a_login_before_removing_the_group(group_env, mocker):
    from jailbee import claude_groups, claude_pool

    mocker.patch("jailbee.claude_pool.registered_repos", return_value=[])
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    holder = claude_groups.group_dir("demo")
    holder.mkdir(parents=True)
    (holder / claude_pool.CREDENTIAL_FILE).write_text("{}")
    parked = claude_pool.PoolChange(
        parked_as="demo@corp.com",
        activated=None,
        updated=[],
        not_updated=[],
        live_sessions=[],
    )
    # The real `park` empties the holder, which is what lets the `rmdir`
    # below succeed — a mock that only returned would leave the credential
    # in place and hide a broken order of operations.
    park = mocker.patch(
        "jailbee.claude_pool.park",
        side_effect=lambda *a, **k: (
            (holder / claude_pool.CREDENTIAL_FILE).unlink(),
            parked,
        )[1],
    )

    result = runner.invoke(app, ["claude", "group", "rm", "demo"], input="y\n")

    assert result.exit_code == 0, result.output
    park.assert_called_once()
    # The holder view, not this repo's own config: parking through the caller's
    # holder would store the wrong group's login.
    assert park.call_args.args[0].claude_credentials_dir == holder
    assert "demo@corp.com" in result.output
    assert not holder.exists()


def test_rm_leaves_the_login_alone_when_the_confirmation_is_declined(group_env, mocker):
    from jailbee import claude_groups, claude_pool

    mocker.patch("jailbee.claude_pool.registered_repos", return_value=[])
    # Without this the command refuses for want of a TTY, and the test would
    # pass without ever reaching the prompt it is about.
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    holder = claude_groups.group_dir("demo")
    holder.mkdir(parents=True)
    (holder / claude_pool.CREDENTIAL_FILE).write_text("{}")
    park = mocker.patch("jailbee.claude_pool.park")

    result = runner.invoke(app, ["claude", "group", "rm", "demo"], input="n\n")

    assert result.exit_code != 0
    park.assert_not_called()
    assert (holder / claude_pool.CREDENTIAL_FILE).exists()


def test_rm_will_not_park_a_login_without_a_tty(group_env, mocker):
    """Parking is not destructive, but it does move a login out of a holder a
    script may still be pointing at — so it stays an explicit request."""
    from jailbee import claude_groups, claude_pool

    mocker.patch("jailbee.claude_pool.registered_repos", return_value=[])
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    holder = claude_groups.group_dir("demo")
    holder.mkdir(parents=True)
    (holder / claude_pool.CREDENTIAL_FILE).write_text("{}")
    park = mocker.patch("jailbee.claude_pool.park")

    result = runner.invoke(app, ["claude", "group", "rm", "demo"])

    assert result.exit_code == 2
    assert "--yes" in result.output
    park.assert_not_called()


def test_rm_reports_what_it_refused_to_delete(group_env, mocker):
    """`rmdir`, never `rm -rf`: whatever else is in there is someone's, and the
    command says what stopped it instead of removing it."""
    from jailbee import claude_groups

    mocker.patch("jailbee.claude_pool.registered_repos", return_value=[])
    holder = claude_groups.group_dir("demo")
    holder.mkdir(parents=True)
    (holder / "notes.txt").write_text("mine")

    result = runner.invoke(app, ["claude", "group", "rm", "demo"])

    assert result.exit_code == 2
    assert "notes.txt" in result.output
    assert (holder / "notes.txt").exists()


# --- Listing the groups themselves -------------------------------------------


def _built(mocker, *rows, **kwargs) -> None:
    """Point `claude group ls` at a fabricated host-wide overview."""
    mocker.patch("jailbee.claude_overview.build", return_value=claude_overview_of(*rows, **kwargs))


def test_group_ls_lists_every_group_and_what_it_holds(group_env, mocker):
    _built(
        mocker,
        claude_row("staff@corp.com", group="staff", repos=("myrepo",), containers=("myrepo-a",)),
        claude_row(None, group="fresh"),
    )

    result = runner.invoke(app, ["claude", "group", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "staff@corp.com" in result.output
    assert "fresh" in result.output
    assert "unused" in result.output
    assert "Credential groups on this host" in _flat(result.output)


def test_group_ls_leaves_out_the_parked_store_and_ungrouped_holders(group_env, mocker):
    """The subject here is the group, not the login: a parked file belongs to
    no group, and an ungrouped holder is one repo's own. Both are `claude ls`."""
    _built(
        mocker,
        claude_row("staff@corp.com", group="staff"),
        claude_row("mine@corp.com", prefix="scratch", repos=("scratch",)),
        claude_row("old@corp.com", live=False),
    )

    result = runner.invoke(app, ["claude", "group", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "staff@corp.com" in result.output
    assert "mine@corp.com" not in result.output
    assert "old@corp.com" not in result.output


def test_group_ls_says_which_group_this_repo_uses(group_env, mocker):
    _built(mocker, claude_row("staff@corp.com", group="work", mine=True))

    result = runner.invoke(app, ["claude", "group", "ls"], env={"COLUMNS": "200"})

    assert f"This repo ({group_env[0].container_prefix}) → group `work`" in _flat(result.output)


def test_group_ls_points_at_claude_ls_for_the_whole_host(group_env, mocker):
    """`claude group --help` names no way to see the logins themselves, and the
    parked store is invisible from this table by design."""
    _built(mocker, claude_row("staff@corp.com", group="staff"))

    result = runner.invoke(app, ["claude", "group", "ls"], env={"COLUMNS": "200"})

    assert "jailbee claude ls" in _flat(result.output)


def test_group_ls_says_when_the_host_has_no_groups(group_env, mocker):
    """Parked logins and ungrouped holders can still exist, so "no groups" is
    not "nothing here" — and the next step is `create`."""
    _built(mocker, claude_row("old@corp.com", live=False))

    result = runner.invoke(app, ["claude", "group", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "group create" in _flat(result.output)


def test_group_ls_json_carries_the_same_fields_as_claude_ls(group_env, mocker):
    """One field set for both tables: two renderings of one row model could
    only disagree."""
    import json

    _built(
        mocker,
        claude_row("staff@corp.com", group="staff", repos=("myrepo",), containers=("myrepo-a",)),
    )

    result = runner.invoke(app, ["claude", "group", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {
            "account": "staff@corp.com",
            "org": None,
            "state": "live",
            "group": "staff",
            "repos": ["myrepo"],
            "containers": ["myrepo-a"],
        }
    ]


def test_group_ls_warns_when_the_containers_could_not_be_listed(group_env, mocker):
    _built(mocker, claude_row("staff@corp.com", group="staff"), containers_known=False)

    result = runner.invoke(app, ["claude", "group", "ls"], env={"COLUMNS": "200"})

    assert "could not be listed" in _flat(result.output)


def test_group_ls_exits_2_when_the_store_cannot_be_read(group_env, mocker):
    mocker.patch("jailbee.claude_overview.build", side_effect=OSError("permission denied"))

    result = runner.invoke(app, ["claude", "group", "ls"])

    assert result.exit_code == 2
    assert "permission denied" in result.output
