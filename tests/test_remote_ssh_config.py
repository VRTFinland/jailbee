"""Tests for the host-global remote SSH policy."""

import pytest
from pydantic import ValidationError

from jailbee.config.models_remote import RemoteCommandPolicy, RemoteConfig, RemoteSSHConfig
from jailbee.global_config import GlobalConfig


def test_remote_ssh_defaults_are_dashboard_only() -> None:
    ssh = GlobalConfig().remote.ssh
    assert ssh.listen == "127.0.0.1"
    assert ssh.port == 8022
    assert ssh.dashboard is True
    assert ssh.shell is False
    assert ssh.exec is False
    assert ssh.commands.mode == "disabled"
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


@pytest.mark.parametrize("entrypoint", ["shell", "exec"])
def test_command_entrypoint_requires_a_command_policy(entrypoint: str) -> None:
    with pytest.raises(ValidationError, match=r"commands\.mode"):
        RemoteSSHConfig(**{entrypoint: True})


def test_allowlist_mode_requires_at_least_one_leaf() -> None:
    with pytest.raises(ValidationError, match="allowlist"):
        RemoteCommandPolicy(mode="allowlist", allow=[])


def test_all_entrypoints_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        RemoteSSHConfig(dashboard=False, shell=False, exec=False)


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
