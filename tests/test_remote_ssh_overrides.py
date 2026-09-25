"""`jb remote ssh serve`'s command-line overrides of `remote.ssh`."""

from __future__ import annotations

import pytest

from jailbee.config import ConfigError
from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
from jailbee.remote_ssh.overrides import ServeOverrides, apply_ssh_overrides, describe_overrides


def test_empty_overrides_is_a_no_op_and_returns_the_same_object() -> None:
    config = RemoteSSHConfig()

    result = apply_ssh_overrides(config, ServeOverrides())

    assert result is config


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("listen", "0.0.0.0"),
        ("port", 2222),
        ("dashboard", False),
        ("shell", True),
        ("exec", True),
        ("restrict_host", False),
    ],
)
def test_each_given_flag_overrides_its_field(field: str, value: object) -> None:
    base = RemoteSSHConfig(shell=True, exec=True, commands=RemoteCommandPolicy(mode="full"))
    overrides = ServeOverrides(**{field: value})

    result = apply_ssh_overrides(base, overrides)

    assert getattr(result, field) == value


def test_unflagged_fields_keep_following_the_base_config() -> None:
    base = RemoteSSHConfig(
        listen="192.0.2.1",
        port=2200,
        shell=True,
        exec=True,
        commands=RemoteCommandPolicy(mode="full"),
    )

    result = apply_ssh_overrides(base, ServeOverrides(dashboard=False))

    assert result.listen == "192.0.2.1"
    assert result.port == 2200
    assert result.shell is True
    assert result.exec is True
    assert result.commands.mode == "full"
    assert result.dashboard is False


def test_commands_mode_override_alone_keeps_the_configured_allow_list() -> None:
    base = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"]))

    result = apply_ssh_overrides(base, ServeOverrides(commands_mode="full"))

    assert result.commands.mode == "full"
    assert result.commands.allow == ["ls"]


def test_allow_override_replaces_rather_than_appends() -> None:
    base = RemoteSSHConfig(
        exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["git pull"])
    )

    result = apply_ssh_overrides(base, ServeOverrides(allow=["ls", "new"]))

    assert result.commands.allow == ["ls", "new"]
    assert "git pull" not in result.commands.allow


def test_invalid_empty_allowlist_raises_config_error_not_a_traceback_worthy_exception() -> None:
    base = RemoteSSHConfig()

    with pytest.raises(ConfigError, match="commands"):
        apply_ssh_overrides(base, ServeOverrides(commands_mode="allowlist"))


def test_disabled_commands_override_preserves_enabled_routes() -> None:
    result = apply_ssh_overrides(RemoteSSHConfig(), ServeOverrides(commands_mode="disabled"))

    assert (result.dashboard, result.shell, result.exec) == (True, True, True)
    assert result.commands.mode == "disabled"


def test_allowlist_override_with_no_allow_entries_is_rejected() -> None:
    base = RemoteSSHConfig()

    with pytest.raises(ConfigError, match="allowlist"):
        apply_ssh_overrides(base, ServeOverrides(commands_mode="allowlist"))


def test_unknown_allow_leaf_is_rejected_like_a_config_file_would_be() -> None:
    base = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full"))

    with pytest.raises(
        ConfigError, match="unknown remote Jailbee command path\\(s\\): git explode"
    ):
        apply_ssh_overrides(base, ServeOverrides(commands_mode="allowlist", allow=["git explode"]))


def test_known_allow_leaf_is_accepted() -> None:
    base = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full"))

    result = apply_ssh_overrides(base, ServeOverrides(commands_mode="allowlist", allow=["ls"]))

    assert result.commands.allow == ["ls"]


def test_describe_overrides_returns_none_when_empty() -> None:
    assert describe_overrides(ServeOverrides()) is None


def test_describe_overrides_lists_only_given_flags() -> None:
    overrides = ServeOverrides(shell=True, commands_mode="allowlist", allow=["ls", "new"])

    summary = describe_overrides(overrides)

    assert summary == "overrides (not from global.yaml): shell=on, commands=allowlist [ls, new]"


def test_describe_overrides_reports_off_and_bind_fields() -> None:
    overrides = ServeOverrides(listen="0.0.0.0", port=18022, dashboard=False)

    summary = describe_overrides(overrides)

    assert summary == "overrides (not from global.yaml): listen=0.0.0.0, port=18022, dashboard=off"


def test_describe_overrides_shows_bare_allow_replacement() -> None:
    overrides = ServeOverrides(allow=["ls"])

    summary = describe_overrides(overrides)

    assert summary == "overrides (not from global.yaml): commands.allow=[ls]"


def test_restrict_host_override_is_named_in_the_startup_line() -> None:
    assert describe_overrides(ServeOverrides(restrict_host=False)) == (
        "overrides (not from global.yaml): restrict_host=off"
    )
    assert ServeOverrides(restrict_host=False).is_empty() is False
