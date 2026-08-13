"""Tests for the HOME isolation fixtures in conftest.

The suite must not write into the developer's real home dir: jailbee derives
the systemd units dir, the default `shared_dir` and the gpg/ssh mount
sources from `Path.home()`, so an unmocked code path leaves real files
behind. These tests fail if `_isolate_home` is removed or stops taking
effect.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest


def _account_home() -> Path:
    """The account's real home, read from passwd so $HOME can't fake it."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_home_is_redirected_away_from_the_real_home():
    assert Path.home() != _account_home()


def test_home_lives_under_the_pytest_temp_root(tmp_path_factory):
    assert str(Path.home()).startswith(str(tmp_path_factory.getbasetemp()))


def test_default_shared_dir_lands_in_the_isolated_home(tmp_path):
    """The path that used to litter the real home: make_config's default
    `shared_dir` is derived from Path.home().
    """
    from tests.conftest import make_config

    cfg = make_config(tmp_path / "myrepo")

    assert cfg.shared_dir is not None
    assert _account_home() not in cfg.shared_dir.parents


def test_private_home_overrides_the_session_home(private_home):
    assert Path.home() == private_home
    assert private_home != _account_home()


def test_private_home_starts_empty(private_home):
    assert list(private_home.iterdir()) == []


def test_session_home_is_restored_after_private_home(request):
    """`private_home` must not leak into the tests that follow it, or the
    override would silently become session-wide.
    """
    session_home = Path.home()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(request.getfixturevalue("tmp_path")))
        assert Path.home() != session_home
    assert Path.home() == session_home
