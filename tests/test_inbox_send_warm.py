"""Fork-only warm overrides in the stdlib spawn helper."""

import pytest

from jailbee.inbox.errors import InboxBadUsage
from jailbee.inbox.send import SendRequest, build_manifest, validate


def test_validate_resolves_pr_branch_without_mutating_request():
    request = SendRequest(type="spawn", pr="42", text="task")
    validated = validate(request)
    assert validated.branch == "pr-placeholder"
    assert request.branch == ""


def test_validate_enforces_runner_cross_option_rules():
    with pytest.raises(InboxBadUsage, match="--profile needs --agent claude-jb"):
        validate(SendRequest(type="spawn", branch="b", text="task", agent="claude", profile="p"))


@pytest.mark.parametrize(
    "options, expected", [({"warm": ["a", "c"]}, ["a", "c"]), ({"no_warm": True}, [])]
)
def test_spawn_warm_manifest(options, expected):
    request = validate(SendRequest(type="spawn", branch="b", text="task", **options))
    assert build_manifest(request, "warm-1", "body.md")["warm_paths"] == expected


def test_spawn_no_override_omits_warm_paths():
    request = validate(SendRequest(type="spawn", branch="b", text="task"))
    assert "warm_paths" not in build_manifest(request, "warm-1", "body.md")


@pytest.mark.parametrize(
    "options",
    [
        {"warm": ["a"], "no_warm": True},
        {"new": True, "warm": ["a"]},
        {"pr": "1", "no_warm": True},
    ],
)
def test_spawn_rejects_invalid_warm_flags(options):
    with pytest.raises(InboxBadUsage):
        validate(SendRequest(type="spawn", branch="b", text="task", **options))


def test_non_spawn_rejects_warm_flags():
    with pytest.raises(InboxBadUsage):
        validate(SendRequest(type="message", text="hello", no_warm=True))
