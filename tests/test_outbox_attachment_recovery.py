"""Host recovery requires explicit consent for an uncertain upload."""

from types import SimpleNamespace

import pytest

from jailbee import attachment_github
from jailbee.attachment_github import UploadTarget
from jailbee.attachment_manifest import Attachment
from jailbee.outbox.attachment_publication import AttachmentPublicationError
from jailbee.outbox.models import OutboxChanged, ProposalId
from jailbee.outbox.publication_db import PublicationStore
from tests.test_outbox_attachment_staging import capture
from tests.test_outbox_broker_ops import PLAIN, comment, manifest, put, stored
from tests.test_outbox_broker_ops import env as env

URL = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"


def test_recovery_attests_url_and_cancel_preserves_uncertain(env, monkeypatch):
    from jailbee.outbox import attachment_recovery as recovery

    desc = capture(env)
    body = "![image](attachment:screen)"
    assert put(env, manifest={**manifest(comment(body)), "attachments": {"screen": desc}}).ok
    (row,) = stored(env)
    publications = PublicationStore(env.store)
    publisher = publications.image_publisher
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda *a: UploadTarget("github.com", "a/b", 1)
    )

    def fail(*args):
        raise attachment_github.UploadUncertainError("timeout")

    monkeypatch.setattr(attachment_github, "upload_to_target", fail)
    with publications.claim(PLAIN, "issue", row.name) as claim:
        digest = publications.stored_digest(PLAIN, "issue", row.name)
        publications.begin(claim, digest=digest, action_count=1, container_full=PLAIN, now=1)
        with pytest.raises(AttachmentPublicationError):
            publisher.resolve_body(
                claim, digest, body, {"screen": Attachment(**desc)}, env.rig.root, "a/b"
            )

    pid = ProposalId("issue", row.name)
    identity = object()
    view = SimpleNamespace(
        available=True,
        identity=identity,
        error=None,
        proposals=[SimpleNamespace(id=pid, revision="rev", error=None)],
    )
    monkeypatch.setattr(recovery, "resolve_target", lambda cfg, incus, name: (cfg, name))
    monkeypatch.setattr(recovery, "instance_identity", lambda incus, name: (identity, PLAIN))
    monkeypatch.setattr(recovery, "load_container", lambda *a, **kw: view)
    monkeypatch.setattr(recovery, "open_publications", lambda: publications)
    args = (None, None, "container", pid, desc["blob"], 1)
    assert (
        recovery.recover(*args, url=URL, allow_duplicate=False, confirm=lambda outcome: False) == 0
    )
    with publications.claim(PLAIN, "issue", row.name) as claim:
        assert publisher.outcomes(claim, digest)[0].state == "uncertain"
    with pytest.raises(OutboxChanged):
        recovery.recover(
            *args,
            url=URL,
            allow_duplicate=False,
            expected_revision="stale",
            confirm=lambda outcome: True,
        )
    assert (
        recovery.recover(*args, url=URL, allow_duplicate=False, confirm=lambda outcome: True) == 0
    )
    with publications.claim(PLAIN, "issue", row.name) as claim:
        assert publisher.outcomes(claim, digest)[0].url == URL
