"""Tests for the browsers: config block."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jailbee.config.models_tools import BrowserConfig, BrowsersConfig


def test_chrome_defaults_to_a_host_mount():
    b = BrowsersConfig()
    assert b.chrome.source == "host"
    assert str(b.chrome.host_path) == "/opt/google/chrome"


def test_firefox_defaults_to_the_golden_image():
    # On Ubuntu the host's Firefox is a snap, so /snap/firefox is not
    # usefully mountable — the image is the only source that works
    # out of the box.
    b = BrowsersConfig()
    assert b.firefox.source == "image"
    assert b.firefox.host_path is None


def test_no_browser_is_enabled_by_default():
    b = BrowsersConfig()
    assert b.enabled_names() == []
    assert b.default is None


def test_enabled_names_is_in_registry_order_not_config_order():
    b = BrowsersConfig.model_validate({"firefox": {"enabled": True}, "chrome": {"enabled": True}})
    assert b.enabled_names() == ["chrome", "firefox"]


def test_partial_chrome_dict_keeps_the_default_host_path():
    # The ordinary "just turn Chrome on" config: a dict that sets only
    # `enabled` must not lose Chrome's default host_path. Pydantic's
    # field-level default_factory only fires when the `chrome` key is
    # absent entirely — a present-but-partial dict validates straight
    # against BrowserConfig, whose own host_path default is None.
    b = BrowsersConfig.model_validate({"chrome": {"enabled": True}})
    assert str(b.chrome.host_path) == "/opt/google/chrome"


def test_explicit_image_source_does_not_backfill_host_path():
    # Once source is explicitly "image", host_path must stay None — a
    # naive backfill would set it regardless of source, and runtime
    # validation must be free to reject host_path set under source: image.
    b = BrowsersConfig.model_validate({"chrome": {"enabled": True, "source": "image"}})
    assert b.chrome.host_path is None


def test_unknown_browser_key_is_rejected():
    with pytest.raises(ValidationError):
        BrowsersConfig.model_validate({"safari": {"enabled": True}})


def test_unknown_source_is_rejected():
    with pytest.raises(ValidationError):
        BrowserConfig.model_validate({"source": "flatpak"})


def test_enabling_firefox_adds_exactly_one_pool_and_one_mount(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True}})
    names = [c.name for c in cfg.effective_shared_caches()]
    assert names.count("firefox-profile") == 1
    assert "chrome-profile" not in names
    # source: image needs no mount; only a host-sourced browser gets one.
    assert not [m for m in cfg.effective_host_mounts() if "firefox" in str(m.container)]


def test_host_sourced_firefox_gets_a_mount(tmp_path):
    from tests.conftest import make_cfg

    ff = tmp_path / "ff"
    ff.mkdir()
    cfg = make_cfg(
        tmp_path,
        browsers={"firefox": {"enabled": True, "source": "host", "host_path": str(ff)}},
    )
    mounts = [m for m in cfg.effective_host_mounts() if m.container == "/opt/firefox"]
    assert len(mounts) == 1
    assert mounts[0].readonly is True


def test_default_config_loads_and_resolves_no_browser(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    assert cfg.validate_runtime() == [] or all("browser" not in i for i in cfg.validate_runtime())
    assert cfg.resolve_default_browser() is None


def test_single_enabled_browser_is_the_implicit_default(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True}})
    assert cfg.resolve_default_browser() == "firefox"


def test_two_enabled_browsers_have_no_implicit_default(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        browsers={
            "chrome": {"enabled": True, "host_path": None, "source": "image"},
            "firefox": {"enabled": True},
        },
    )
    assert cfg.resolve_default_browser() is None


def test_explicit_default_naming_a_disabled_browser_is_an_error(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, browsers={"default": "firefox"})
    assert any("browsers.default" in i for i in cfg.validate_runtime())


def test_image_source_with_host_path_is_an_error(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        browsers={
            "chrome": {"enabled": True, "source": "image", "host_path": "/opt/google/chrome"}
        },
    )
    assert any("host_path" in i and "image" in i for i in cfg.validate_runtime())


def test_legacy_chrome_block_still_loads(tmp_path, capsys):
    from jailbee.config.loader import resolve_browsers_raw

    raw = resolve_browsers_raw({"chrome": {"enabled": True, "url": "https://example.test"}})
    assert "chrome" not in raw
    assert raw["browsers"]["chrome"]["enabled"] is True
    assert raw["browsers"]["chrome"]["url"] == "https://example.test"
    # The legacy block predates `source:`, so it means what it always meant.
    assert raw["browsers"]["chrome"]["source"] == "host"


def test_legacy_chrome_block_warns_where_it_moved(capsys):
    from jailbee.config.loader import resolve_browsers_raw

    resolve_browsers_raw({"chrome": {"enabled": True}})
    # `tui.warn` prints to stdout, not stderr (see
    # test_tui.test_warn_plain_keeps_bracketed_text_verbatim) — so the
    # deprecation notice is read from `.out`.
    out = capsys.readouterr().out
    assert "chrome:" in out and "browsers.chrome" in out


def test_an_explicit_browsers_block_wins_over_the_legacy_one():
    from jailbee.config.loader import resolve_browsers_raw

    raw = resolve_browsers_raw(
        {"chrome": {"url": "https://old.test"}, "browsers": {"chrome": {"url": "https://new.test"}}}
    )
    assert raw["browsers"]["chrome"]["url"] == "https://new.test"


def test_no_warning_when_there_is_no_legacy_block(capsys):
    from jailbee.config.loader import resolve_browsers_raw

    resolve_browsers_raw({"browsers": {"chrome": {"enabled": True}}})
    assert "chrome:" not in capsys.readouterr().out


def test_legacy_chrome_block_loads_through_the_real_loader(tmp_path):
    """End-to-end proof, not just the `resolve_browsers_raw` unit: a repo
    config still spelled the old way must load through the real
    `load_config_from_text` path (retired-key checks, deep-merge with the
    global layer, model validation) to a `Config` with the browser
    reachable at `cfg.browsers.chrome`.
    """
    from jailbee.config.loader import load_config_from_text

    text = "container_prefix: myrepo\nchrome:\n  enabled: true\n  url: https://legacy.example\n"
    cfg = load_config_from_text(text, tmp_path / ".jailbee" / "config.yaml")
    assert cfg.browsers.chrome.enabled is True
    assert cfg.browsers.chrome.url == "https://legacy.example"
    assert cfg.browsers.chrome.source == "host"
