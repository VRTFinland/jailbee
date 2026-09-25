"""Tests for the host-local per-repo layer's own rules (`config.local_layer`)."""

from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from jailbee.config import ConfigError
from jailbee.config.local_layer import (
    all_local_credential_groups,
    check_token_perms,
    local_config_dir,
    local_config_path,
    local_credentials,
    read_local_raw,
    split_local_raw,
    validate_local_raw,
)
from jailbee.config.models_agents import GithubConfig
from jailbee.config.models_net import Credentials, LocalCredentials


def _write_local(prefix: str, data: object) -> Path:
    path = local_config_path(prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_local_dir_sits_next_to_global_yaml():
    from jailbee.global_config import default_global_config_path

    assert local_config_dir() == default_global_config_path().parent / "repos"
    assert local_config_path("myapp") == local_config_dir() / "myapp.yaml"


def test_missing_local_file_is_an_empty_layer():
    assert read_local_raw("nothing-here") == {}


@pytest.mark.parametrize("content", ["", "null\n"])
def test_empty_or_null_local_file_is_an_empty_layer(content):
    path = local_config_path("myapp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert read_local_raw("myapp") == {}


def test_list_local_file_names_the_file():
    path = _write_local("myapp", ["a", "b"])
    with pytest.raises(ConfigError, match=str(path)):
        read_local_raw("myapp")


def test_broken_yaml_local_file_names_the_file():
    path = local_config_path("myapp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("egress_allow: [unclosed\n")
    with pytest.raises(ConfigError, match=str(path)):
        read_local_raw("myapp")


@pytest.mark.parametrize(
    "key",
    [
        "container_prefix",
        "credential_group",
        "claude_credentials_dir",
        "scratch",
        "config_edit",
        "update_check",
        "remote",
        "install_host_skills",
    ],
)
def test_refused_keys_name_the_key_and_the_file(key):
    with pytest.raises(ConfigError, match=rf"`{key}`.*/tmp/x.yaml|/tmp/x.yaml.*`{key}`"):
        split_local_raw({key: "x"}, "/tmp/x.yaml")


@pytest.mark.parametrize("key", ["ls", "dashboard", "docker_registry_mirror", "egress_allow"])
def test_repo_legal_keys_pass_through(key):
    overlay, creds = split_local_raw({key: {}}, "/tmp/x.yaml")
    assert key in overlay
    assert creds is None


def test_legacy_claude_credentials_is_refused_with_the_new_spelling():
    with pytest.raises(ConfigError, match=r"`credentials:`"):
        split_local_raw({"claude_credentials": {"group": "a"}}, "/tmp/x.yaml")


def test_api_tokens_is_refused_in_favour_of_token():
    with pytest.raises(ConfigError, match=r"github\.token"):
        split_local_raw({"github": {"api_tokens": {"a": "b"}}}, "/tmp/x.yaml")


def test_credentials_are_split_off_the_overlay():
    overlay, creds = split_local_raw(
        {"credentials": {"group": "team-a"}, "egress_allow": ["x.org"]}, "/tmp/x.yaml"
    )
    assert overlay == {"egress_allow": ["x.org"]}
    assert creds == LocalCredentials(group="team-a")


def test_credentials_block_rejects_unknown_keys():
    with pytest.raises(ConfigError, match=r"/tmp/x.yaml"):
        split_local_raw({"credentials": {"repos": {}}}, "/tmp/x.yaml")


def test_explicit_null_group_opts_out_over_the_global_default():
    creds = Credentials(group="shared", repos={"myapp": "other"})
    assert creds.group_for("myapp", LocalCredentials(group=None)) is None


def test_local_group_wins_over_the_legacy_map():
    creds = Credentials(group="shared", repos={"myapp": "other"})
    assert creds.group_for("myapp", LocalCredentials(group="mine")) == "mine"


def test_empty_local_credentials_block_falls_through():
    creds = Credentials(group="shared", repos={"myapp": "other"})
    assert creds.group_for("myapp", LocalCredentials()) == "other"
    assert creds.group_for("elsewhere", LocalCredentials()) == "shared"


def test_token_wins_over_the_map():
    gh = GithubConfig(token=SecretStr("local"), api_tokens={"myapp": SecretStr("legacy")})
    secret = gh.token_for("myapp")
    assert secret is not None and secret.get_secret_value() == "local"


def test_map_is_used_without_a_token():
    gh = GithubConfig(api_tokens={"myapp": SecretStr("legacy")})
    secret = gh.token_for("myapp")
    assert secret is not None and secret.get_secret_value() == "legacy"
    assert gh.token_for("other") is None


def test_token_file_must_be_private():
    path = _write_local("myapp", {"github": {"token": "ghp_x"}})
    path.chmod(0o644)
    with pytest.raises(ConfigError, match=r"chmod 600"):
        check_token_perms(path, {"github": {"token": "ghp_x"}})
    path.chmod(0o600)
    check_token_perms(path, {"github": {"token": "ghp_x"}})


def test_perms_do_not_matter_without_a_token():
    path = _write_local("myapp", {"egress_allow": ["x.org"]})
    path.chmod(0o644)
    check_token_perms(path, {"egress_allow": ["x.org"]})


def test_validate_local_raw_runs_the_model_over_the_overlay():
    with pytest.raises(ConfigError, match=r"/tmp/x.yaml"):
        validate_local_raw({"egress_allow": "not-a-list"}, "/tmp/x.yaml")
    validate_local_raw({"agents": {"codex": {"enabled": False}}}, "/tmp/x.yaml")


def test_local_credentials_reads_the_file():
    _write_local("myapp", {"credentials": {"group": "team-a"}})
    assert local_credentials("myapp") == LocalCredentials(group="team-a")
    assert local_credentials("absent") is None


def test_all_local_credential_groups_skips_null_and_unreadable():
    _write_local("a", {"credentials": {"group": "g1"}})
    _write_local("b", {"credentials": {"group": None}})
    broken = local_config_path("c")
    broken.write_text("{nope\n")
    assert all_local_credential_groups() == {"g1"}
