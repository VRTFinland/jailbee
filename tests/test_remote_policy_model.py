"""`RemotePolicy`: the transport-agnostic remote permissions, shared by SSH and web."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from jailbee.config.models_remote import (
    RemoteCommandPolicy,
    RemotePolicy,
    RemoteSSHConfig,
    RemoteWebConfig,
)
from jailbee.global_config import GlobalConfig, load_global_config

POLICY_KEYS = {
    "commands",
    "restrict_host",
    "excluded_repos",
    "mount_roots",
    "mounts",
    "orchestration",
    "network",
    "files",
    "rdp",
}


def test_web_defaults_to_the_most_restrictive_policy():
    web = GlobalConfig().remote.web
    assert web.commands.mode == "disabled"
    assert web.commands.allow == []
    assert web.restrict_host is True
    assert web.mounts is False
    assert web.mount_roots == []
    assert web.orchestration == "off"
    assert web.network is False
    assert web.files_enabled() is False
    assert web.rdp is False
    assert web.excluded_repos == []


def test_web_restrict_host_cannot_be_turned_off():
    with pytest.raises(ValidationError, match="restrict_host"):
        RemoteWebConfig(restrict_host=False)


def test_web_has_policy_fields_only():
    assert set(RemoteWebConfig.model_fields) == POLICY_KEYS
    assert set(RemotePolicy.model_fields) == POLICY_KEYS
    with pytest.raises(ValidationError):
        RemoteWebConfig(port=1234)


def test_web_files_default_follows_commands_mode_but_stays_off_when_disabled():
    full = RemoteWebConfig(commands=RemoteCommandPolicy(mode="full"))
    assert full.files_enabled() is True
    assert (
        RemoteWebConfig(files=False, commands=RemoteCommandPolicy(mode="full")).files_enabled()
        is False
    )


def test_ssh_keeps_its_own_defaults():
    ssh = RemoteSSHConfig()
    assert ssh.commands.mode == "full"
    assert ssh.orchestration == "ssh"
    assert ssh.restrict_host is True


def test_ssh_yaml_loads_to_the_same_values_and_exposes_its_policy(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "remote": {
                    "ssh": {
                        "listen": "::1",
                        "port": 2222,
                        "commands": {"mode": "allowlist", "allow": ["ls"]},
                        "excluded_repos": ["secret"],
                        "mounts": True,
                        "network": True,
                        "files": False,
                        "rdp": True,
                        "orchestration": "orchestrator",
                        "waypipe": False,
                    }
                }
            }
        )
    )
    gcfg, _ = load_global_config(path)
    ssh = gcfg.remote.ssh
    assert (ssh.listen, ssh.port, ssh.waypipe) == ("::1", 2222, False)
    assert ssh.commands.allow == ["ls"]

    policy = ssh.policy
    assert type(policy) is RemotePolicy
    assert policy.commands.mode == "allowlist"
    assert policy.excluded_repos == ["secret"]
    assert policy.mounts and policy.network and policy.rdp
    assert policy.files is False
    assert policy.orchestration == "orchestrator"
    assert policy.restrict_host is True


def test_policy_follows_model_copy_updates():
    ssh = RemoteSSHConfig().model_copy(update={"network": True})
    assert ssh.policy.network is True


def test_web_policy_is_a_plain_remote_policy_and_loads_from_yaml(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text(
        yaml.safe_dump({"remote": {"web": {"commands": {"mode": "full"}, "excluded_repos": ["x"]}}})
    )
    gcfg, _ = load_global_config(path)
    policy = gcfg.remote.web.policy
    assert type(policy) is RemotePolicy
    assert policy.commands.mode == "full"
    assert policy.excluded_repos == ["x"]
    assert policy.restrict_host is True


def test_web_validation_errors_name_remote_web():
    with pytest.raises(ValidationError, match=r"remote\.web\.excluded_repos"):
        RemoteWebConfig(excluded_repos=["a", "a"])
