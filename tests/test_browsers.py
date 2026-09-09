"""Tests for the builtin browser specs."""

from __future__ import annotations

from jailbee.browsers import builtin_specs
from tests.conftest import make_cfg


def _spec(cfg, name):
    return next(s for s in builtin_specs(cfg) if s.name == name)


def test_host_and_image_sources_give_different_binaries(tmp_path):
    host = make_cfg(tmp_path, browsers={"chrome": {"enabled": True, "source": "host"}})
    image = make_cfg(
        tmp_path, browsers={"chrome": {"enabled": True, "source": "image", "host_path": None}}
    )
    assert _spec(host, "chrome").command[0] == "/opt/google/chrome/google-chrome"
    assert _spec(image, "chrome").command[0] == "/usr/bin/google-chrome-stable"


def test_firefox_image_source_uses_the_apt_binary(tmp_path):
    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True}})
    assert _spec(cfg, "firefox").command[0] == "/usr/bin/firefox"


def test_chrome_gets_the_ozone_flag_only_on_wayland(tmp_path, mocker):
    cfg = make_cfg(tmp_path, browsers={"chrome": {"enabled": True}})
    mocker.patch("jailbee.browsers.host_is_wayland", return_value=True)
    assert "--ozone-platform=wayland" in _spec(cfg, "chrome").command
    mocker.patch("jailbee.browsers.host_is_wayland", return_value=False)
    assert "--ozone-platform=wayland" not in _spec(cfg, "chrome").command


def test_chrome_dark_mode_is_flags_firefox_dark_mode_is_env(tmp_path):
    # Firefox has no --force-dark-mode equivalent; forcing dark *content* is
    # an extension concern. GTK_THEME darkens the browser UI only, and the
    # config field's description says so.
    chrome = make_cfg(tmp_path, browsers={"chrome": {"enabled": True, "dark_mode": True}})
    firefox = make_cfg(tmp_path, browsers={"firefox": {"enabled": True, "dark_mode": True}})
    assert "--force-dark-mode" in _spec(chrome, "chrome").command
    assert _spec(chrome, "chrome").env.get("GTK_THEME") is None
    assert _spec(firefox, "firefox").env["GTK_THEME"] == "Adwaita:dark"
    assert not [a for a in _spec(firefox, "firefox").command if "dark" in a]


def test_each_browser_gets_its_own_profile_pool(tmp_path):
    cfg = make_cfg(
        tmp_path,
        browsers={"chrome": {"enabled": True}, "firefox": {"enabled": True}},
    )
    assert _spec(cfg, "chrome").pool == "chrome-profile"
    assert _spec(cfg, "firefox").pool == "firefox-profile"


def test_configured_url_becomes_default_url_not_a_baked_in_command_arg(tmp_path):
    # The URL used to be appended straight into `command`, which meant a
    # caller-supplied URL at launch time landed *alongside* it instead of
    # replacing it (apps.launch appended its own args unconditionally,
    # producing a launched command with the URL twice). `default_url` is
    # apps.launch's signal to only use it when no explicit args are given.
    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True, "url": "https://x.test"}})
    spec = _spec(cfg, "firefox")
    assert spec.default_url == "https://x.test"
    assert "https://x.test" not in spec.command
    assert spec.accepts_url is True


def test_a_shared_url_reaches_every_enabled_browser(tmp_path):
    """`browsers.url` is the common case: one repo, one app URL, and no
    reason to write it once per browser."""
    cfg = make_cfg(
        tmp_path,
        browsers={
            "url": "https://app.test",
            "chrome": {"enabled": True},
            "firefox": {"enabled": True},
        },
    )
    assert _spec(cfg, "chrome").default_url == "https://app.test"
    assert _spec(cfg, "firefox").default_url == "https://app.test"


def test_a_per_browser_url_overrides_the_shared_one(tmp_path):
    cfg = make_cfg(
        tmp_path,
        browsers={
            "url": "https://app.test",
            "chrome": {"enabled": True},
            "firefox": {"enabled": True, "url": "https://app.test/admin"},
        },
    )
    assert _spec(cfg, "chrome").default_url == "https://app.test"
    assert _spec(cfg, "firefox").default_url == "https://app.test/admin"


def test_no_shared_url_leaves_a_per_browser_url_alone(tmp_path):
    """The pre-`browsers.url` behaviour, pinned: adding the shared field
    must not disturb a config that only sets the per-browser one."""
    cfg = make_cfg(tmp_path, browsers={"chrome": {"enabled": True, "url": "https://only.test"}})
    assert _spec(cfg, "chrome").default_url == "https://only.test"


def test_no_url_anywhere_stays_none(tmp_path):
    cfg = make_cfg(tmp_path, browsers={"chrome": {"enabled": True}})
    assert _spec(cfg, "chrome").default_url is None


def test_a_disabled_browser_produces_no_spec(tmp_path):
    assert builtin_specs(make_cfg(tmp_path)) == []
