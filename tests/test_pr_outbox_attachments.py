"""PR publication resolves captured images before publishing any text."""

from jailbee import attachment_github, pr, pr_outbox
from jailbee.attachment_github import UploadTarget
from jailbee.outbox import store_db
from jailbee.outbox.publication_db import PublicationStore
from jailbee.pr_flow import PrScope
from tests.test_outbox_attachment_staging import capture
from tests.test_outbox_broker_ops import PLAIN, put
from tests.test_outbox_broker_ops import env as env

URL = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"


def test_review_and_comment_share_upload(env, monkeypatch):
    desc = capture(env)
    body = "![image](attachment:screen)"
    data = {
        "version": 1,
        "repo": ".",
        "pr": 7,
        "attachments": {"screen": desc},
        "actions": [
            {
                "type": "review",
                "body": body,
                "comments": [{"path": "a.py", "line": 1, "body": body}],
            },
            {"type": "comment", "body": body},
        ],
    }
    result = put(env, kind="pr", manifest=data)
    assert result.ok, result
    (row,) = store_db.read_proposals(env.rig.root, PLAIN, "pr")
    manifest = pr_outbox.parse_manifest(row.name, row.manifest, row.bodies)
    scope = PrScope(repo_root=env.rig.root, remote="origin", subpath=None, prefix="proj")
    info = pr.PrInfo(7, "feature", "a" * 40, "OPEN", "main")
    target = pr_outbox.Target(manifest, info, False, scope)
    monkeypatch.setattr(pr_outbox, "resolve_target", lambda *args, **kwargs: target)
    uploads, reviews, comments = [], [], []
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    monkeypatch.setattr(
        attachment_github, "upload_to_target", lambda *args: uploads.append(args) or URL
    )
    monkeypatch.setattr(
        pr, "submit_review", lambda *args, **kwargs: reviews.append(kwargs) or "review-url"
    )
    monkeypatch.setattr(
        pr, "add_issue_comment", lambda *args, **kwargs: comments.append(args[2]) or "comment-url"
    )
    store = PublicationStore(env.store)
    digest = store.stored_digest(PLAIN, "pr", row.name)
    outcome = pr_outbox.apply_manifest(
        env.rig.cfg,
        env.rig.incus,
        PLAIN,
        target,
        pr_outbox.Progress(frozenset(), {}),
        uid=None,
        publisher="user",
        uuid=PLAIN,
        digest=digest,
        publications=store,
    )
    assert outcome.failure is None
    assert len(uploads) == 1
    assert reviews[0]["body"] == f"![image]({URL})"
    assert reviews[0]["comments"][0]["body"] == f"![image]({URL})"
    assert comments == [f"![image]({URL})"]


def test_uncertain_upload_withholds_text_and_retry(env, monkeypatch):
    desc = capture(env)
    data = {
        "version": 1,
        "repo": ".",
        "pr": 7,
        "attachments": {"screen": desc},
        "actions": [{"type": "comment", "body": "![image](attachment:screen)"}],
    }
    assert put(env, kind="pr", manifest=data).ok
    (row,) = store_db.read_proposals(env.rig.root, PLAIN, "pr")
    manifest = pr_outbox.parse_manifest(row.name, row.manifest, row.bodies)
    target = pr_outbox.Target(
        manifest,
        pr.PrInfo(7, "feature", "a" * 40, "OPEN", "main"),
        False,
        PrScope(env.rig.root, "origin", "proj", None),
    )
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    calls, texts, authorization = [], [], []

    def upload(*args):
        assert authorization
        calls.append(args)
        raise attachment_github.UploadUncertainError("timeout")

    monkeypatch.setattr(attachment_github, "upload_to_target", upload)
    monkeypatch.setattr(pr, "add_issue_comment", lambda *args, **kwargs: texts.append(args))
    store = PublicationStore(env.store)
    digest = store.stored_digest(PLAIN, "pr", row.name)
    for _ in range(2):
        outcome = pr_outbox.apply_manifest(
            env.rig.cfg,
            env.rig.incus,
            PLAIN,
            target,
            pr_outbox.Progress(frozenset(), {}),
            uid=None,
            publisher="user",
            uuid=PLAIN,
            digest=digest,
            publications=store,
            before_action=lambda: authorization.append(True),
        )
        assert outcome.failure
    assert len(calls) == 1
    assert not texts
    assert not store.load(PLAIN, "pr", row.name).actions


def test_review_head_moves_during_upload(env, monkeypatch):
    desc = capture(env)
    data = {
        "version": 1,
        "repo": ".",
        "pr": 7,
        "head_sha": "a" * 40,
        "attachments": {"screen": desc},
        "actions": [{"type": "review", "body": "![image](attachment:screen)", "comments": []}],
    }
    assert put(env, kind="pr", manifest=data).ok
    (row,) = store_db.read_proposals(env.rig.root, PLAIN, "pr")
    manifest = pr_outbox.parse_manifest(row.name, row.manifest, row.bodies)
    scope = PrScope(env.rig.root, "origin", "proj", None)
    target = pr_outbox.Target(
        manifest, pr.PrInfo(7, "feature", "a" * 40, "OPEN", "main"), False, scope
    )
    moved = pr_outbox.Target(
        manifest, pr.PrInfo(7, "feature", "b" * 40, "OPEN", "main"), True, scope
    )
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    monkeypatch.setattr(attachment_github, "upload_to_target", lambda *args: URL)
    monkeypatch.setattr(pr_outbox, "resolve_target", lambda *args, **kwargs: moved)
    posted = []
    monkeypatch.setattr(pr, "submit_review", lambda *args, **kwargs: posted.append(args))
    store = PublicationStore(env.store)
    digest = store.stored_digest(PLAIN, "pr", row.name)
    outcome = pr_outbox.apply_manifest(
        env.rig.cfg,
        env.rig.incus,
        PLAIN,
        target,
        pr_outbox.Progress(frozenset(), {}),
        uid=None,
        publisher="user",
        uuid=PLAIN,
        digest=digest,
        publications=store,
    )
    assert outcome.failure and "changed" in outcome.failure
    assert not posted
    assert not store.load(PLAIN, "pr", row.name).actions
    with store.claim(PLAIN, "pr", row.name) as claim:
        assert store.image_publisher.outcomes(claim, digest)[0].url == URL
    assert "screen.png" in "\n".join(pr_outbox.plan_lines(target, None))
    assert desc["sha256"] in "\n".join(pr_outbox.show_lines(manifest))
