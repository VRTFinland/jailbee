"""Issue publication resolves captured images only after approval."""

import pytest

from jailbee import attachment_github, issue_github, issue_outbox
from jailbee.attachment_github import UploadTarget
from jailbee.outbox import publication_db
from tests.test_outbox_attachment_staging import capture
from tests.test_outbox_broker_ops import PLAIN, comment, manifest, put, stored
from tests.test_outbox_broker_ops import env as env

URL = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"
BODY = "![Evidence](attachment:screen)"


@pytest.fixture
def flow(env, monkeypatch):
    monkeypatch.setattr(publication_db, "default_root", lambda: env.rig.root)
    env.rig.incus.list_containers.return_value[-1]["created_at"] = "2026-01-01"
    monkeypatch.setattr(
        issue_outbox,
        "resolve_repo_targets",
        lambda cfg: {".": issue_outbox.RepoTarget(".", cfg.repo_root, "acme/app")},
    )
    monkeypatch.setattr(issue_github, "current_login", lambda root: "alice")
    snapshot = issue_github.IssueSnapshot(
        7, "Title", "Body", (), "open", "https://github.com/acme/app/issues/7", False
    )
    monkeypatch.setattr(issue_github, "get_issue", lambda *args: snapshot)
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    descriptor = capture(env)
    assert put(env, manifest={**manifest(comment(BODY)), "attachments": {"screen": descriptor}}).ok
    name = stored(env)[0].name
    uploads = []
    monkeypatch.setattr(
        attachment_github, "upload_to_target", lambda *args: uploads.append(args) or URL
    )
    calls = []
    monkeypatch.setattr(
        issue_github,
        "add_comment",
        lambda *args, **kwargs: (
            calls.append(kwargs)
            or issue_github.MutationReceipt(
                7, "https://github.com/acme/app/issues/7#issuecomment-1"
            )
        ),
    )
    pubs = publication_db.PublicationStore(env.store)

    def prepare():
        return issue_outbox.prepare_batch(env.rig.cfg, env.rig.incus, PLAIN, [name])

    def apply(batch, **kwargs):
        return issue_outbox.apply_batch(
            batch,
            incus=env.rig.incus,
            publications=pubs,
            container_prefix="proj",
            publisher="user",
            **kwargs,
        )

    return prepare, apply, uploads, calls


def test_edit_refuses_when_issue_changes_during_upload(env, monkeypatch):
    monkeypatch.setattr(publication_db, "default_root", lambda: env.rig.root)
    env.rig.incus.list_containers.return_value[-1]["created_at"] = "2026-01-01"
    monkeypatch.setattr(
        issue_outbox,
        "resolve_repo_targets",
        lambda cfg: {".": issue_outbox.RepoTarget(".", cfg.repo_root, "acme/app")},
    )
    monkeypatch.setattr(issue_github, "current_login", lambda root: "alice")
    current = ["Old body"]
    monkeypatch.setattr(
        issue_github,
        "get_issue",
        lambda *args: issue_github.IssueSnapshot(
            7, "Title", current[0], (), "open", "https://github.com/acme/app/issues/7", False
        ),
    )
    descriptor = capture(env)
    edit = {"type": "edit", "repo": ".", "issue": 7, "body": BODY, "expected": {"body": "Old body"}}
    assert put(env, manifest={**manifest(edit), "attachments": {"screen": descriptor}}).ok
    name = stored(env)[0].name
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    uploads = []

    def upload(*args):
        uploads.append(args)
        current[0] = "Changed remotely"
        return URL

    monkeypatch.setattr(attachment_github, "upload_to_target", upload)
    writes = []
    monkeypatch.setattr(issue_github, "edit_issue", lambda *args, **kwargs: writes.append(kwargs))
    batch = issue_outbox.prepare_batch(env.rig.cfg, env.rig.incus, PLAIN, [name])
    issue_outbox.revalidate_batch(batch)
    report = issue_outbox.apply_batch(
        batch,
        incus=env.rig.incus,
        publications=publication_db.PublicationStore(env.store),
        container_prefix="proj",
        publisher="user",
    )
    assert report.failure and not report.failure.uncertain
    assert "expected.body" in report.failure.detail
    assert len(uploads) == 1
    assert writes == []


