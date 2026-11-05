"""Fork-only warm overrides in the stdlib spawn helper."""

import pytest

from jailbee.inbox.errors import InboxBadUsage
from jailbee.inbox.send import SendRequest, build_manifest, parse_send, validate


def test_validate_resolves_pr_branch_without_mutating_request():
    request = SendRequest(type="spawn", pr="42", text="task")
    validated = validate(request)
    assert validated.branch == "pr-placeholder"
    assert request.branch == ""


def test_validate_enforces_runner_cross_option_rules():
    with pytest.raises(InboxBadUsage, match="--profile needs --agent claude-jb"):
        validate(SendRequest(type="spawn", branch="b", text="task", agent="claude", profile="p"))


@pytest.mark.parametrize(
    "flags, expected", [(["--warm", "a", "--warm", "c"], ["a", "c"]), (["--no-warm"], [])]
)
def test_spawn_warm_manifest(flags, expected):
    request = parse_send(["spawn", "--branch", "b", "--text", "task", *flags])
    assert build_manifest(request, "warm-1", "body.md")["warm_paths"] == expected


def test_spawn_no_override_omits_warm_paths():
    request = parse_send(["spawn", "--branch", "b", "--text", "task"])
    assert "warm_paths" not in build_manifest(request, "warm-1", "body.md")


@pytest.mark.parametrize(
    "flags",
    [
        ["--warm", "a", "--no-warm"],
        ["--new", "--warm", "a"],
        ["--pr", "1", "--no-warm"],
        ["--warm", ""],
    ],
)
def test_spawn_rejects_invalid_warm_flags(flags):
    with pytest.raises(InboxBadUsage):
        parse_send(["spawn", "--branch", "b", "--text", "task", *flags])


def test_non_spawn_rejects_warm_flags():
    with pytest.raises(InboxBadUsage):
        parse_send(["message", "--text", "hello", "--no-warm"])
