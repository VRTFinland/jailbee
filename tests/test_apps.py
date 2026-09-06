"""Tests for the GUI app registry."""

from __future__ import annotations

import pytest

from jailbee.apps import app_log_path, get_app, resolve_apps
from tests.conftest import make_cfg


def test_config_apps_become_specs(tmp_path):
    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f", "args": ["--no-sandbox"]}})
    spec = get_app(cfg, "figma")
    assert spec.command == ["/opt/f/f", "--no-sandbox"]
    assert spec.source == "config"
    assert spec.pool is None


def test_registry_order_is_stable_regardless_of_yaml_key_order(tmp_path):
    # The dashboard action menu and `jailbee apps ls` both render this order.
    # A user's YAML key order must not reshuffle either.
    a = make_cfg(tmp_path, apps={"zed": {"command": "/z"}, "arc": {"command": "/a"}})
    b = make_cfg(tmp_path, apps={"arc": {"command": "/a"}, "zed": {"command": "/z"}})
    assert [s.name for s in resolve_apps(a)] == [s.name for s in resolve_apps(b)]


@pytest.mark.xfail(reason="browsers.builtin_specs lands in Task 8", strict=True)
def test_builtins_come_before_config_apps(tmp_path):
    cfg = make_cfg(
        tmp_path,
        browsers={"firefox": {"enabled": True}},
        apps={"arc": {"command": "/a"}},
    )
    names = [s.name for s in resolve_apps(cfg)]
    assert names.index("firefox") < names.index("arc")


def test_disabled_browsers_are_not_in_the_registry(tmp_path):
    cfg = make_cfg(tmp_path)
    assert [s.name for s in resolve_apps(cfg)] == []


def test_unknown_app_error_names_what_is_available(tmp_path):
    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    with pytest.raises(ValueError, match="figma"):
        get_app(cfg, "nope")


def test_log_path_is_per_app(tmp_path):
    assert app_log_path("firefox") == "/tmp/jailbee-app-firefox.log"
