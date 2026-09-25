"""Tests for the host-global remote SSH policy."""

import pytest
from pydantic import ValidationError

from jailbee.config import ConfigError
from jailbee.config.models_remote import RemoteCommandPolicy, RemoteConfig, RemoteSSHConfig
from jailbee.global_config import GlobalConfig, validate_global_raw


def test_remote_ssh_defaults_enable_all_routes_with_full_commands() -> None:
    ssh = GlobalConfig().remote.ssh
    assert ssh.listen == "127.0.0.1"
    assert ssh.port == 8022
    assert ssh.dashboard is True
    assert ssh.default_entrypoint == "help"
    assert ssh.shell is True
    assert ssh.exec is True
    assert ssh.commands.mode == "full"
    assert ssh.commands.allow == []


@pytest.mark.parametrize(
    ("mode", "allow"),
    [("allowlist", ["git pull"]), ("full", [])],
)
def test_command_entrypoint_accepts_an_enabled_policy(mode: str, allow: list[str]) -> None:
    ssh = RemoteSSHConfig(
        exec=True,
        commands=RemoteCommandPolicy(mode=mode, allow=allow),
    )

    assert ssh.exec is True
    assert ssh.commands.mode == mode
    assert ssh.commands.allow == allow


def test_disabled_command_policy_does_not_disable_routes() -> None:
    ssh = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))
    assert (ssh.dashboard, ssh.shell, ssh.exec) == (True, True, True)


def test_allowlist_mode_requires_at_least_one_leaf() -> None:
    with pytest.raises(ValidationError, match="allowlist"):
        RemoteCommandPolicy(mode="allowlist", allow=[])


def test_all_entrypoints_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        RemoteSSHConfig(dashboard=False, shell=False, exec=False)


@pytest.mark.parametrize("entrypoint", ["dashboard", "shell"])
def test_default_entrypoint_must_be_enabled(entrypoint: str) -> None:
    with pytest.raises(ValidationError, match="default_entrypoint"):
        RemoteSSHConfig(
            default_entrypoint=entrypoint,
            dashboard=entrypoint != "dashboard",
            shell=entrypoint != "shell",
            exec=True,
            commands=RemoteCommandPolicy(mode="full"),
        )


def test_default_entrypoint_rejects_one_shot_commands() -> None:
    with pytest.raises(ValidationError, match="default_entrypoint"):
        RemoteSSHConfig(default_entrypoint="ls")


@pytest.mark.parametrize("port", [0, 65536])
def test_port_must_be_in_tcp_range(port: int) -> None:
    with pytest.raises(ValidationError):
        RemoteSSHConfig(port=port)


def test_listen_must_be_an_ip_literal() -> None:
    with pytest.raises(ValidationError):
        RemoteSSHConfig(listen="localhost")


@pytest.mark.parametrize("entry", [" git pull", "git pull ", "--help", "git --help"])
def test_allowlist_rejects_non_command_paths(entry: str) -> None:
    with pytest.raises(ValidationError, match="command paths"):
        RemoteCommandPolicy(mode="allowlist", allow=[entry])


def test_allowlist_rejects_duplicates() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        RemoteCommandPolicy(mode="allowlist", allow=["git pull", "git pull"])


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (RemoteCommandPolicy, {"unknown": True}),
        (RemoteSSHConfig, {"unknown": True}),
        (RemoteConfig, {"unknown": True}),
    ],
)
def test_remote_models_reject_unknown_keys(model, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model(**kwargs)


@pytest.mark.parametrize("mode", ["allowlist", "full"])
def test_global_validation_accepts_public_allowlist_leaves(tmp_path, mode: str) -> None:
    cfg = validate_global_raw(
        {
            "remote": {
                "ssh": {
                    "exec": True,
                    "commands": {"mode": mode, "allow": ["git pull"]},
                }
            }
        },
        tmp_path / "global.yaml",
    )
    assert cfg.remote.ssh.commands.allow == ["git pull"]


@pytest.mark.parametrize("mode", ["allowlist", "full"])
def test_global_validation_rejects_unknown_allowlist_leaves(tmp_path, mode: str) -> None:
    path = tmp_path / "global.yaml"
    with pytest.raises(
        ConfigError,
        match=rf"(?s)Global config validation failed in {path}:.*git explode",
    ):
        validate_global_raw(
            {
                "remote": {
                    "ssh": {
                        "exec": True,
                        "commands": {"mode": mode, "allow": ["git explode"]},
                    }
                }
            },
            path,
        )


def test_restrict_host_defaults_on_and_accepts_false() -> None:
    from jailbee.config.models_remote import RemoteSSHConfig

    assert RemoteSSHConfig().restrict_host is True
    assert RemoteSSHConfig(restrict_host=False).restrict_host is False
