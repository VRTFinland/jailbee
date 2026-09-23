import dataclasses
import json

import pytest

from jailbee.issue_manifest import (
    CommentAction,
    CreateAction,
    CreatedIssue,
    EditAction,
    ExistingIssue,
    IssueManifestError,
    LabelsAction,
    StateAction,
    parse_manifest,
)


def _manifest(*actions: object, **extra: object) -> str:
    payload: dict[str, object] = {"version": 1, "actions": list(actions)}
    payload.update(extra)
    return json.dumps(payload)


def _create(ref: str = "new", **extra: object) -> dict[str, object]:
    action: dict[str, object] = {
        "type": "create",
        "repo": ".",
        "ref": ref,
        "title": "New issue",
        "body": "Details",
    }
    action.update(extra)
    return action


def _comment(**extra: object) -> dict[str, object]:
    action: dict[str, object] = {
        "type": "comment",
        "repo": ".",
        "issue": 12,
        "body": "Context",
    }
    action.update(extra)
    return action


def _parse_action(action: object, files: dict[str, str] | None = None):
    return parse_manifest("001-work.json", _manifest(action), files or {})


def test_parse_mixed_manifest_into_immutable_actions() -> None:
    payload = {
        "version": 1,
        "actions": [
            {
                "type": "create",
                "repo": ".",
                "ref": "cache-cleanup",
                "title": "Remove legacy cache",
                "body": "Details",
                "labels": ["refactor"],
            },
            {
                "type": "comment",
                "repo": ".",
                "issue_ref": "cache-cleanup",
                "body_file": "001-cache.md",
            },
            {
                "type": "create",
                "repo": "libs/parser",
                "ref": "parser-errors",
                "title": "Improve parser errors",
                "body": "Explain malformed input.",
            },
            {
                "type": "edit",
                "repo": ".",
                "issue": 42,
                "title": "Clarify cache invalidation",
                "body": "Replacement body",
                "expected": {"title": "Cache bug", "body": None},
            },
            {
                "type": "comment",
                "repo": ".",
                "issue": 42,
                "body": "Additional context",
            },
            {
                "type": "labels",
                "repo": ".",
                "issue": 42,
                "add": ["priority:high"],
                "remove": ["needs-triage"],
                "expected": {"labels": ["bug", "needs-triage"]},
            },
            {
                "type": "state",
                "repo": ".",
                "issue": 42,
                "state": "closed",
                "reason": "not_planned",
                "expected": {"state": "open"},
            },
            {
                "type": "state",
                "repo": "libs/parser",
                "issue": 7,
                "state": "open",
                "expected": {"state": "closed"},
            },
        ],
    }
    manifest = parse_manifest("001-work.json", json.dumps(payload), {"001-cache.md": "Follow-up"})

    assert manifest.name == "001-work.json"
    assert manifest.version == 1
    assert manifest.actions[0] == CreateAction(
        repo=".",
        ref="cache-cleanup",
        title="Remove legacy cache",
        body="Details",
        labels=("refactor",),
    )
    assert manifest.actions[1] == CommentAction(
        repo=".", target=CreatedIssue(ref="cache-cleanup"), body="Follow-up"
    )
    assert manifest.actions[2] == CreateAction(
        repo="libs/parser",
        ref="parser-errors",
        title="Improve parser errors",
        body="Explain malformed input.",
        labels=(),
    )
    assert manifest.actions[3] == EditAction(
        repo=".",
        target=ExistingIssue(number=42),
        title="Clarify cache invalidation",
        body="Replacement body",
        expected_title="Cache bug",
        expected_body=None,
        has_expected_title=True,
        has_expected_body=True,
    )
    assert manifest.actions[5] == LabelsAction(
        repo=".",
        target=ExistingIssue(number=42),
        add=("priority:high",),
        remove=("needs-triage",),
        expected_labels=("bug", "needs-triage"),
    )
    assert manifest.actions[6] == StateAction(
        repo=".",
        target=ExistingIssue(number=42),
        state="closed",
        reason="not_planned",
        expected_state="open",
    )
    assert manifest.actions[7] == StateAction(
        repo="libs/parser",
        target=ExistingIssue(number=7),
        state="open",
        reason=None,
        expected_state="closed",
    )
    assert manifest.body_files == frozenset({"001-cache.md"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.name = "changed.json"  # type: ignore[misc]


def test_edit_allows_an_empty_body_to_clear_it() -> None:
    manifest = _parse_action(
        {
            "type": "edit",
            "repo": ".",
            "issue": 8,
            "body": "",
            "expected": {"body": "Old body"},
        }
    )

    assert manifest.actions[0] == EditAction(
        repo=".",
        target=ExistingIssue(number=8),
        title=None,
        body="",
        expected_title=None,
        expected_body="Old body",
        has_expected_title=False,
        has_expected_body=True,
    )


@pytest.mark.parametrize(
    ("expected_body", "accepted"),
    [
        ("x" * (64 * 1024), True),
        ("x" * (64 * 1024 + 1), False),
        ("é" * (32 * 1024), True),
        ("é" * (32 * 1024) + "x", False),
    ],
    ids=["ascii-boundary", "ascii-overflow", "utf8-boundary", "utf8-overflow"],
)
def test_expected_edit_body_uses_the_utf8_body_size_limit(
    expected_body: str, accepted: bool
) -> None:
    action = {
        "type": "edit",
        "repo": ".",
        "issue": 8,
        "body": "Replacement",
        "expected": {"body": expected_body},
    }

    if accepted:
        manifest = _parse_action(action)
        assert manifest.actions[0] == EditAction(
            repo=".",
            target=ExistingIssue(number=8),
            title=None,
            body="Replacement",
            expected_title=None,
            expected_body=expected_body,
            has_expected_title=False,
            has_expected_body=True,
        )
    else:
        with pytest.raises(IssueManifestError, match=r"action 0.*expected\.body.*64 KiB"):
            _parse_action(action)


def test_rejects_a_malformed_unicode_escape_with_action_context() -> None:
    with pytest.raises(IssueManifestError, match=r"action 0.*body.*valid UTF-8"):
        _parse_action(_comment(body="\ud800"))


def test_rejects_raw_malformed_unicode_with_manifest_context() -> None:
    text = json.dumps({"version": 1, "actions": [_comment(body="\ud800")]}, ensure_ascii=False)

    with pytest.raises(IssueManifestError, match=r"001-work\.json.*manifest.*valid UTF-8"):
        parse_manifest("001-work.json", text, {})


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"actions": [_create()]}, "version"),
        ({"version": 2, "actions": [_create()]}, "version"),
        ({"version": 1.0, "actions": [_create()]}, "version"),
        ({"version": True, "actions": [_create()]}, "version"),
        ({"version": 1}, "actions"),
        ({"version": 1, "actions": []}, "actions"),
        ({"version": 1, "actions": [_create()], "extra": 1}, "unknown"),
        ({"version": 1, "actions": [{"repo": "."}]}, "type"),
        ({"version": 1, "actions": [{"type": "delete", "repo": "."}]}, "type"),
        ({"version": 1, "actions": [_create(extra=True)]}, "action 0.*unknown"),
    ],
    ids=[
        "missing-version",
        "unknown-version",
        "float-version",
        "bool-version",
        "missing-actions",
        "empty-actions",
        "unknown-envelope-field",
        "missing-action-type",
        "unknown-action-type",
        "unknown-action-field",
    ],
)
def test_rejects_unknown_or_missing_envelope_action_and_fields(payload: object, match: str) -> None:
    with pytest.raises(IssueManifestError, match=match):
        parse_manifest("001-work.json", json.dumps(payload), {})


