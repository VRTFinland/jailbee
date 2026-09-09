"""Unit tests for jailbee.pr_outbox."""

from __future__ import annotations

import json

import pytest


def _manifest_text(**overrides) -> str:
    payload = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": 1234,
        "head_sha": "abc1234",
        "actions": [{"type": "comment", "body": "looks good"}],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_minimal_comment_manifest():
    from jailbee.pr_outbox import CommentAction, parse_manifest

    m = parse_manifest("001-x.json", _manifest_text(), {})

    assert m.name == "001-x.json"
    assert m.repo == "acme/widgets"
    assert m.pr == 1234
    assert m.actions == (CommentAction(body="looks good"),)


def test_parse_review_resolves_body_file_and_span():
    from jailbee.pr_outbox import LineComment, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body_file": "001-summary.md",
                "comments": [
                    {"path": "src/a.py", "line": 88, "body": "rounds the wrong way"},
                    {
                        "path": "src/a.py",
                        "start_line": 120,
                        "line": 134,
                        "body_file": "001-c2.md",
                    },
                ],
            }
        ]
    )
    m = parse_manifest(
        "001-x.json", text, {"001-summary.md": "Two findings.", "001-c2.md": "Extract a helper."}
    )

    review = m.actions[0]
    assert review.body == "Two findings."
    assert review.event == "COMMENT"
    assert review.comments == (
        LineComment(path="src/a.py", line=88, body="rounds the wrong way"),
        LineComment(path="src/a.py", line=134, start_line=120, body="Extract a helper."),
    )


def test_parse_rejects_approve_event():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[{"type": "review", "event": "APPROVE", "body": "ship it", "comments": []}]
    )
    with pytest.raises(ManifestError, match="event 'APPROVE'"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_body_and_body_file_together():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body": "a", "body_file": "b.md"}])
    with pytest.raises(ManifestError, match=r"001-x.json action 0"):
        parse_manifest("001-x.json", text, {"b.md": "b"})


def test_parse_rejects_body_file_escaping_the_outbox():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body_file": "../secrets.md"}])
    with pytest.raises(ManifestError, match="outside the outbox"):
        parse_manifest("001-x.json", text, {"../secrets.md": "leak"})


def test_parse_null_pr_allows_only_a_description():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    ok = parse_manifest(
        "002-d.json",
        _manifest_text(pr=None, head_sha=None, actions=[{"type": "description", "body": "B"}]),
        {},
    )
    assert ok.pr is None

    with pytest.raises(ManifestError, match="pr: null"):
        parse_manifest(
            "002-d.json",
            _manifest_text(pr=None, head_sha=None, actions=[{"type": "comment", "body": "c"}]),
            {},
        )


def test_parse_rejects_unknown_version_by_name():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    with pytest.raises(ManifestError, match="version 2"):
        parse_manifest("001-x.json", _manifest_text(version=2), {})


def test_parse_rejects_start_line_not_before_line():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "x",
                "comments": [
                    {"path": "a.py", "start_line": 134, "line": 120, "body": "b"},
                ],
            }
        ]
    )
    with pytest.raises(ManifestError, match="start_line 134 must be before line 120"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_invalid_side():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "x",
                "comments": [
                    {"path": "a.py", "line": 10, "side": "UP", "body": "b"},
                ],
            }
        ]
    )
    with pytest.raises(ManifestError, match="'RIGHT' or 'LEFT'"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_absolute_comment_path():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "x",
                "comments": [{"path": "/etc/passwd", "line": 1, "body": "b"}],
            }
        ]
    )
    with pytest.raises(ManifestError, match="outside the repo"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_comment_path_with_dotdot_component():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(
        actions=[
            {
                "type": "review",
                "body": "x",
                "comments": [{"path": "../../etc/passwd", "line": 1, "body": "b"}],
            }
        ]
    )
    with pytest.raises(ManifestError, match="outside the repo"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_second_review_action():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    review = {"type": "review", "body": "x", "comments": []}
    text = _manifest_text(actions=[review, review])
    with pytest.raises(ManifestError, match="only one review action"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_second_description_action():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    description = {"type": "description", "body": "x"}
    text = _manifest_text(actions=[description, description])
    with pytest.raises(ManifestError, match="only one description action"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_unknown_action_type():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "bogus", "body": "x"}])
    with pytest.raises(ManifestError, match=r"action 0.*unknown action type 'bogus'"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_too_many_comments():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    comments = [{"path": "a.py", "line": i + 1, "body": "b"} for i in range(101)]
    text = _manifest_text(actions=[{"type": "review", "body": "x", "comments": comments}])
    with pytest.raises(ManifestError, match="more than the cap of 100"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_too_many_actions():
    actions = [{"type": "comment", "body": "x"} for _ in range(51)]
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=actions)
    with pytest.raises(ManifestError, match="more than the cap of 50"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_body_over_64kb():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body": "x" * (65 * 1024)}])
    with pytest.raises(ManifestError, match="larger than 64 KB"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_body_file_absent_from_bodies():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "comment", "body_file": "missing.md"}])
    with pytest.raises(ManifestError, match="no such file in the outbox"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_empty_actions_list():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[])
    with pytest.raises(ManifestError, match="no actions"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_non_integer_pr():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(pr="not-a-number")
    with pytest.raises(ManifestError, match="pr must be an integer"):
        parse_manifest("001-x.json", text, {})


def test_parse_rejects_non_integer_comment_id():
    from jailbee.pr_outbox import ManifestError, parse_manifest

    text = _manifest_text(actions=[{"type": "reply", "comment_id": "abc", "body": "x"}])
    with pytest.raises(ManifestError, match="comment_id must be an integer"):
        parse_manifest("001-x.json", text, {})
