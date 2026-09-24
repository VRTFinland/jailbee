"""Tests for the global+repo layered loader inside load_config."""

from pathlib import Path

import pytest
import yaml

from jailbee.config import ConfigError, load_config
from jailbee.config.local_layer import local_config_path


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def repo_and_global(tmp_path, mocker, monkeypatch):
    """Set up a repo with a (mocked) home for `~/.config/jailbee/global.yaml`."""
    repo_root = tmp_path / "myrepo"
    (repo_root / ".jailbee").mkdir(parents=True)
    (repo_root / ".git").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    global_path = home / ".config" / "jailbee" / "global.yaml"
    repo_path = repo_root / ".jailbee" / "config.yaml"
    return repo_root, repo_path, global_path


def test_missing_global_behaves_like_today(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"jetbrains": {"ide": "pycharm"}})

    cfg = load_config(repo_path)

    assert cfg.jetbrains.ide == "pycharm"


def test_global_scalar_used_when_repo_silent(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"jetbrains": {"ide": "idea"}})
    _write(repo_path, {})

    cfg = load_config(repo_path)

    assert cfg.jetbrains.ide == "idea"


def test_repo_scalar_overrides_global(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"jetbrains": {"ide": "idea"}})
    _write(repo_path, {"jetbrains": {"ide": "pycharm"}})

    cfg = load_config(repo_path)

    assert cfg.jetbrains.ide == "pycharm"


def test_global_list_appended_by_repo(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"egress_allow": ["api.anthropic.com:443"]})
    _write(repo_path, {"egress_allow": ["pypi.org:443"]})

    cfg = load_config(repo_path)

    assert cfg.egress_allow == ["api.anthropic.com:443", "pypi.org:443"]


def test_empty_repo_list_resets_global(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"egress_allow": ["api.anthropic.com:443"]})
    _write(repo_path, {"egress_allow": []})

    cfg = load_config(repo_path)

    assert cfg.egress_allow == []


def test_container_path_appended_from_global(repo_and_global):
    """`container.path` follows the list-append convention, which has a
    consequence worth pinning: the global layer's entries land *earlier* in
    the rendered PATH, so they win a name collision against the repo's own.
    """
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"container": {"path": ["~/bin"]}})
    _write(repo_path, {"container": {"path": ["scripts"]}})

    cfg = load_config(repo_path)

    assert cfg.container.path == ["~/bin", "scripts"]


def test_golden_enable_snippets_appended_from_global(repo_and_global):
    """`golden` is a plain nested dict, so it goes through the same
    generic deep_merge as every other block: global.yaml's
    `golden.enable_snippets` must survive into the merged Config, with
    the repo's own entries appended (list-append convention, same as
    `egress_allow` above).
    """
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"golden": {"enable_snippets": ["nodejs"]}})
    _write(repo_path, {"golden": {"enable_snippets": ["docker"]}})

    cfg = load_config(repo_path)

    assert cfg.golden.enable_snippets == ["nodejs", "docker"]


def test_golden_scalar_from_global_used_when_repo_silent(repo_and_global):
    """A golden scalar field (not just enable_snippets) set only in the
    global layer must also reach the merged Config — golden isn't
    special-cased out of the global->repo merge.
    """
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"golden": {"java": "amazon-corretto-21"}})
    _write(repo_path, {})

    cfg = load_config(repo_path)

    assert cfg.golden.java == "amazon-corretto-21"


def test_golden_stacks_merge_global_and_repo(repo_and_global):
    """The golden.stacks nested dict is deep-merged key-by-key, not
    wholesale-replaced: repo keys override global per-key (java), repo-only
    keys are added (node), and — critically — global-only keys the repo
    never mentions (python) still survive into the merged Config. A
    wholesale-replace regression (repo `stacks` dict clobbering global's
    entirely) would satisfy the java/node assertions alone, so the
    global-only `python` key is what actually proves deep-merge here.
    """
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"golden": {"stacks": {"java": "corretto-17", "python": True}}})
    _write(repo_path, {"golden": {"stacks": {"node": 22, "java": "openjdk-21"}}})

    cfg = load_config(repo_path)

    # repo overrides java per key; node from repo; global-only key survives
    assert cfg.golden.stacks.java == "openjdk-21"
    assert cfg.golden.stacks.node == 22
    assert cfg.golden.stacks.python is True


