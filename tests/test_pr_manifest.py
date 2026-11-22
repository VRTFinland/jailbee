"""The shared PR schema is pure, with host branch validation still injectable."""

import json

import pytest


@pytest.mark.parametrize(
    ("ref", "valid"),
    [
        ("main", True),
        ("refs/heads/topic", True),
        ("feature/one", True),
        ("HEAD", False),
        ("-bad", False),
        ("a..b", False),
        ("a@{b", False),
        ("a.lock", False),
        ("a.lock/b", False),
        (".hidden/x", False),
        ("a//b", False),
        ("a/", False),
        ("a.", False),
        ("a b", False),
        ("a\tb", False),
        ("a\x7fb", False),
        ("a?b", False),
        ("a[b", False),
        ("a\\b", False),
        ("@", True),
        ("a" * 300, True),
        ("topic/ends./next", True),
    ],
)
def test_pure_branch_ref_validation(ref, valid):
    from jailbee.pr_manifest import valid_branch_ref

    assert valid_branch_ref(ref) is valid


@pytest.mark.parametrize("field,value", [("source", "refs/heads/a..b"), ("base", "HEAD")])
def test_pure_parser_rejects_invalid_refs(field, value):
    from jailbee.pr_manifest import ManifestError, parse_manifest

    raw = {
        "version": 1,
        "repo": "o/r",
        "pr": 7,
        "source": "refs/heads/topic",
        "source_sha": "a" * 40,
        "base": "main",
        "actions": [],
    }
    raw[field] = value
    with pytest.raises(ManifestError, match="not a valid"):
        parse_manifest("001-source.json", json.dumps(raw), {})


def test_host_wrapper_keeps_injected_git_validation(mocker):
    from jailbee import pr_manifest, pr_outbox

    raw = {
        "version": 1,
        "repo": "o/r",
        "pr": 7,
        "source": "refs/heads/topic",
        "source_sha": "a" * 40,
        "base": "main",
        "actions": [],
    }
    text = json.dumps(raw)
    mocker.patch("jailbee.pr_outbox.git.check_ref_format", return_value=True)
    assert pr_outbox.parse_manifest("001-source.json", text, {}) == pr_manifest.parse_manifest(
        "001-source.json", text, {}
    )
    mocker.patch("jailbee.git.check_ref_format", return_value=False)
    with pytest.raises(pr_outbox.ManifestError, match="source is not a valid"):
        pr_outbox.parse_manifest("001-source.json", text, {})
    assert pr_manifest.parse_manifest("001-source.json", text, {}).source == "refs/heads/topic"
    assert pr_outbox.Manifest is pr_manifest.Manifest
    assert pr_outbox.DescriptionAction is pr_manifest.DescriptionAction


def _edit_item(**changes):
    item = {"type": "comment_edit", "comment_id": 7, "body": "new", "expected": {"body": "old"}}
    return {**item, **changes}


def _parse_edit(*items):
    import json

    from jailbee.pr_manifest import parse_manifest

    text = json.dumps({"version": 1, "repo": "a/b", "pr": 3, "actions": list(items)})
    return parse_manifest("m.json", text, {})


def test_parse_comment_edit_carries_the_expected_body():
    from jailbee.pr_manifest import CommentEditAction

    (action,) = _parse_edit(_edit_item()).actions
    assert action == CommentEditAction(comment_id=7, body="new", expected_body="old")


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (_edit_item(expected=None), "expected.body is required"),
        (_edit_item(expected={}), "expected.body is required"),
        (_edit_item(expected={"body": 3}), "expected.body must be a string"),
        (_edit_item(body=""), "non-empty"),
        (_edit_item(comment_id=0), "positive"),
        (_edit_item(comment_id="7"), "comment_id must be an integer"),
    ],
)
def test_parse_comment_edit_rejects_malformed_actions(item, message):
    from jailbee.pr_manifest import ManifestError

    with pytest.raises(ManifestError, match=message):
        _parse_edit(item)


def test_parse_rejects_two_edits_of_one_comment():
    from jailbee.pr_manifest import ManifestError

    with pytest.raises(ManifestError, match="edited twice"):
        _parse_edit(_edit_item(), _edit_item(body="other"))


def _state_manifest(**item):
    action = {"type": "state", "state": "closed", "expected": {"state": "open"}, **item}
    return json.dumps({"version": 1, "repo": "o/r", "pr": 3, "head_sha": None, "actions": [action]})


def test_state_action_parses():
    from jailbee.pr_manifest import StateAction, parse_manifest

    parsed = parse_manifest("m.json", _state_manifest(), {})
    action = parsed.actions[0]
    assert isinstance(action, StateAction)
    assert (action.state, action.expected_state) == ("closed", "open")


@pytest.mark.parametrize(
    "item",
    [
        {"state": "merged"},
        {"state": "closed", "expected": {"state": "closed"}},
        {"expected": {}},
        {"expected": {"state": "bogus"}},
        {"expected": None},
    ],
)
def test_state_action_is_refused_when_malformed(item):
    from jailbee.pr_manifest import ManifestError, parse_manifest

    with pytest.raises(ManifestError):
        parse_manifest("m.json", _state_manifest(**item), {})


def test_state_action_needs_a_numbered_pr():
    from jailbee.pr_manifest import ManifestError, parse_manifest

    text = json.loads(_state_manifest())
    text["pr"] = None
    with pytest.raises(ManifestError):
        parse_manifest("m.json", json.dumps(text), {})