@pytest.mark.parametrize("issue", [True, False, 0, -1, 1.5, "1"])
def test_rejects_non_positive_or_non_integer_issue_numbers(issue: object) -> None:
    with pytest.raises(IssueManifestError, match="issue"):
        _parse_action(_comment(issue=issue))


@pytest.mark.parametrize("repo", ["/abs", "../x", "a/../b", "a\\b", "./x"])
def test_rejects_invalid_repo_paths(repo: str) -> None:
    with pytest.raises(IssueManifestError, match="repo"):
        _parse_action(_create(repo=repo))


@pytest.mark.parametrize("repo", ["libs/parser", "third_party/vendor/parser", "owner/repo"])
def test_accepts_normalized_relative_repo_paths_for_later_host_resolution(repo: str) -> None:
    manifest = _parse_action(_create(repo=repo))

    assert manifest.actions[0] == CreateAction(
        repo=repo, ref="new", title="New issue", body="Details", labels=()
    )


@pytest.mark.parametrize(
    ("action", "files", "match"),
    [
        (_create(body_file="body.md"), {"body.md": "x"}, "exactly one"),
        ({"type": "create", "repo": ".", "ref": "new", "title": "New issue"}, {}, "exactly one"),
        (
            {
                "type": "create",
                "repo": ".",
                "ref": "new",
                "title": "New issue",
                "body_file": "missing.md",
            },
            {},
            "missing.md",
        ),
        (
            {
                "type": "create",
                "repo": ".",
                "ref": "new",
                "title": "New issue",
                "body_file": "../body.md",
            },
            {"../body.md": "x"},
            "body_file",
        ),
        (
            {
                "type": "create",
                "repo": ".",
                "ref": "new",
                "title": "New issue",
                "body_file": "nested/body.md",
            },
            {"nested/body.md": "x"},
            "body_file",
        ),
        (
            {
                "type": "create",
                "repo": ".",
                "ref": "new",
                "title": "New issue",
                "body_file": "nested\\body.md",
            },
            {"nested\\body.md": "x"},
            "body_file",
        ),
        (_create(body="x" * (64 * 1024 + 1)), {}, "64 KiB"),
        (
            {
                "type": "create",
                "repo": ".",
                "ref": "new",
                "title": "New issue",
                "body_file": "body.md",
            },
            {"body.md": "é" * 32769},
            "64 KiB",
        ),
    ],
)
def test_rejects_invalid_body_sources(
    action: dict[str, object], files: dict[str, str], match: str
) -> None:
    with pytest.raises(IssueManifestError, match=match):
        _parse_action(action, files)