def test_host_mounts_appended(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(
        global_path,
        {"host_mounts": [{"host": "~/.gnupg", "container": "/home/dev/.gnupg", "readonly": True}]},
    )
    _write(
        repo_path,
        {"host_mounts": [{"host": "~/.aws", "container": "/home/dev/.aws", "readonly": True}]},
    )

    cfg = load_config(repo_path)
    hosts = [str(m.host) for m in cfg.host_mounts]

    assert len(cfg.host_mounts) == 2
    assert any(h.endswith("/.gnupg") for h in hosts)
    assert any(h.endswith("/.aws") for h in hosts)


def test_host_level_docker_registry_mirror_not_merged(repo_and_global, tmp_path):
    """`docker_registry_mirror` at global level is host-level (GlobalConfig)
    and must not leak into Config-level validation.
    """
    _, repo_path, global_path = repo_and_global
    # Use the host-level schema shape ({port, enabled, image}) which is
    # NOT compatible with Config-level DockerRegistryMirrorRepoConfig
    # ({extra_registries}). If the host-level dict were merged into the
    # Config layer, validation would fail.
    _write(
        global_path,
        {"docker_registry_mirror": {"port": 3128, "enabled": True}},
    )
    _write(repo_path, {})

    cfg = load_config(repo_path)  # must not raise

    # Repo-level DockerRegistryMirrorRepoConfig defaults to empty.
    assert cfg.docker_registry_mirror.extra_registries == []


def test_invalid_global_yaml_raises_with_path(repo_and_global):
    _, repo_path, global_path = repo_and_global
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(":not valid: yaml: at all\n  - [")
    _write(repo_path, {})

    with pytest.raises(ConfigError) as exc:
        load_config(repo_path)

    assert "global.yaml" in str(exc.value)


def test_validation_error_in_merged_result_reports_repo_path(repo_and_global):
    """When a merged value fails Pydantic validation, the error message
    references the repo config path (the final loadable artifact)."""
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"defaults": {"network": "loose"}})
    _write(repo_path, {"defaults": {"network": "bogus-mode"}})

    with pytest.raises(ConfigError) as exc:
        load_config(repo_path)

    assert str(repo_path) in str(exc.value)


