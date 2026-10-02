"""Tests for the host-local per-repo layer's own rules (`config.local_layer`)."""

from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from jailbee.config import ConfigError
from jailbee.config.local_layer import (
    all_local_credential_groups,
    all_local_litellm_views,
    local_config_dir,
    local_config_path,
    local_credentials,
    local_litellm_overlay,
    local_litellm_scopes,
    read_local_raw,
    repo_litellm_view,
    scope_files,
    split_local_raw,
    token_perms_warning,
    validate_local_raw,
)
from jailbee.config.models_agents import GithubConfig
from jailbee.config.models_litellm import LiteLLMConfig
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


def test_token_file_should_be_private():
    path = _write_local("myapp", {"github": {"token": "ghp_x"}})
    path.chmod(0o644)
    warning = token_perms_warning(path, {"github": {"token": "ghp_x"}})
    assert warning is not None and "chmod 600" in warning
    path.chmod(0o600)
    assert token_perms_warning(path, {"github": {"token": "ghp_x"}}) is None


def test_perms_do_not_matter_without_a_token():
    path = _write_local("myapp", {"egress_allow": ["x.org"]})
    path.chmod(0o644)
    assert token_perms_warning(path, {"egress_allow": ["x.org"]}) is None


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


def test_litellm_is_peeled_off_the_overlay_not_refused():
    overlay, _ = split_local_raw({"litellm": {"default_profile": "codex"}}, "/tmp/x.yaml")
    assert "litellm" not in overlay


@pytest.mark.parametrize("key", ["enabled", "version", "accounts", "egress", "extra"])
def test_host_only_litellm_keys_are_refused_by_name(key):
    with pytest.raises(ConfigError, match=rf"`litellm\.{key}`.*global\.yaml"):
        local_litellm_overlay({"litellm": {key: None}}, "/tmp/x.yaml")


def test_no_litellm_block_is_no_overlay():
    assert local_litellm_overlay({}, "/tmp/x.yaml") is None
    assert local_litellm_overlay({"litellm": None}, "/tmp/x.yaml") is None


def test_a_pasted_key_in_an_overlay_is_never_echoed_or_chained():
    key = "sk-or-v1-" + "b" * 40
    raw = {"litellm": {"routes": {"kimi": {"model": "openrouter/x", "api_key": key}}}}
    with pytest.raises(ConfigError) as caught:
        local_litellm_overlay(raw, "/tmp/x.yaml")
    assert key not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_view_scope_is_the_prefix_only_when_routes_or_profiles_change():
    host = LiteLLMConfig()
    own = local_litellm_overlay(
        {"litellm": {"routes": {"sol-high": {"effort": "max"}}}}, "/tmp/a.yaml"
    )
    shared = local_litellm_overlay({"litellm": {"autostart": True}}, "/tmp/b.yaml")
    assert repo_litellm_view(host, "a", own, "/tmp/a.yaml").scope == "a"
    view = repo_litellm_view(host, "b", shared, "/tmp/b.yaml")
    assert (view.scope, view.origin, view.config.autostart) == (None, "/tmp/b.yaml", True)
    assert repo_litellm_view(host, "c", None, "/tmp/c.yaml").origin is None


def test_an_overlay_that_does_not_fit_names_its_file_and_global_yaml():
    overlay = local_litellm_overlay(
        {"litellm": {"profiles": {"codex": {"opus": "gone"}}}}, "/tmp/a.yaml"
    )
    with pytest.raises(ConfigError, match=r"/tmp/a\.yaml.*global\.yaml") as caught:
        repo_litellm_view(LiteLLMConfig(), "a", overlay, "/tmp/a.yaml")
    assert "unknown route 'gone'" in str(caught.value)


def test_all_views_skip_a_broken_file_and_keep_the_rest():
    _write_local("good", {"litellm": {"routes": {"sol-high": {"effort": "max"}}}})
    _write_local("plain", {"egress_allow": ["x.org"]})
    broken = _write_local("broken", {"litellm": {"profiles": {"codex": {"opus": "gone"}}}})
    views, issues = all_local_litellm_views(LiteLLMConfig())
    assert [v.prefix for v in views] == ["good"]
    assert len(issues) == 1 and str(broken) in issues[0] and "skipped" in issues[0].lower()
    scopes, _ = local_litellm_scopes(LiteLLMConfig())
    assert list(scopes) == ["good"]
    assert scopes["good"].effective_routes()["sol-high"].effort == "max"


def test_a_yaml_syntax_error_reports_path_and_line_never_the_snippet():
    _write_local("good", {"litellm": {"routes": {"sol-high": {"effort": "max"}}}})
    path = local_config_path("bad")
    path.write_text("egress_allow: []\ntoken: ghp_SECRETSECRET: x\n")
    views, issues = all_local_litellm_views(LiteLLMConfig())
    assert [v.prefix for v in views] == ["good"]
    assert len(issues) == 1
    assert issues[0] == f"{path} is not valid YAML (line 2); skipped"
    assert "SECRET" not in issues[0] and "cannot use its override" not in issues[0]


def test_a_file_that_is_not_a_mapping_is_skipped_without_the_override_wording():
    path = local_config_path("list")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- a\n- b\n")
    views, issues = all_local_litellm_views(LiteLLMConfig())
    assert views == [] and len(issues) == 1
    assert str(path) in issues[0] and "cannot use its override" not in issues[0]


def test_a_prefix_only_override_is_a_view_but_not_a_scope():
    _write_local("only", {"litellm": {"default_profile": "codex"}})
    views, _ = all_local_litellm_views(LiteLLMConfig())
    assert [(v.prefix, v.view.scope) for v in views] == [("only", None)]
    assert local_litellm_scopes(LiteLLMConfig())[0] == {}


def test_validate_local_raw_checks_the_litellm_block():
    with pytest.raises(ConfigError, match="litellm"):
        validate_local_raw({"litellm": {"autostart": "sometimes"}}, "/tmp/x.yaml")


def test_scope_files_follow_the_scope_order():
    scopes = {"b": LiteLLMConfig(), "a": LiteLLMConfig()}
    assert scope_files(scopes) == [str(local_config_path("b")), str(local_config_path("a"))]
