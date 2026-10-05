"""Tests for the global `--repo PREFIX` argv lift."""

from __future__ import annotations

import pytest

from jailbee.repo_option import RepoOptionError, lift_repo, with_repo_first


@pytest.mark.parametrize(
    ("argv", "prefix", "rest"),
    [
        (["--repo", "x", "ls"], "x", ["ls"]),
        (["ls", "--repo", "x"], "x", ["ls"]),
        (["ls", "--repo=x", "--all"], "x", ["ls", "--all"]),
        (["chrome", "feat", "--repo", "andbible"], "andbible", ["chrome", "feat"]),
        (["git", "--repo", "x", "pull", "feat"], "x", ["git", "pull", "feat"]),
        (["net", "--repo", "x", "egress", "ls"], "x", ["net", "egress", "ls"]),
        (["ls"], None, ["ls"]),
        ([], None, []),
    ],
)
def test_lift_finds_the_global_option_anywhere(argv, prefix, rest):
    assert lift_repo(argv) == (prefix, rest)


def test_tokens_after_double_dash_are_never_inspected():
    argv = ["exec", "feat", "--", "grep", "--repo", "x"]
    assert lift_repo(argv) == (None, argv)


def test_leaf_owned_repo_is_left_for_the_leaf():
    # `net egress add --repo` is the leaf's own repo-scope flag.
    argv = ["net", "egress", "add", "example.com", "--repo"]
    assert lift_repo(argv) == (None, argv)
    assert lift_repo(["console", "--repo", "x"]) == (None, ["console", "--repo", "x"])


def test_repo_inside_the_command_path_is_global_even_for_an_owning_leaf():
    argv = ["net", "--repo", "x", "egress", "add", "--repo", "example.com"]
    assert lift_repo(argv) == ("x", ["net", "egress", "add", "--repo", "example.com"])


def test_alias_paths_resolve_like_their_public_leaf():
    # `egress add` is a hidden alias of `net egress add`, which owns `--repo`.
    argv = ["egress", "add", "example.com", "--repo"]
    assert lift_repo(argv) == (None, argv)


def test_unknown_command_lifts_only_a_leading_option():
    assert lift_repo(["--repo", "x", "nope", "--repo", "y"]) == (
        "x",
        ["nope", "--repo", "y"],
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--repo", "x", "ls", "--repo", "y"],
        ["ls", "--repo"],
        ["ls", "--repo", "--all"],
        ["ls", "--repo="],
        ["--repo"],
    ],
)
def test_malformed_or_repeated_option_is_an_error(argv):
    with pytest.raises(RepoOptionError):
        lift_repo(argv)


def test_repo_after_value_taking_option_is_still_lifted():
    # Click, not the lift, then reports `--base` missing a value.
    assert lift_repo(["new", "feat", "--base", "--repo", "x"]) == (
        "x",
        ["new", "feat", "--base"],
    )


def test_no_tree_walk_without_repo_option(mocker):
    walk = mocker.patch("jailbee.remote_ssh.router.routable_leaf_paths")
    assert lift_repo(["ls", "--all"]) == (None, ["ls", "--all"])
    walk.assert_not_called()


def test_with_repo_first():
    assert with_repo_first("x", ["ls"]) == ["--repo", "x", "ls"]
    assert with_repo_first(None, ["ls"]) == ["ls"]
