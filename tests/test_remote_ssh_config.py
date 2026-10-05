"""Tests for the host-global remote SSH policy."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from jailbee.config import ConfigError
from jailbee.config.common import normalize_remote_ssh_keys
from jailbee.config.models_remote import RemoteCommandPolicy, RemoteConfig, RemoteSSHConfig
from jailbee.global_config import GlobalConfig, validate_global_raw


def test_remote_ssh_defaults_enable_all_routes_with_full_commands() -> None:
    ssh = GlobalConfig().remote.ssh
    assert ssh.listen == "127.0.0.1"
    assert ssh.port == 8022
    assert ssh.dashboard is True
    assert ssh.default_entrypoint == "help"
    assert ssh.console is True
    assert ssh.exec is True
    assert ssh.commands.mode == "full"
    assert ssh.commands.allow == []
    assert ssh.excluded_repos == []


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
    assert (ssh.dashboard, ssh.console, ssh.exec) == (True, True, True)


def test_allowlist_mode_requires_at_least_one_leaf() -> None:
    with pytest.raises(ValidationError, match="allowlist"):
        RemoteCommandPolicy(mode="allowlist", allow=[])


def test_all_entrypoints_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        RemoteSSHConfig(dashboard=False, console=False, exec=False)


@pytest.mark.parametrize("entrypoint", ["dashboard", "console"])
def test_default_entrypoint_must_be_enabled(entrypoint: str) -> None:
    with pytest.raises(ValidationError, match="default_entrypoint"):
        RemoteSSHConfig(
            default_entrypoint=entrypoint,
            dashboard=entrypoint != "dashboard",
            console=entrypoint != "console",
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


def test_excluded_repos_accepts_valid_unregistered_prefix() -> None:
    assert RemoteSSHConfig(excluded_repos=["future-repo-2"]).excluded_repos == ["future-repo-2"]


@pytest.mark.parametrize("prefixes", [["secret", "secret"], ["Project"]])
def test_excluded_repos_reject_duplicates_and_invalid_prefixes(prefixes: list[str]) -> None:
    with pytest.raises(ValidationError):
        RemoteSSHConfig(excluded_repos=prefixes)


def test_excluded_repos_require_host_restrictions() -> None:
    with pytest.raises(ValidationError, match="restrict_host"):
        RemoteSSHConfig(excluded_repos=["secret"], restrict_host=False)


def test_gui_is_off_by_default():
    from jailbee.config.models_remote import RemoteSSHConfig

    assert RemoteSSHConfig().gui is False


def test_gui_can_be_turned_on():
    from jailbee.config.models_remote import RemoteSSHConfig

    assert RemoteSSHConfig(gui=True).gui is True


def test_files_defaults_off_and_accepts_true() -> None:
    from jailbee.config.models_remote import RemoteSSHConfig

    assert RemoteSSHConfig().files is False
    assert RemoteSSHConfig(files=True).files is True


def test_network_defaults_off_and_accepts_true() -> None:
    from jailbee.config.models_remote import RemoteSSHConfig

    assert RemoteSSHConfig().network is False
    assert RemoteSSHConfig(network=True).network is True


def test_fold_renames_shell_and_entrypoint() -> None:
    raw: dict[str, object] = {
        "remote": {"ssh": {"shell": False, "default_entrypoint": "shell", "port": 1}}
    }
    out, folded = normalize_remote_ssh_keys(raw, "g.yaml")
    assert folded
    assert out["remote"]["ssh"] == {  # type: ignore[index]
        "console": False,
        "default_entrypoint": "console",
        "port": 1,
    }
    assert raw["remote"]["ssh"]["shell"] is False  # type: ignore[index]  # input untouched


def test_fold_is_noop_without_legacy_keys() -> None:
    raw: dict[str, object] = {"remote": {"ssh": {"console": True}}}
    assert normalize_remote_ssh_keys(raw, "g.yaml") == (raw, False)
    assert normalize_remote_ssh_keys({}, "g.yaml") == ({}, False)
    assert normalize_remote_ssh_keys({"remote": None}, "g.yaml") == ({"remote": None}, False)


def test_both_spellings_is_an_error_naming_both_keys() -> None:
    with pytest.raises(ConfigError) as exc:
        normalize_remote_ssh_keys({"remote": {"ssh": {"shell": True, "console": True}}}, "g.yaml")
    assert "remote.ssh.console" in str(exc.value)
    assert "remote.ssh.shell" in str(exc.value)


def test_both_spellings_in_global_yaml_is_an_error() -> None:
    with pytest.raises(ConfigError, match=r"remote\.ssh\.shell"):
        validate_global_raw({"remote": {"ssh": {"shell": True, "console": True}}}, Path("/g.yaml"))


def test_legacy_global_yaml_loads_with_notice(mocker) -> None:
    emit = mocker.patch("jailbee.notices.emit")
    cfg = validate_global_raw({"remote": {"ssh": {"shell": False}}}, Path("/g.yaml"))
    assert cfg.remote.ssh.console is False
    assert emit.call_args.args[0].key == "legacy-remote-ssh-shell"


def test_legacy_fold_is_silent_without_emit_hint(mocker) -> None:
    emit = mocker.patch("jailbee.notices.emit")
    validate_global_raw({"remote": {"ssh": {"shell": False}}}, Path("/g.yaml"), emit_hint=False)
    emit.assert_not_called()


def test_legacy_default_entrypoint_shell_folds_to_console() -> None:
    cfg = validate_global_raw(
        {"remote": {"ssh": {"default_entrypoint": "shell"}}}, Path("/g.yaml"), emit_hint=False
    )
    assert cfg.remote.ssh.default_entrypoint == "console"


def test_disabled_console_cannot_be_legacy_default_entrypoint() -> None:
    with pytest.raises(ConfigError, match="enabled entry point"):
        validate_global_raw(
            {"remote": {"ssh": {"console": False, "default_entrypoint": "shell"}}},
            Path("/g.yaml"),
        )
