"""Tests for the browsers: config block."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jailbee.config.models_tools import BrowserConfig, BrowsersConfig


def _unwrapped_stderr(capsys) -> str:
    """Captured stderr with Rich's soft wrapping undone.

    `tui.hint` renders through a Rich `Console`, which wraps at the capture
    width (80 columns) and will happily split a long path mid-word. Rejoining
    the lines keeps a path assertion an assertion about the message rather
    than about the terminal width the suite happens to run at.
    """
    return capsys.readouterr().err.replace("\n", "")


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


def test_partial_firefox_dict_keeps_the_default_source():
    # The ordinary "just turn Firefox on" config: a dict that sets only
    # `enabled` must not lose Firefox's default source. Pydantic's
    # field-level default_factory only fires when the `firefox` key is
    # absent entirely — a present-but-partial dict validates straight
    # against BrowserConfig, whose own source default is "host". The
    # backfill in _backfill_firefox_default_source restores the intended
    # image source.
    b = BrowsersConfig.model_validate({"firefox": {"enabled": True}})
    assert b.firefox.source == "image"


def test_explicit_host_source_does_not_backfill_firefox_source():
    # Once source is explicitly "host", source must stay "host" — a
    # naive backfill would override an explicit choice, which would be
    # worse than the bug it fixes.
    b = BrowsersConfig.model_validate({"firefox": {"enabled": True, "source": "host"}})
    assert b.firefox.source == "host"


def test_a_firefox_host_path_implies_the_host_source():
    # `firefox: {enabled: true, host_path: /opt/firefox}` says "mount this
    # host install" as plainly as `source: host` does. Backfilling
    # `source: image` over it made `validate_runtime` reject the config for
    # setting `host_path` under `source: image` — an error about a key the
    # user never wrote, naming a contradiction jailbee invented. Chrome's
    # backfill has the mirror-image guard.
    b = BrowsersConfig.model_validate({"firefox": {"enabled": True, "host_path": "/opt/firefox"}})
    assert b.firefox.source == "host"


def test_a_firefox_host_path_config_passes_runtime_validation(tmp_path):
    """The consequence of the backfill guard, end to end.

    Asserting `source == "host"` alone would still pass if `validate_runtime`
    were the thing that changed; this pins the user-visible symptom — a
    config that named an existing Firefox install and got an error about
    `source: image`.
    """
    from tests.conftest import make_cfg

    ff = tmp_path / "ff"
    ff.mkdir()
    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True, "host_path": str(ff)}})
    assert [i for i in cfg.validate_runtime() if "firefox" in i] == []


def test_an_explicit_null_firefox_host_path_still_gets_the_image_source():
    # `host_path: null` is the field's own default, not a host install, so
    # the guard must key on the *value*, not on the key's presence.
    b = BrowsersConfig.model_validate({"firefox": {"enabled": True, "host_path": None}})
    assert b.firefox.source == "image"


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


def test_host_source_with_no_host_path_is_an_error(tmp_path):
    """Firefox has no default `host_path` (see
    `test_firefox_defaults_to_the_golden_image`), so `source: host` with no
    `host_path` is the natural way a user asks for a host Firefox and lands
    directly on this branch — distinct from the missing-directory branch
    below, so it must name `host_path is null` specifically, not just any
    non-empty issue."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True, "source": "host"}})
    assert any(
        "browsers.firefox.source is `host` but host_path is null" in i
        for i in cfg.validate_runtime()
    )


def test_host_path_pointing_at_a_missing_directory_is_an_error(tmp_path):
    """Distinct from the null-host_path branch: here `host_path` is set but
    points nowhere, so the message must name `host_path does not exist`
    specifically."""
    from tests.conftest import make_cfg

    missing = tmp_path / "no-such-chrome-install"
    cfg = make_cfg(
        tmp_path,
        browsers={"chrome": {"enabled": True, "source": "host", "host_path": str(missing)}},
    )
    assert any(
        f"browsers.chrome.host_path does not exist: {missing}" in i for i in cfg.validate_runtime()
    )


def test_legacy_chrome_block_still_loads(tmp_path, capsys):
    from jailbee.config.loader import resolve_browsers_raw

    raw = resolve_browsers_raw({"chrome": {"enabled": True, "url": "https://example.test"}})
    assert "chrome" not in raw
    assert raw["browsers"]["chrome"]["enabled"] is True
    assert raw["browsers"]["chrome"]["url"] == "https://example.test"
    # The legacy block predates `source:`, so it means what it always meant.
    assert raw["browsers"]["chrome"]["source"] == "host"


