"""Attachment upload publication retains safe outcomes across retries."""

import pytest

from jailbee import attachment_github
from jailbee.attachment_github import UploadTarget
from jailbee.attachment_manifest import Attachment
from jailbee.outbox.attachment_publication import AttachmentPublicationError, AttachmentPublisher
from jailbee.outbox.publication_db import ClaimBusyError, PublicationStore
from tests.test_outbox_attachment_staging import capture
from tests.test_outbox_broker_ops import PLAIN, comment, manifest, put, stored
from tests.test_outbox_broker_ops import env as env

URL = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"


def test_target_failure_is_definite_and_leaves_no_upload_marker(env, monkeypatch):
    desc = capture(env)
    body = "![image](attachment:screen)"
    assert put(env, manifest={**manifest(comment(body)), "attachments": {"screen": desc}}).ok
    (row,) = stored(env)
    publications = PublicationStore(env.store)
    publisher = AttachmentPublisher(env.store, publications)

    def fail(*args):
        raise attachment_github.UploadTargetError("repository lookup failed")

    monkeypatch.setattr(attachment_github, "resolve_target", fail)
    with publications.claim(PLAIN, "issue", row.name) as claim:
        digest = publications.stored_digest(PLAIN, "issue", row.name)
        publications.begin(claim, digest=digest, action_count=1, container_full=PLAIN, now=1)
        with pytest.raises(AttachmentPublicationError, match="repository lookup failed") as error:
            publisher.resolve_body(
                claim, digest, body, {"screen": Attachment(**desc)}, env.rig.root, "acme/app"
            )
        assert not error.value.uncertain
        assert publisher.outcomes(claim, digest) == ()
        assert publications.load(PLAIN, "issue", row.name).actions == ()


def test_upload_is_reused_on_retry(env, monkeypatch):
    desc = capture(env)
    body = "![image](attachment:screen)"
    assert put(env, manifest={**manifest(comment(body)), "attachments": {"screen": desc}}).ok
    (row,) = stored(env)
    publications = PublicationStore(env.store)
    publisher = AttachmentPublisher(env.store, publications)
    calls = []
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    monkeypatch.setattr(
        attachment_github, "upload_to_target", lambda *args: calls.append(args) or URL
    )
    with publications.claim(PLAIN, "issue", row.name) as claim:
        digest = publications.stored_digest(PLAIN, "issue", row.name)
        assert digest is not None
        publications.begin(claim, digest=digest, action_count=1, container_full=PLAIN, now=1)
        for _ in range(2):
            assert (
                publisher.resolve_body(
                    claim, digest, body, {"screen": Attachment(**desc)}, env.rig.root, "acme/app"
                )
                == f"![image]({URL})"
            )
    assert len(calls) == 1


def test_uncertain_upload_blocks_retry_and_stale_claim(env, monkeypatch):
    desc = capture(env)
    body = "![image](attachment:screen)"
    assert put(env, manifest={**manifest(comment(body)), "attachments": {"screen": desc}}).ok
    (row,) = stored(env)
    publications = PublicationStore(env.store)
    publisher = AttachmentPublisher(env.store, publications)
    calls = []
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )

    def fail(*args):
        calls.append(args)
        raise attachment_github.UploadUncertainError("timeout")

    monkeypatch.setattr(attachment_github, "upload_to_target", fail)
    with publications.claim(PLAIN, "issue", row.name) as claim:
        digest = publications.stored_digest(PLAIN, "issue", row.name)
        publications.begin(claim, digest=digest, action_count=1, container_full=PLAIN, now=1)
        for _ in range(2):
            with pytest.raises(AttachmentPublicationError) as error:
                publisher.resolve_body(
                    claim, digest, body, {"screen": Attachment(**desc)}, env.rig.root, "acme/app"
                )
            assert error.value.uncertain
        assert publisher.outcomes(claim, digest)[0].state == "uncertain"
        with pytest.raises(AttachmentPublicationError) as error:
            publisher.resolve_body(claim, digest, "ordinary text", {}, env.rig.root, "other/repo")
        assert error.value.uncertain
        publisher.reconcile(claim, digest, desc["blob"], "github.com", 1, url=URL)
        assert (
            publisher.resolve_body(
                claim, digest, body, {"screen": Attachment(**desc)}, env.rig.root, "acme/app"
            )
            == f"![image]({URL})"
        )
    with pytest.raises(ClaimBusyError):
        publisher.resolve_body(
            claim, digest, body, {"screen": Attachment(**desc)}, env.rig.root, "acme/app"
        )
    assert len(calls) == 1


def test_uploads_are_scoped_to_repository_identity(env, monkeypatch):
    desc = capture(env)
    body = "![image](attachment:screen)"
    assert put(env, manifest={**manifest(comment(body)), "attachments": {"screen": desc}}).ok
    (row,) = stored(env)
    publications = PublicationStore(env.store)
    publisher = AttachmentPublisher(env.store, publications)
    calls = []
    monkeypatch.setattr(
        attachment_github,
        "resolve_target",
        lambda root, repo: UploadTarget("github.com", repo, 1 if repo == "a/a" else 2),
    )
    monkeypatch.setattr(
        attachment_github, "upload_to_target", lambda *args: calls.append(args) or URL
    )
    with publications.claim(PLAIN, "issue", row.name) as claim:
        digest = publications.stored_digest(PLAIN, "issue", row.name)
        publications.begin(claim, digest=digest, action_count=1, container_full=PLAIN, now=1)
        for repo in ("a/a", "b/b"):
            publisher.resolve_body(
                claim, digest, body, {"screen": Attachment(**desc)}, env.rig.root, repo
            )
        assert len(publisher.outcomes(claim, digest)) == 2
    assert len(calls) == 2