def test_optional_mounts_deep_merge(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(
        global_path,
        {
            "optional_mounts": {
                "m2": {"host": "~/.m2", "container": "/home/dev/.m2"},
            }
        },
    )
    _write(
        repo_path,
        {
            "optional_mounts": {
                "aws": {"host": "~/.aws", "container": "/home/dev/.aws"},
            }
        },
    )

    cfg = load_config(repo_path)

    assert set(cfg.optional_mounts) == {"m2", "aws"}


# ---------- github block placement / perms / enabled-empty


def test_load_config_rejects_github_block_in_repo_yaml(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(
        repo_path,
        {
            "github": {
                "enabled": True,
                "api_tokens": {"leaked": "github_pat_DO_NOT_COMMIT"},
            }
        },
    )

    with pytest.raises(ConfigError, match=r"`github` block is not allowed in repo"):
        load_config(repo_path)


def test_load_config_rejects_insecure_global_yaml_with_tokens(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(
        global_path,
        {
            "github": {
                "enabled": True,
                "api_tokens": {"sampleapp": "github_pat_xxx"},
            }
        },
    )
    global_path.chmod(0o644)
    _write(repo_path, {"container_prefix": "sampleapp"})

    with pytest.raises(ConfigError, match=r"insecure perms"):
        load_config(repo_path)


def test_load_config_accepts_secure_global_yaml_with_tokens(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(
        global_path,
        {
            "github": {
                "enabled": True,
                "api_tokens": {"sampleapp": "github_pat_xxx"},
            }
        },
    )
    global_path.chmod(0o600)
    _write(repo_path, {"container_prefix": "sampleapp"})

    cfg = load_config(repo_path)
    assert cfg.github.enabled is True
    assert cfg.github.api_tokens["sampleapp"].get_secret_value() == "github_pat_xxx"


def test_load_config_rejects_github_enabled_with_empty_tokens(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"github": {"enabled": True}})
    global_path.chmod(0o600)
    _write(repo_path, {"container_prefix": "sampleapp"})

    with pytest.raises(ConfigError, match=r"api_tokens is empty"):
        load_config(repo_path)


def test_full_global_config_with_github_loads_cleanly(repo_and_global):
    """Integration check: full_global_config.yaml fixture (which carries
    every integration block including github) merges cleanly with a
    container_prefix=myrepo repo config and produces a Config whose
    github branch is populated."""
    _, repo_path, global_path = repo_and_global

    fixture = Path(__file__).parent / "fixtures" / "full_global_config.yaml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(fixture.read_text())
    global_path.chmod(0o600)
    _write(repo_path, {"container_prefix": "myrepo"})

    cfg = load_config(repo_path)
    assert cfg.github.enabled is True
    assert "myrepo" in cfg.github.api_tokens
    assert cfg.github.api_tokens["myrepo"].get_secret_value() == "github_pat_TEST_FIXTURE_VALUE"
    # Sanity: the other blocks come through too.
    assert cfg.gpg.enabled is True
    assert cfg.claude.enabled is True
    assert cfg.jetbrains.enabled is True
    assert cfg.chrome.enabled is True
    # Egress auto-add fires for this prefix.
    assert "api.github.com:443" in cfg.effective_egress_allow()


# ---------- credentials block placement / resolution


def test_new_credentials_key_sets_computed_group(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"credentials": {"group": "work"}})
    _write(repo_path, {})

    assert load_config(repo_path).credential_group == "work"


def test_legacy_claude_credentials_key_sets_the_same_group(repo_and_global):
    from jailbee import notices

    _, repo_path, global_path = repo_and_global
    _write(global_path, {"claude_credentials": {"group": "work"}})
    _write(repo_path, {})

    cfg = load_config(repo_path)

    assert cfg.credential_group == "work"
    active = notices.active()
    assert len(active) == 1
    assert active[0].key == "legacy-credentials-block"
    assert any("credentials" in line for line in active[0].lines)


def test_new_credentials_key_emits_no_deprecation_notice(repo_and_global):
    from jailbee import notices

    _, repo_path, global_path = repo_and_global
    _write(global_path, {"credentials": {"group": "work"}})
    _write(repo_path, {})

    load_config(repo_path)

    assert notices.active() == ()


def test_both_credentials_keys_are_refused(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(
        global_path,
        {
            "credentials": {"group": "work"},
            "claude_credentials": {"group": "personal"},
        },
    )
    _write(repo_path, {})

    with pytest.raises(ConfigError, match="Both `credentials` and deprecated"):
        load_config(repo_path)


def test_credential_group_is_none_without_the_block(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {})

    assert load_config(repo_path).credential_group is None


def test_credential_group_honours_a_per_repo_opt_out(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(
        global_path,
        {"credentials": {"group": "work", "repos": {"myrepo": None}}},
    )
    _write(repo_path, {})

    assert load_config(repo_path).credential_group is None


def test_credentials_in_a_repo_config_is_refused(repo_and_global):
    """Silently ignoring it is the wrong behaviour for a key whose whole point
    is that it is host-only — the same reasoning as the `github` block's ban."""
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"credentials": {"group": "work"}})

    with pytest.raises(ConfigError, match=r"global\.yaml"):
        load_config(repo_path)


def test_credential_group_in_a_repo_config_is_refused(repo_and_global):
    """The computed attribute is a declared Config field, so YAML could set it
    and be overwritten silently. Ban the name too."""
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"credential_group": "work"})

    with pytest.raises(ConfigError, match=r"global\.yaml"):
        load_config(repo_path)


def test_new_background_from_global(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"new": {"background": True}})
    _write(repo_path, {})
    cfg = load_config(repo_path)
    assert cfg.new.background is True


def test_repo_new_background_overrides_global(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"new": {"background": True}})
    _write(repo_path, {"new": {"background": False}})
    cfg = load_config(repo_path)
    assert cfg.new.background is False


def test_confirm_global_used_when_repo_silent(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"confirm": {"auto_target": False}})
    _write(repo_path, {})

    cfg = load_config(repo_path)

    assert cfg.confirm.auto_target is False


def test_confirm_repo_overrides_global(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"confirm": {"auto_target": False}})
    _write(repo_path, {"confirm": {"auto_target": True}})

    cfg = load_config(repo_path)

    assert cfg.confirm.auto_target is True


def _write_local(prefix: str, data: dict, mode: int = 0o600) -> Path:
    path = local_config_path(prefix)
    _write(path, data)
    path.chmod(mode)
    return path


def test_local_scalar_overrides_repo(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"container_prefix": "myrepo", "jetbrains": {"ide": "pycharm"}})
    _write_local("myrepo", {"jetbrains": {"ide": "idea"}})

    assert load_config(repo_path).jetbrains.ide == "idea"


def test_local_list_appends_and_empty_list_clears(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"egress_allow": ["g.org"]})
    _write(repo_path, {"container_prefix": "myrepo", "egress_allow": ["r.org"]})
    _write_local("myrepo", {"egress_allow": ["l.org"]})
    assert load_config(repo_path).egress_allow == ["g.org", "r.org", "l.org"]

    _write_local("myrepo", {"egress_allow": []})
    assert load_config(repo_path).egress_allow == []