def test_legacy_chrome_block_warns_where_it_moved(tmp_path, capsys):
    from jailbee.config.loader import load_config_from_text

    path = tmp_path / ".jailbee" / "config.yaml"
    load_config_from_text("container_prefix: myrepo\nchrome:\n  enabled: true\n", path)
    # The notice uses `tui.hint`, not `tui.warn`: it runs on every config
    # load, and `warn` prints to stdout, which is exactly where `jailbee ls
    # --format json` puts script-parsed output. `hint` goes to stderr for
    # that reason (see its own docstring), so the deprecation notice is read
    # from `.err`, not `.out`.
    err = _unwrapped_stderr(capsys)
    assert "chrome:" in err and "browsers.chrome" in err


def test_the_legacy_chrome_notice_names_the_file_that_carries_the_block(tmp_path, capsys):
    """Naming the file is the whole point of emitting the notice one layer
    up from the fold. `jailbee claude ls` and the dashboards load *every*
    registered repo's config, so a notice that said only "in config" sent
    the user hunting through a repo they had already migrated.
    """
    from jailbee.config.loader import load_config_from_text

    path = tmp_path / ".jailbee" / "config.yaml"
    load_config_from_text("container_prefix: myrepo\nchrome:\n  enabled: true\n", path)

    assert str(path) in _unwrapped_stderr(capsys)


def test_the_notice_names_the_global_config_when_the_block_lives_there(tmp_path, capsys):
    """The likelier half of the same bug: `chrome:` shipped in `global.yaml`
    on every host that ever enabled Chrome, so the file to edit is usually
    not the repo config the command happens to be reading.
    """
    from jailbee.config.loader import load_config_from_layers
    from jailbee.global_config import default_global_config_path

    load_config_from_layers(
        {"chrome": {"enabled": True}},
        {"container_prefix": "myrepo"},
        tmp_path / ".jailbee" / "config.yaml",
        origin=str(tmp_path / ".jailbee" / "config.yaml"),
    )

    err = _unwrapped_stderr(capsys)
    assert str(default_global_config_path()) in err
    assert str(tmp_path / ".jailbee" / "config.yaml") not in err


def test_a_block_in_both_layers_gets_a_line_each(tmp_path, capsys):
    """Two files, two edits, two lines — the notice is advice about a file,
    not a statement that the key exists somewhere.
    """
    from jailbee.config.loader import load_config_from_layers
    from jailbee.global_config import default_global_config_path

    repo_path = tmp_path / ".jailbee" / "config.yaml"
    load_config_from_layers(
        {"chrome": {"enabled": True}},
        {"container_prefix": "myrepo", "chrome": {"url": "https://repo.test"}},
        repo_path,
        origin=str(repo_path),
    )

    err = _unwrapped_stderr(capsys)
    assert err.count("browsers.chrome") == 2
    assert str(default_global_config_path()) in err
    assert str(repo_path) in err


def test_effective_url_falls_back_to_the_shared_one(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, browsers={"url": "https://shared.test", "chrome": {"enabled": True}})
    assert cfg.browsers.effective_url("chrome") == "https://shared.test"
    assert cfg.browsers.effective_url("firefox") == "https://shared.test"
    # The per-browser field itself is untouched — the fallback is a read-time
    # resolution, not a mutation of the loaded Config (which is read-only).
    assert cfg.browsers.chrome.url is None


def test_effective_url_prefers_the_per_browser_one(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        browsers={"url": "https://shared.test", "chrome": {"url": "https://own.test"}},
    )
    assert cfg.browsers.effective_url("chrome") == "https://own.test"


def test_effective_url_is_none_when_nothing_sets_one(tmp_path):
    from tests.conftest import make_cfg

    assert make_cfg(tmp_path).browsers.effective_url("chrome") is None


def test_a_legacy_chrome_url_still_wins_over_a_shared_one(tmp_path):
    """The fold puts the legacy `chrome.url` at `browsers.chrome.url`, which
    is a per-browser value and must therefore beat `browsers.url` — the same
    precedence an explicitly written `browsers.chrome.url` gets. A host that
    has not migrated its `global.yaml` keeps the URL it configured.
    """
    from jailbee.config.loader import load_config_from_text

    text = (
        "container_prefix: myrepo\n"
        "chrome:\n"
        "  enabled: true\n"
        "  url: https://legacy.test\n"
        "browsers:\n"
        "  url: https://shared.test\n"
    )
    cfg = load_config_from_text(text, tmp_path / ".jailbee" / "config.yaml")
    assert cfg.browsers.effective_url("chrome") == "https://legacy.test"
    assert cfg.browsers.effective_url("firefox") == "https://shared.test"