@pytest.mark.parametrize(
    ("action", "match"),
    [
        (_create(title=""), "title"),
        (_comment(body=""), "body"),
    ],
)
def test_rejects_empty_required_prose(action: dict[str, object], match: str) -> None:
    with pytest.raises(IssueManifestError, match=match):
        _parse_action(action)


@pytest.mark.parametrize(
    ("actions", "match"),
    [
        ([_create("same"), _create("same")], "duplicate.*ref"),
        (
            [
                {"type": "comment", "repo": ".", "issue_ref": "later", "body": "x"},
                _create("later"),
            ],
            "issue_ref",
        ),
        (
            [{"type": "comment", "repo": ".", "issue_ref": "missing", "body": "x"}],
            "issue_ref",
        ),
        (
            [
                _create("other", repo="libs/a"),
                {"type": "comment", "repo": ".", "issue_ref": "other", "body": "x"},
            ],
            "same repo",
        ),
    ],
)
def test_rejects_invalid_local_references(actions: list[object], match: str) -> None:
    with pytest.raises(IssueManifestError, match=match):
        parse_manifest("001-work.json", _manifest(*actions), {})


def test_rejects_more_than_fifty_actions() -> None:
    with pytest.raises(IssueManifestError, match="50"):
        parse_manifest("001-work.json", _manifest(*[_comment() for _ in range(51)]), {})


@pytest.mark.parametrize(
    ("action", "match"),
    [
        (_create(labels=["bug", "bug"]), "duplicate.*label"),
        (_create(labels=["Bug", "bug"]), "duplicate.*label"),
        (
            {
                "type": "labels",
                "repo": ".",
                "issue": 1,
                "add": ["Bug"],
                "remove": ["bug"],
                "expected": {"labels": ["bug"]},
            },
            "both add and remove",
        ),
        (
            {
                "type": "labels",
                "repo": ".",
                "issue": 1,
                "add": [],
                "remove": [],
                "expected": {"labels": []},
            },
            "add or remove",
        ),
        (
            {"type": "labels", "repo": ".", "issue": 1, "add": ["bug"]},
            "expected.labels",
        ),
        (
            {
                "type": "labels",
                "repo": ".",
                "issue": 1,
                "add": ["bug"],
                "expected": {"labels": ["Bug", "bug"]},
            },
            "duplicate.*label",
        ),
    ],
)
def test_rejects_invalid_label_changes(action: dict[str, object], match: str) -> None:
    with pytest.raises(IssueManifestError, match=match):
        _parse_action(action)