def test_local_file_is_found_by_the_derived_prefix(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {})
    _write_local("myrepo", {"jetbrains": {"ide": "idea"}})
    assert load_config(repo_path).jetbrains.ide == "idea"


def test_local_can_disable_an_agent_the_global_layer_enables(repo_and_global):
    from jailbee.agents import enabled_agent_specs

    _, repo_path, global_path = repo_and_global
    _write(global_path, {"agents": {"codex": {"enabled": True}}})
    _write(repo_path, {"container_prefix": "myrepo"})
    _write_local("myrepo", {"agents": {"codex": {"enabled": False}}})
    names = [s.name for s in enabled_agent_specs(load_config(repo_path))]
    assert "codex" not in names


def test_illegal_directory_prefix_still_raises_the_prefix_error(tmp_path, mocker, monkeypatch):
    repo_root = tmp_path / "My_Repo"
    (repo_root / ".jailbee").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    repo_path = repo_root / ".jailbee" / "config.yaml"
    _write(repo_path, {})

    with pytest.raises(ConfigError, match=r"Invalid container_prefix 'My_Repo'"):
        load_config(repo_path)


def test_local_container_prefix_is_refused_naming_the_local_file(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"container_prefix": "myrepo"})
    path = _write_local("myrepo", {"container_prefix": "other"})

    with pytest.raises(ConfigError, match=str(path)):
        load_config(repo_path)


def test_local_token_is_used_and_needs_0600(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"github": {"enabled": True}})
    _write(repo_path, {"container_prefix": "myrepo"})
    _write_local("myrepo", {"github": {"token": "ghp_local"}})
    cfg = load_config(repo_path)
    secret = cfg.github.token_for("myrepo")
    assert secret is not None and secret.get_secret_value() == "ghp_local"

    local_config_path("myrepo").chmod(0o644)
    with pytest.raises(ConfigError, match=r"chmod 600"):
        load_config(repo_path)


def test_enabled_without_any_token_still_fails(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"github": {"enabled": True}})
    global_path.chmod(0o600)
    _write(repo_path, {"container_prefix": "myrepo"})
    with pytest.raises(ConfigError, match=r"api_tokens is empty"):
        load_config(repo_path)


def test_token_in_global_yaml_is_refused(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"github": {"token": "ghp_x"}})
    global_path.chmod(0o600)
    _write(repo_path, {"container_prefix": "myrepo"})
    with pytest.raises(ConfigError, match=r"github\.token.*per-repo"):
        load_config(repo_path)


def test_local_group_wins_over_the_legacy_map_and_warns_on_conflict(repo_and_global, mocker):
    hint = mocker.patch("jailbee.tui.hint")
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"credentials": {"repos": {"myrepo": "old"}}})
    _write(repo_path, {"container_prefix": "myrepo"})
    _write_local("myrepo", {"credentials": {"group": "new"}})
    assert load_config(repo_path).credential_group == "new"
    text = " ".join(line for call in hint.call_args_list for line in call.args[0])
    assert "credentials.repos.myrepo" in text
    assert "jailbee config migrate" in text
    assert "different value" in text


def test_legacy_map_alone_still_works_and_points_at_migrate(repo_and_global, mocker):
    hint = mocker.patch("jailbee.tui.hint")
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"credentials": {"repos": {"myrepo": "old"}}})
    _write(repo_path, {"container_prefix": "myrepo"})
    assert load_config(repo_path).credential_group == "old"
    text = " ".join(line for call in hint.call_args_list for line in call.args[0])
    assert "jailbee config migrate" in text
    assert "different value" not in text


def test_branch_autostart_gets_local_tweaks(repo_and_global):
    from jailbee.config import load_config_from_text

    _, repo_path, _ = repo_and_global
    _write_local("myrepo", {"autostart": {"env": {"FROM_LOCAL": "1"}}})
    cfg = load_config_from_text("container_prefix: myrepo\n", repo_path)
    assert cfg.autostart.env["FROM_LOCAL"] == "1"


def test_staged_local_raw_is_used_instead_of_the_file(repo_and_global):
    from jailbee.config import load_config_from_layers

    _, repo_path, _ = repo_and_global
    _write_local("myrepo", {"jetbrains": {"ide": "idea"}})
    cfg = load_config_from_layers(
        {},
        {"container_prefix": "myrepo"},
        repo_path,
        origin=str(repo_path),
        local_raw={"jetbrains": {"ide": "goland"}},
    )
    assert cfg.jetbrains.ide == "goland"
