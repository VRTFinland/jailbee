"""Deleting proposal parts keeps attachment declarations in sync."""

import json
from dataclasses import replace

from jailbee.outbox.delete import DeleteSelection, plan_clear, plan_delete
from jailbee.outbox.models import AttachmentUploadView, ProposalId
from tests.outbox_support import IDENTITY
from tests.test_outbox_attachment_staging import capture
from tests.test_outbox_broker_ops import PLAIN, comment, manifest, put, stored
from tests.test_outbox_broker_ops import env as env


def configure(env, monkeypatch):
    from jailbee import db
    from jailbee.outbox import publication_db, service

    monkeypatch.setattr(db, "state_dir", lambda: env.rig.root.parent)
    (env.rig.root.parent / "orchestration").symlink_to(env.rig.root, target_is_directory=True)
    monkeypatch.setattr(publication_db, "default_root", lambda: env.rig.root)
    monkeypatch.setattr(service, "_instance", lambda *args: (IDENTITY, PLAIN))
    return service


def test_delete_last_attachment_use_from_stored_issue(env, monkeypatch):
    service = configure(env, monkeypatch)
    descriptor = capture(env)
    response = put(
        env,
        manifest={
            **manifest(comment("![x](attachment:screen)"), comment("keep")),
            "attachments": {"screen": descriptor},
        },
    )
    assert response.ok
    row = stored(env)[0]
    view = service.load_container(env.rig.cfg, env.rig.incus, PLAIN)
    plan = plan_delete(view, ProposalId("issue", row.name), DeleteSelection(action=0))
    service.execute_delete(env.rig.cfg, env.rig.incus, PLAIN, plan)
    updated = json.loads(stored(env)[0].manifest)
    assert not updated.get("attachments")
    assert updated["actions"] == [comment("keep")]


def test_shared_attachment_in_body_file_survives(env, monkeypatch):
    service = configure(env, monkeypatch)
    descriptor = capture(env)
    response = put(
        env,
        manifest={
            **manifest(
                comment("![x](attachment:screen)"),
                {"type": "comment", "repo": ".", "issue": 7, "body_file": "body.md"},
            ),
            "attachments": {"screen": descriptor},
        },
        bodies={"body.md": "![x](attachment:screen)"},
    )
    assert response.ok, response
    row = stored(env)[0]
    view = service.load_container(env.rig.cfg, env.rig.incus, PLAIN)
    plan = plan_delete(view, ProposalId("issue", row.name), DeleteSelection(action=0))
    service.execute_delete(env.rig.cfg, env.rig.incus, PLAIN, plan)
    updated = json.loads(stored(env)[0].manifest)
    assert updated["attachments"] == {"screen": descriptor}
    assert list(stored(env)[0].bodies.values()) == ["![x](attachment:screen)"]


def test_delete_review_inline_comment_last_use(env, monkeypatch):
    from jailbee.outbox import store_db

    service = configure(env, monkeypatch)
    descriptor = capture(env)
    payload = {
        "version": 1,
        "repo": ".",
        "pr": 42,
        "head_sha": "a" * 40,
        "actions": [
            {
                "type": "review",
                "event": "COMMENT",
                "body": "keep",
                "comments": [
                    {"path": "a.py", "line": 1, "body": "![x](attachment:screen)"},
                    {"path": "a.py", "line": 2, "body": "`![x](attachment:screen)`"},
                ],
            }
        ],
        "attachments": {"screen": descriptor},
    }
    row = store_db.host_store(env.rig.root).put_proposal(
        PLAIN, "pr", "review", lambda stem: (json.dumps(payload), {}), now=1
    )
    view = service.load_container(env.rig.cfg, env.rig.incus, PLAIN)
    plan = plan_delete(view, ProposalId("pr", row.name), DeleteSelection(action=0, comment=0))
    service.execute_delete(env.rig.cfg, env.rig.incus, PLAIN, plan)
    updated = json.loads(store_db.read_proposals(env.rig.root, PLAIN, "pr")[0].manifest)
    assert not updated.get("attachments")
    assert updated["actions"][0]["comments"][0]["body"] == "`![x](attachment:screen)`"


def test_whole_delete_and_clear_warn_about_uploaded_assets(env, monkeypatch):
    service = configure(env, monkeypatch)
    assert put(env).ok
    view = service.load_container(env.rig.cfg, env.rig.incus, PLAIN)
    upload = AttachmentUploadView(
        "digest",
        "blob",
        "github.com",
        1,
        "x.png",
        "image/png",
        1,
        "hash",
        "applied",
        "https://example.com/x",
    )
    proposal = replace(view.proposals[0], uploads=(upload,))
    view = replace(view, proposals=(proposal,))
    plan = plan_delete(view, proposal.id, DeleteSelection())
    assert any("remote assets" in line for line in plan.summary)
    assert any("remote assets" in line for line in plan_clear(view).warnings)
