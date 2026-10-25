from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from jailbee.config import ConfigError, load_config
from jailbee.config.local_layer import local_config_path
from jailbee.config.models_orchestration import (
    GrantableConfig,
    OrchestrationHostConfig,
    OrchestrationProfile,
    OrchestrationView,
    RepoOrchestration,
)
from jailbee.global_config import GlobalConfig
from tests.conftest import make_cfg


def test_profile_defaults_match_grant_defaults():
    p = OrchestrationProfile()
    assert (p.max_running_children, p.operations, p.grantable.mounts) == (
        4,
        ["import", "message", "restart", "spawn", "stop"],
        [],
    )


def test_unknown_operation_rejected():
    with pytest.raises(ValidationError, match="fly"):
        OrchestrationProfile(operations=["spawn", "message", "fly"])


def test_operations_must_include_spawn_and_message():
    with pytest.raises(ValidationError, match="message"):
        OrchestrationProfile(operations=["spawn"])


def test_default_profile_must_exist():
    with pytest.raises(ValidationError, match="nope"):
        OrchestrationHostConfig(profiles={}, default_profile="nope")


def test_partial_context_skips_default_profile_check():
    cfg = OrchestrationHostConfig.model_validate(
        {"default_profile": "nope"}, context={"partial": True}
    )
    assert cfg.default_profile == "nope"


def test_bad_profile_name():
    with pytest.raises(ValidationError):
        OrchestrationHostConfig(profiles={"Bad Name": {}})


def test_grantable_egress_validated():
    with pytest.raises(ValidationError, match="egress"):
        GrantableConfig(egress=["not a valid entry!!"])


def test_repo_egress_validated_and_extra_forbidden():
    with pytest.raises(ValidationError):
        RepoOrchestration(egress=["not a valid entry!!"])
    with pytest.raises(ValidationError):
        RepoOrchestration.model_validate({"bogus": 1})


def test_view_default():
    v = OrchestrationView()
    assert (dict(v.profiles), v.default_profile, v.repo) == ({}, None, RepoOrchestration())


def test_global_config_accepts_orchestration_block():
    g = GlobalConfig.model_validate(
        {
            "orchestration": {
                "profiles": {"bugfixer": {"max_running_children": 5}},
                "default_profile": "bugfixer",
            }
        }
    )
    assert g.orchestration.profiles["bugfixer"].max_running_children == 5


def test_orchestration_is_a_host_level_key():
    from jailbee.config.common import _HOST_LEVEL_KEYS

    assert "orchestration" in _HOST_LEVEL_KEYS


def test_local_layer_peels_rather_than_refuses_orchestration():
    from jailbee.config.local_layer import split_local_raw

    overlay, _ = split_local_raw({"orchestration": {"default_profile": None}}, "/tmp/x.yaml")
    assert "orchestration" not in overlay


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def repo_and_global(tmp_path, mocker, monkeypatch):
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


def test_profiles_from_global_reach_the_view(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(
        global_path,
        {
            "orchestration": {
                "profiles": {"bugfixer": {"max_running_children": 5}},
                "default_profile": "bugfixer",
            }
        },
    )
    _write(repo_path, {})
    view = load_config(repo_path).orchestration_view()
    assert view.default_profile == "bugfixer"
    assert view.profiles["bugfixer"].max_running_children == 5


@pytest.mark.parametrize("key", ["profiles", "default_profile"])
def test_repo_layer_cannot_widen(repo_and_global, key):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"orchestration": {key: {} if key == "profiles" else "x"}})
    with pytest.raises(ConfigError, match=rf"orchestration\.{key}.*global\.yaml"):
        load_config(repo_path)


def test_repo_layer_widening_error_names_local_file(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"orchestration": {"profiles": {}}})
    with pytest.raises(ConfigError, match=r"repos/"):
        load_config(repo_path)


def test_repo_layer_narrows(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(
        repo_path,
        {
            "orchestration": {
                "allowed": False,
                "max_running_children": 1,
                "mounts": ["data"],
                "loose": False,
            }
        },
    )
    repo = load_config(repo_path).orchestration_view().repo
    assert (repo.allowed, repo.max_running_children, repo.mounts, repo.loose) == (
        False,
        1,
        ["data"],
        False,
    )


def test_invalid_repo_block_names_origin(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"orchestration": {"max_running_children": 0}})
    with pytest.raises(ConfigError, match=r"orchestration.*config\.yaml"):
        load_config(repo_path)


def test_orchestration_is_not_merged_into_config(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"orchestration": {"profiles": {"p": {}}}})
    _write(repo_path, {})
    cfg = load_config(repo_path)
    assert "orchestration" not in cfg.model_dump()


def test_local_profile_replaces_global_by_name_and_sets_default(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"orchestration": {"profiles": {"p": {"max_running_children": 5}}}})
    _write(repo_path, {"container_prefix": "myrepo"})
    _write(
        local_config_path("myrepo"),
        {
            "orchestration": {
                "profiles": {"p": {"max_running_children": 1}},
                "default_profile": "p",
            }
        },
    )
    view = load_config(repo_path).orchestration_view()
    assert view.profiles["p"].max_running_children == 1 and view.default_profile == "p"


def test_local_profile_adds_to_global(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"orchestration": {"profiles": {"a": {}}, "default_profile": "a"}})
    _write(repo_path, {"container_prefix": "myrepo"})
    _write(local_config_path("myrepo"), {"orchestration": {"profiles": {"b": {}}}})
    view = load_config(repo_path).orchestration_view()
    assert set(view.profiles) == {"a", "b"} and view.default_profile == "a"


def test_default_naming_a_missing_profile_fails_with_both_files(repo_and_global):
    _, repo_path, global_path = repo_and_global
    _write(global_path, {"orchestration": {"profiles": {}}})
    _write(repo_path, {"container_prefix": "myrepo"})
    _write(local_config_path("myrepo"), {"orchestration": {"default_profile": "gone"}})
    with pytest.raises(ConfigError, match="gone") as exc:
        load_config(repo_path)
    assert "global.yaml" in str(exc.value)
    assert "myrepo.yaml" in str(exc.value)


def test_local_file_rejects_repo_narrowing_keys(repo_and_global):
    _, repo_path, _ = repo_and_global
    _write(repo_path, {"container_prefix": "myrepo"})
    _write(local_config_path("myrepo"), {"orchestration": {"allowed": False}})
    with pytest.raises(ConfigError, match="orchestration"):
        load_config(repo_path)


def test_hand_built_config_has_empty_view(tmp_path):
    view = make_cfg(tmp_path).orchestration_view()
    assert view.profiles == {} and view.default_profile is None and view.repo.allowed is True