def test_the_cap_is_per_file_not_per_process(tmp_path, capsys):
    """Two repos, two lines — the once-only guard keys on the source file.

    A process-wide cap would make the host-wide commands
    (`jailbee claude ls`, the dashboards, which load every registered repo's
    config) report the first unmigrated repo they happen to read and stay
    silent about the rest.
    """
    from jailbee.config.loader import load_config_from_text

    text = "container_prefix: myrepo\nchrome:\n  enabled: true\n"
    for name in ("alpha", "beta"):
        load_config_from_text(text, tmp_path / name / ".jailbee" / "config.yaml")

    err = _unwrapped_stderr(capsys)
    assert err.count("browsers.chrome") == 2
    assert str(tmp_path / "alpha" / ".jailbee" / "config.yaml") in err
    assert str(tmp_path / "beta" / ".jailbee" / "config.yaml") in err


def test_the_notice_prints_once_across_three_real_loads(tmp_path, capsys):
    """The same guarantee through the loader `jailbee new` actually calls.

    The unit above folds a dict directly; this reproduces the reported
    shape — three full `load_config_from_text` builds in one process — so
    a guard placed too close to `resolve_browsers_raw`'s internals (and
    bypassed by the real path) still fails here.
    """
    from jailbee.config.loader import load_config_from_text

    text = "container_prefix: myrepo\nchrome:\n  enabled: true\n"
    path = tmp_path / ".jailbee" / "config.yaml"
    for _ in range(3):
        load_config_from_text(text, path)
    assert capsys.readouterr().err.count("browsers.chrome") == 1


def test_an_explicit_browsers_block_wins_over_the_legacy_one():
    from jailbee.config.loader import resolve_browsers_raw

    raw = resolve_browsers_raw(
        {"chrome": {"url": "https://old.test"}, "browsers": {"chrome": {"url": "https://new.test"}}}
    )
    assert raw["browsers"]["chrome"]["url"] == "https://new.test"


def test_make_cfg_folds_a_legacy_chrome_override_without_printing(tmp_path, capsys):
    """`make_cfg(chrome=...)` must fold the legacy block but stay silent.

    Silence comes from `resolve_browsers_raw` being a pure fold; were the
    notice moved back into it, every such call would write to stderr, and
    the first test to assert on `capsys.readouterr().err` would find a line
    no code under test produced. The fold itself must still happen — this
    asserts both halves, so buying silence by skipping the fold fails here
    too.
    """
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, chrome={"enabled": True})
    assert cfg.browsers.chrome.enabled is True
    assert cfg.browsers.chrome.source == "host"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_no_warning_when_there_is_no_legacy_block(tmp_path, capsys):
    from jailbee.config.loader import load_config_from_text

    text = "container_prefix: myrepo\nbrowsers:\n  chrome:\n    enabled: true\n"
    load_config_from_text(text, tmp_path / ".jailbee" / "config.yaml")
    assert "chrome:" not in capsys.readouterr().err


def test_the_fold_itself_never_prints(capsys):
    """`config_edit.layers.resolve` needs the fold quiet: it runs on every
    editor reload, including mid-session, where a hint printed to the
    terminal would corrupt the display. It gets that for free — the fold
    carries no notice of its own.
    """
    from jailbee.config.loader import resolve_browsers_raw

    raw = resolve_browsers_raw({"chrome": {"enabled": True}})
    assert raw["browsers"]["chrome"]["enabled"] is True
    assert capsys.readouterr().err == ""


def test_emit_hint_false_suppresses_the_notice_but_not_the_fold(tmp_path, capsys):
    """`config_edit.layers.validate` calls `load_config_from_layers` from the
    editor's save handler while the full-screen `Application` is live, where
    `hint()` writing straight to a Rich stderr `Console` would corrupt the
    display.
    """
    from jailbee.config.loader import load_config_from_layers

    path = tmp_path / ".jailbee" / "config.yaml"
    cfg = load_config_from_layers(
        {},
        {"container_prefix": "myrepo", "chrome": {"enabled": True}},
        path,
        origin=str(path),
        emit_hint=False,
    )
    assert cfg.browsers.chrome.enabled is True
    assert capsys.readouterr().err == ""


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