@pytest.mark.parametrize(
    ("action", "match"),
    [
        (
            {"type": "edit", "repo": ".", "issue": 1, "title": "New", "expected": {}},
            "expected.title",
        ),
        (
            {
                "type": "edit",
                "repo": ".",
                "issue": 1,
                "title": "New",
                "expected": {"title": "Old", "body": "Unchanged"},
            },
            "expected.body",
        ),
        (
            {
                "type": "edit",
                "repo": ".",
                "issue": 1,
                "body": "New",
                "expected": {"title": "Unchanged", "body": "Old"},
            },
            "expected.title",
        ),
        (
            {"type": "edit", "repo": ".", "issue": 1, "expected": {}},
            "title or body",
        ),
    ],
)
def test_rejects_edit_expectations_that_do_not_match_outputs(
    action: dict[str, object], match: str
) -> None:
    with pytest.raises(IssueManifestError, match=match):
        _parse_action(action)


@pytest.mark.parametrize(
    ("action", "match"),
    [
        (
            {
                "type": "state",
                "repo": ".",
                "issue": 1,
                "state": [],
                "expected": {"state": "open"},
            },
            "state",
        ),
        (
            {
                "type": "state",
                "repo": ".",
                "issue": 1,
                "state": "closed",
                "reason": [],
                "expected": {"state": "open"},
            },
            "reason",
        ),
        (
            {
                "type": "state",
                "repo": ".",
                "issue": 1,
                "state": "closed",
                "expected": {"state": "open"},
            },
            "reason",
        ),
        (
            {
                "type": "state",
                "repo": ".",
                "issue": 1,
                "state": "closed",
                "reason": "duplicate",
                "expected": {"state": "open"},
            },
            "completed.*not_planned",
        ),
        (
            {
                "type": "state",
                "repo": ".",
                "issue": 1,
                "state": "open",
                "reason": "completed",
                "expected": {"state": "closed"},
            },
            "forbids reason",
        ),
        (
            {
                "type": "state",
                "repo": ".",
                "issue": 1,
                "state": "open",
                "expected": {"state": "open"},
            },
            "differ",
        ),
    ],
)
def test_rejects_invalid_state_changes(action: dict[str, object], match: str) -> None:
    with pytest.raises(IssueManifestError, match=match):
        _parse_action(action)


@pytest.mark.parametrize(
    "actions",
    [
        [
            {"type": "edit", "repo": ".", "issue": 1, "title": "One", "expected": {"title": "Old"}},
            {"type": "edit", "repo": ".", "issue": 1, "title": "Two", "expected": {"title": "Old"}},
        ],
        [
            {
                "type": "labels",
                "repo": ".",
                "issue": 1,
                "add": ["bug"],
                "expected": {"labels": []},
            },
            {
                "type": "labels",
                "repo": ".",
                "issue": 1,
                "remove": ["bug"],
                "expected": {"labels": ["bug"]},
            },
        ],
        [
            {
                "type": "state",
                "repo": ".",
                "issue": 1,
                "state": "closed",
                "reason": "completed",
                "expected": {"state": "open"},
            },
            {
                "type": "state",
                "repo": ".",
                "issue": 1,
                "state": "open",
                "expected": {"state": "closed"},
            },
        ],
    ],
    ids=["title", "labels", "state"],
)
def test_rejects_two_changes_to_the_same_target_field(actions: list[object]) -> None:
    with pytest.raises(IssueManifestError, match=r"action 1.*already changes"):
        parse_manifest("001-work.json", _manifest(*actions), {})


def test_allows_multiple_comments_and_disjoint_edits_to_one_target() -> None:
    manifest = parse_manifest(
        "001-work.json",
        _manifest(
            _comment(issue=1, body="One"),
            _comment(issue=1, body="Two"),
            {"type": "edit", "repo": ".", "issue": 1, "title": "New", "expected": {"title": "Old"}},
            {
                "type": "edit",
                "repo": ".",
                "issue": 1,
                "body": "New body",
                "expected": {"body": "Old body"},
            },
        ),
        {},
    )

    assert len(manifest.actions) == 4
