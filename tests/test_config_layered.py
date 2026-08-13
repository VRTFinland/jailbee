"""Tests for the global+repo layered loader inside load_config."""

from pathlib import Path

import pytest
import yaml

from jailbee.config import ConfigError, load_config


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
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
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
