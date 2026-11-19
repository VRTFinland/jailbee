"""Draft attachment declarations survive appends and submission."""

import json

from jailbee.orchestration import wire
from tests.test_outbox_attachment_staging import capture
from tests.test_outbox_attachment_staging import env as env
from tests.test_outbox_broker_ops import PLAIN, comment
from tests.test_outbox_broker_pr_kinds import HEAD, append, envelope, review


def test_draft_attachment_namespace_and_submit(env):
    descriptor = capture(env)
    args = dict(uuid=PLAIN, kind="issue", name="evidence")
    first = env.call(
        wire.OUTBOX_DRAFT_APPEND,
        **args,
        action=comment("![one](attachment:screen)"),
        attachments={"screen": descriptor},
    )
    assert first.ok, first
    second = env.call(
        wire.OUTBOX_DRAFT_APPEND,
        **args,
        action=comment("![two](attachment:screen)"),
        attachments={"screen": descriptor},
        expected_revision=1,
    )
    assert second.ok, second
    invalid = env.call(
        wire.OUTBOX_DRAFT_APPEND,
        **args,
        action=comment("![three](attachment:screen)"),
        attachments={"screen": {**descriptor, "name": "other.png"}},
        expected_revision=2,
    )
    assert not invalid.ok
    stale = env.call(
        wire.OUTBOX_DRAFT_APPEND,
        **args,
        action=comment("![three](attachment:screen)"),
        expected_revision=1,
    )
    assert not stale.ok
    missing_revision = env.call(
        wire.OUTBOX_DRAFT_APPEND,
        **args,
        action=comment("![three](attachment:screen)"),
        attachments={"screen": descriptor},
    )
    assert not missing_revision.ok
    shown = env.call(wire.OUTBOX_DRAFT_SHOW, **args)
    assert shown.result["revision"] == 2
    submitted = env.call(wire.OUTBOX_DRAFT_SUBMIT, **args)
    assert submitted.ok, submitted
    proposal = env.call(wire.OUTBOX_SHOW, uuid=PLAIN, kind="issue", name=submitted.result["name"])
    assert json.loads(proposal.result["manifest"])["attachments"] == {"screen": descriptor}


def test_review_draft_reuses_attachment_in_comments_and_summary(env):
    descriptor = capture(env)
    name = "review-12"

    def inline(text):
        return {"path": "src/a.py", "line": 3, "side": "RIGHT", "body": text}

    first = append(
        env,
        review(inline("![first](attachment:screen)")),
        name=name,
        attachments={"screen": descriptor},
    )
    assert first.ok, first
    second = append(
        env, review(inline("![second](attachment:screen)")), name=name, expected_revision=1
    )
    assert second.ok, second
    args = dict(uuid=PLAIN, kind="pr", name=name)
    before = env.call(wire.OUTBOX_DRAFT_SHOW, **args).result
    invalid = env.call(
        wire.OUTBOX_DRAFT_SUBMIT,
        **args,
        review={**envelope(), "body": "summary"},
        attachments={"screen": {**descriptor, "name": "other.png"}},
    )
    assert not invalid.ok
    assert env.call(wire.OUTBOX_DRAFT_SHOW, **args).result == before
    submitted = env.call(
        wire.OUTBOX_DRAFT_SUBMIT,
        **args,
        review={
            "repo": "acme/widgets",
            "pr": 12,
            "head_sha": HEAD,
            "body": "![summary](attachment:screen)",
        },
    )
    assert submitted.ok, submitted
    proposal = env.call(wire.OUTBOX_SHOW, uuid=PLAIN, kind="pr", name=submitted.result["name"])
    manifest = json.loads(proposal.result["manifest"])
    assert manifest["attachments"] == {"screen": descriptor}
    action = manifest["actions"][0]
    assert action["body"] == "![summary](attachment:screen)"
    assert [item["body"] for item in action["comments"]] == [
        "![first](attachment:screen)",
        "![second](attachment:screen)",
    ]


def test_discard_preserves_image_used_by_another_draft(env):
    descriptor = capture(env)
    for name in ("one", "two"):
        response = env.call(
            wire.OUTBOX_DRAFT_APPEND,
            uuid=PLAIN,
            kind="issue",
            name=name,
            action=comment("![image](attachment:screen)"),
            attachments={"screen": descriptor},
        )
        assert response.ok, response
    assert env.call(wire.OUTBOX_DRAFT_DISCARD, uuid=PLAIN, kind="issue", name="one").ok
    response = env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="issue", name="two")
    assert response.ok, response
