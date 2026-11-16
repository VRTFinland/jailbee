import json

import pytest

from jailbee.pr_manifest import ManifestError, parse_manifest


ATTACHMENT = {
    "blob": "a" * 32,
    "name": "image.png",
    "content_type": "image/png",
    "size": 42,
    "sha256": "b" * 64,
}


def parse(actions, *, attachments=None, pr=1, **extra):
    data = {"version": 1, "repo": ".", "pr": pr, "actions": actions, **extra}
    if attachments is not None:
        data["attachments"] = attachments
    return parse_manifest("test.json", json.dumps(data), {"body.md": "![image](attachment:pic)"})


def test_review_aggregates_summary_and_inline_references():
    manifest = parse(
        [
            {
                "type": "review",
                "body": "![x](attachment:pic)",
                "comments": [{"path": "a.py", "line": 1, "body_file": "body.md"}],
            }
        ],
        attachments={"pic": ATTACHMENT},
    )
    assert manifest.attachments["pic"].name == "image.png"
    with pytest.raises(TypeError):
        manifest.attachments["other"] = manifest.attachments["pic"]


@pytest.mark.parametrize(
    "action",
    [
        {"type": "review", "body": "![x](attachment:pic)", "comments": []},
        {
            "type": "review",
            "body": "summary",
            "comments": [{"path": "a", "line": 1, "body": "![x](attachment:pic)"}],
        },
        {"type": "reply", "comment_id": 10, "body": "![x](attachment:pic)"},
        {"type": "comment", "body": "![x](attachment:pic)"},
        {"type": "description", "body": "![x](attachment:pic)"},
        {"type": "description", "body_file": "body.md"},
    ],
)
def test_each_prose_body_requires_and_accepts_declaration(action):
    with pytest.raises(ManifestError, match="attachment"):
        parse([action])
    assert "pic" in parse([action], attachments={"pic": ATTACHMENT}).attachments


@pytest.mark.parametrize(
    "pr,extra",
    [
        (None, {}),
        (None, {"source": "refs/heads/topic", "source_sha": "a" * 40}),
        (5, {"source": "refs/heads/topic", "source_sha": "a" * 40}),
    ],
)
def test_description_without_pr_or_with_source(pr, extra):
    actions = [{"type": "description", "body_file": "body.md"}]
    with pytest.raises(ManifestError, match="attachment"):
        parse(actions, pr=pr, **extra)
    assert "pic" in parse(actions, pr=pr, attachments={"pic": ATTACHMENT}, **extra).attachments


def test_unused_invalid_and_source_file_metadata_are_rejected():
    actions = [{"type": "comment", "body": "ordinary"}]
    for attachments in ({"pic": ATTACHMENT}, {"pic": {"file": "image.png"}}, []):
        with pytest.raises(ManifestError, match="attachment"):
            parse(actions, attachments=attachments)


def test_literal_code_does_not_count_as_reference():
    actions = [{"type": "comment", "body": "`![x](attachment:pic)`"}]
    manifest = parse(actions)
    assert not manifest.attachments
    with pytest.raises(TypeError):
        manifest.attachments["pic"] = ATTACHMENT
    with pytest.raises(ManifestError, match="attachment"):
        parse(actions, attachments={"pic": ATTACHMENT})