def test_create_and_comment_share_one_upload(env, monkeypatch):
    monkeypatch.setattr(publication_db, "default_root", lambda: env.rig.root)
    env.rig.incus.list_containers.return_value[-1]["created_at"] = "2026-01-01"
    monkeypatch.setattr(
        issue_outbox,
        "resolve_repo_targets",
        lambda cfg: {".": issue_outbox.RepoTarget(".", cfg.repo_root, "acme/app")},
    )
    monkeypatch.setattr(issue_github, "current_login", lambda root: "alice")
    monkeypatch.setattr(issue_github, "list_labels", lambda *args: {})
    descriptor = capture(env)
    create = {"type": "create", "repo": ".", "ref": "new", "title": "Title", "body": BODY}
    reply = {"type": "comment", "repo": ".", "issue_ref": "new", "body": BODY}
    assert put(env, manifest={**manifest(create, reply), "attachments": {"screen": descriptor}}).ok
    name = stored(env)[0].name
    uploads, bodies = [], []
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    monkeypatch.setattr(
        attachment_github, "upload_to_target", lambda *args: uploads.append(args) or URL
    )

    def mutation(*args, **kwargs):
        bodies.append(kwargs["body"])
        return issue_github.MutationReceipt(7, "https://github.com/acme/app/issues/7")

    monkeypatch.setattr(issue_github, "create_issue", mutation)
    monkeypatch.setattr(issue_github, "add_comment", mutation)
    batch = issue_outbox.prepare_batch(env.rig.cfg, env.rig.incus, PLAIN, [name])
    issue_outbox.revalidate_batch(batch)
    assert uploads == []
    report = issue_outbox.apply_batch(
        batch,
        incus=env.rig.incus,
        publications=publication_db.PublicationStore(env.store),
        container_prefix="proj",
        publisher="user",
    )
    assert report.failure is None
    assert len(uploads) == 1
    assert bodies == [f"![Evidence]({URL})"] * 2


def test_upload_uncertain_stops_retries_without_posting_text(flow, monkeypatch):
    prepare, apply, uploads, calls = flow

    def timeout(*args):
        uploads.append(args)
        raise attachment_github.UploadUncertainError("timeout")

    monkeypatch.setattr(attachment_github, "upload_to_target", timeout)
    batch = prepare()
    first = apply(batch)
    second = apply(prepare())
    assert first.failure and first.failure.uncertain
    assert second.failure and second.failure.uncertain
    assert len(uploads) == 1
    assert calls == []


def test_successful_upload_is_reused_after_text_rejection(flow, monkeypatch):
    prepare, apply, uploads, calls = flow
    original = issue_github.add_comment

    def reject(*args, **kwargs):
        raise issue_github.IssueGithubMutationError("rejected", uncertain=False)

    monkeypatch.setattr(issue_github, "add_comment", reject)
    first = apply(prepare())
    assert first.failure and not first.failure.uncertain
    monkeypatch.setattr(issue_github, "add_comment", original)
    assert apply(prepare()).failure is None
    assert len(uploads) == 1
    assert calls[0]["body"] == f"![Evidence]({URL})"


def test_failed_authorization_never_uploads(flow):
    prepare, apply, uploads, calls = flow

    def refuse():
        raise RuntimeError("not approved")

    with pytest.raises(RuntimeError, match="not approved"):
        apply(prepare(), before_action=refuse)
    assert uploads == []
    assert calls == []


def test_comment_uploads_only_after_approval(flow):
    prepare, apply, uploads, calls = flow
    batch = prepare()
    issue_outbox.revalidate_batch(batch)
    plan = "\n".join(issue_outbox.plan_lines(batch))
    shown = "\n".join(issue_outbox.show_lines(batch.manifests[0].manifest, None, PLAIN))
    descriptor = batch.manifests[0].manifest.attachments["screen"]
    for text in (plan, shown):
        assert "screen.png" in text
        assert "image/png" in text
        assert str(descriptor.size) in text
        assert descriptor.sha256 in text
        assert "action 0" in text
        assert "line 1" in text
        assert "attachment:screen" in text
    assert uploads == []
    events = []
    report = apply(batch, before_action=lambda: events.append(len(uploads)))
    assert report.failure is None
    assert events == [0]
    assert len(uploads) == 1
    assert calls[0]["body"] == f"![Evidence]({URL})"
    assert batch.manifests[0].actions[0].action.body == BODY
