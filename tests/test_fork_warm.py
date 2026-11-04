"""Warm fork pattern policy and reflink seeding."""

import pytest
from pydantic import ValidationError

from jailbee.config import ForkConfig
from jailbee.fork_warm import match_patterns, validate_pattern


@pytest.mark.parametrize("pattern, expected", [(".gradle", ".gradle"), ("*/build", "*/build"), ("app/bibleview-js/node_modules", "app/bibleview-js/node_modules"), ("**/build", "**/build"), ("build/", "build")])
def test_validate_pattern_accepts(pattern, expected):
    assert validate_pattern(pattern) == expected


@pytest.mark.parametrize("pattern", ["", "/abs", "a/../b", "..", ".git/x", "a b", "x;rm", "$(x)", '"q"', ".", "./", "a/./b", "a//b"])
def test_validate_pattern_rejects(pattern):
    with pytest.raises(ValueError):
        validate_pattern(pattern)


def test_match_star_does_not_cross_slash():
    assert match_patterns(["*/build"], ["app/build/", "a/b/build/", "build/"]) == (["app/build"], [])


def test_match_double_star():
    assert match_patterns(["**/build"], ["build/", "a/b/build/"])[0] == ["build", "a/b/build"]


def test_match_reports_unmatched():
    assert match_patterns([".gradle", "nope"], [".gradle/"]) == ([".gradle"], ["nope"])


def test_match_drops_descendant():
    assert match_patterns(["app", "app/build"], ["app/build/", "app/", "app/"])[0] == ["app"]


def test_fork_config():
    assert ForkConfig().warm_paths == []
    assert ForkConfig(warm_paths=["build/"]).warm_paths == ["build"]
    with pytest.raises(ValidationError):
        ForkConfig(warm_paths=["/x"])


def test_full_config_defaults(tmp_path):
    from tests.conftest import make_cfg

    assert make_cfg(tmp_path).fork.warm_paths == []
