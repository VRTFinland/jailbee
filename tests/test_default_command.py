from __future__ import annotations

import pytest

from jailbee.config import ConfigError
from jailbee.default_command import resolve
from jailbee.global_config import GlobalConfig


def _never() -> GlobalConfig:
    raise AssertionError("must not load config")


def test_non_interactive_is_always_help() -> None:
    assert resolve(interactive=False, load=_never) == ("help", None)


@pytest.mark.parametrize("value", ["dashboard", "gui", "console", "help"])
def test_configured_value_is_used(value: str) -> None:
    cfg = GlobalConfig.model_validate({"default_command": value})
    assert resolve(interactive=True, load=lambda: cfg) == (value, None)


def test_default_is_dashboard() -> None:
    assert resolve(interactive=True, load=GlobalConfig) == ("dashboard", None)


def test_broken_global_config_falls_back_to_dashboard_with_warning() -> None:
    def broken() -> GlobalConfig:
        raise ConfigError("Invalid YAML in /g.yaml: boom")

    choice, warning = resolve(interactive=True, load=broken)
    assert choice == "dashboard"
    assert warning is not None and "boom" in warning


def test_unknown_value_is_rejected() -> None:
    with pytest.raises(ValueError):
        GlobalConfig.model_validate({"default_command": "shell"})
