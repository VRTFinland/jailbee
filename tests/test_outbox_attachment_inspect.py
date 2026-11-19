"""Attachment metadata is disclosed through the shared inspection API."""

import json
from dataclasses import replace

from jailbee.outbox.inspect import build_views, detail_json, overview_json
from jailbee.outbox.models import AttachmentUploadView, ContainerView
from tests.outbox_support import IDENTITY, store


def test_issue_attachment_metadata_and_placement_are_visible() -> None:
    manifest = {
        "version": 1,
        "attachments": {
            "diagram": {
                "blob": "a" * 32,
                "name": "map.png",
                "content_type": "image/png",
                "size": 12,
                "sha256": "a" * 64,
            }
        },
        "actions": [
            {"type": "comment", "repo": ".", "issue": 42, "body": "See\n![map](attachment:diagram)"}
        ],
    }
    snapshot = store("issue", {"001.json": json.dumps(manifest)})
    proposal = build_views(IDENTITY, [snapshot])[0]
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), (proposal,))
    attachment = detail_json(container, proposal)["proposal"]["attachments"][0]
    assert attachment["id"] == "diagram"
    assert attachment["sha256"] == "a" * 64
    assert attachment["name"] == "map.png"
    assert attachment["uses"] == [{"action_index": 0, "line": 2, "comment_index": None}]
    assert overview_json([container])["containers"][0]["proposals"][0]["attachments"] == [
        attachment
    ]
    assert proposal.actions[0].body == "See\n![map](attachment:diagram)"
    manifest["attachments"]["diagram"]["sha256"] = "b" * 64
    changed = store("issue", {"001.json": json.dumps(manifest)})
    assert build_views(IDENTITY, [changed])[0].revision != proposal.revision


def test_pr_review_attachment_discloses_summary_and_inline_comment_positions() -> None:
    manifest = {
        "version": 1,
        "repo": ".",
        "pr": 42,
        "head_sha": "a" * 40,
        "attachments": {
            "image": {
                "blob": "a" * 32,
                "name": "map.png",
                "content_type": "image/png",
                "size": 12,
                "sha256": "a" * 64,
            }
        },
        "actions": [
            {
                "type": "review",
                "event": "COMMENT",
                "body": "![x](attachment:image)",
                "comments": [
                    {"path": "a.py", "line": 1, "body": "first\n![x](attachment:image)"},
                    {"path": "b.py", "line": 2, "body": "![x](attachment:image)"},
                ],
            }
        ],
    }
    snapshot = store("pr", {"001.json": json.dumps(manifest)})
    proposal = build_views(IDENTITY, [snapshot])[0]
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), (proposal,))
    attachment = detail_json(container, proposal)["proposal"]["attachments"][0]
    assert attachment["uses"] == [
        {"action_index": 0, "line": 1, "comment_index": None},
        {"action_index": 0, "line": 2, "comment_index": 0},
        {"action_index": 0, "line": 1, "comment_index": 1},
    ]
    assert proposal.actions[0].comments[0].text == "first\n![x](attachment:image)"


def test_upload_evidence_changes_revision_and_blocks_editing() -> None:
    snapshot = store(
        "issue",
        {
            "001.json": json.dumps(
                {
                    "version": 1,
                    "actions": [{"type": "comment", "repo": ".", "issue": 42, "body": "hello"}],
                }
            )
        },
    )
    before = build_views(IDENTITY, [snapshot])[0]
    upload = AttachmentUploadView(
        "digest", "blob", "github.com", 1, "map.png", "image/png", 12, "a" * 64, "uncertain", None
    )
    snapshot = replace(snapshot, attachment_uploads=(("001.json", upload),))
    proposal = build_views(IDENTITY, [snapshot])[0]
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), (proposal,))
    assert proposal.revision != before.revision
    assert proposal.state == "uncertain"
    assert proposal.edit_block
    assert detail_json(container, proposal)["proposal"]["uploads"][0]["state"] == "uncertain"


def test_ordinary_issue_json_omits_attachments() -> None:
    snapshot = store(
        "issue",
        {
            "001.json": json.dumps(
                {
                    "version": 1,
                    "actions": [{"type": "comment", "repo": ".", "issue": 42, "body": "hello"}],
                }
            )
        },
    )
    proposal = build_views(IDENTITY, [snapshot])[0]
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), (proposal,))
    assert "attachments" not in detail_json(container, proposal)["proposal"]
