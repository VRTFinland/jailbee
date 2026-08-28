"""Tests for path utilities."""

from pathlib import Path

import pytest

from jailbee.config import ConfigNotFoundError
from jailbee.paths import display_path, expand_path, find_repo_config, xdg_data_home


def test_expand_path_resolves_home():
    result = expand_path("~/foo/bar")
    assert result == Path.home() / "foo" / "bar"


def test_expand_path_resolves_envvars(monkeypatch):
    monkeypatch.setenv("MYVAR", "/tmp/x")
    result = expand_path("$MYVAR/sub")
    assert result == Path("/tmp/x/sub")


def test_expand_path_returns_absolute_for_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = expand_path("relative/path")
    assert result.is_absolute()


def test_display_path_contracts_the_home_directory():
    """The inverse of `expand_path`'s tilde handling: round-tripping a path
    under $HOME through both must give back the original."""
    path = Path.home() / ".local" / "share" / "jailbee"
    assert display_path(path) == "~/.local/share/jailbee"
    assert expand_path(display_path(path)) == path


def test_display_path_leaves_a_path_outside_home_absolute():
    assert display_path(Path("/data/creds/gisgro")) == "/data/creds/gisgro"


def test_display_path_renders_the_home_directory_itself_as_a_bare_tilde():
    """`relative_to` yields "." for the home directory, and "~/." is not a
    path anyone writes."""
    assert display_path(Path.home()) == "~"


def test_display_path_falls_back_when_home_cannot_be_resolved(mocker):
    """`Path.home()` raises RuntimeError when it cannot be determined, and a
    display helper must not turn that into a traceback."""
    mocker.patch("jailbee.paths.Path.home", side_effect=RuntimeError("no home"))
    assert display_path(Path("/data/x")) == "/data/x"


def test_find_repo_config_returns_cwd_path(tmp_path, monkeypatch):
    (tmp_path / ".gie").mkdir()
    cfg = tmp_path / ".gie" / "config.yaml"
    cfg.write_text("{}\n")
    monkeypatch.chdir(tmp_path)

    result = find_repo_config()

    assert result == cfg


def test_find_repo_config_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigNotFoundError) as exc_info:
        find_repo_config()

    assert ".jailbee/config.yaml" in str(exc_info.value)
    assert "jailbee config init" in str(exc_info.value)


def test_find_repo_config_raises_when_dir_not_file(tmp_path, monkeypatch):
    # .gie/config.yaml is a directory, not a file
    (tmp_path / ".gie" / "config.yaml").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigNotFoundError):
        find_repo_config()


def test_find_repo_config_prefers_jailbee_dir(tmp_path, monkeypatch):
    from jailbee.paths import find_repo_config

    for name in (".jailbee", ".gie"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.yaml").write_text("")
    monkeypatch.chdir(tmp_path)

    assert find_repo_config() == tmp_path / ".jailbee" / "config.yaml"


def test_find_repo_config_accepts_legacy_gie_dir_with_one_warning(tmp_path, monkeypatch, mocker):
    from jailbee import paths

    (tmp_path / ".gie").mkdir()
    (tmp_path / ".gie" / "config.yaml").write_text("")
    monkeypatch.chdir(tmp_path)
    paths._warn_legacy_config_dir.cache_clear()
    warn = mocker.patch("jailbee.tui.warn")

    assert paths.find_repo_config() == tmp_path / ".gie" / "config.yaml"
    assert paths.find_repo_config() == tmp_path / ".gie" / "config.yaml"

    warn.assert_called_once()
    assert ".jailbee" in warn.call_args.args[0]


def test_find_repo_config_error_names_the_new_location(tmp_path, monkeypatch):
    from jailbee.config import ConfigNotFoundError
    from jailbee.paths import find_repo_config

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigNotFoundError, match=r"\.jailbee/config\.yaml"):
        find_repo_config()


def test_repo_config_path_prefers_jailbee_and_returns_none_when_absent(tmp_path):
    from jailbee.paths import repo_config_path

    assert repo_config_path(tmp_path) is None

    (tmp_path / ".gie").mkdir()
    (tmp_path / ".gie" / "config.yaml").write_text("")
    assert repo_config_path(tmp_path) == tmp_path / ".gie" / "config.yaml"

    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text("")
    assert repo_config_path(tmp_path) == tmp_path / ".jailbee" / "config.yaml"


def test_repo_config_dir_name_defaults_to_jailbee(tmp_path):
    from jailbee.paths import repo_config_dir_name

    assert repo_config_dir_name(tmp_path) == ".jailbee"

    (tmp_path / ".gie").mkdir()
    (tmp_path / ".gie" / "config.yaml").write_text("")
    assert repo_config_dir_name(tmp_path) == ".gie"


# ---- xdg_data_home ----


def test_xdg_data_home_default_is_local_share(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert xdg_data_home() == Path.home() / ".local" / "share"


def test_xdg_data_home_honors_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert xdg_data_home() == tmp_path / "xdg"


def test_xdg_data_home_empty_env_falls_back(monkeypatch):
    """Empty $XDG_DATA_HOME should be treated as unset (POSIX convention)."""
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert xdg_data_home() == Path.home() / ".local" / "share"
