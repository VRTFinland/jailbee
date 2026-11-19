import hashlib
import json

from jailbee import attachment_github
from jailbee.attachment_github import UploadTarget
from jailbee.branch_pr import publish_source_manifest
from jailbee.outbox.attachment_store import AttachmentStore
from tests.test_branch_pr import source_publisher as source_publisher

URL = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"


def prepare(p):
    data = b"\x89PNG\r\n\x1a\nimage"
    store = AttachmentStore(p["files"]._store())
    blob = store.begin(
        "uuid-full", "screen.png", "image/png", len(data), hashlib.sha256(data).hexdigest(), 1
    )
    store.append("uuid-full", blob, 0, data, 1)
    descriptor = store.finish("uuid-full", blob, 1)
    manifest = dict(p["data"])
    manifest["actions"] = [
        {
            "type": "description",
            "title": "Title",
            "body": "![one](attachment:screen) ![two](attachment:screen)",
        }
    ]
    manifest["attachments"] = {"screen": descriptor}
    p["files"]["m.json"] = json.dumps(manifest)
    return descriptor


def publish(p, **kwargs):
    return publish_source_manifest(
        p["cfg"],
        p["incus"],
        "c",
        "m.json",
        force=False,
        yes=kwargs.get("yes", True),
        confirm=kwargs.get("confirm", lambda _: True),
        publisher="user",
    )


def test_source_images_are_previewed_and_rendered(source_publisher, monkeypatch, capsys):
    p = source_publisher
    desc = prepare(p)
    calls = []
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    monkeypatch.setattr(
        attachment_github, "upload_to_target", lambda *args: calls.append(args) or URL
    )

    def confirm(question):
        preview = capsys.readouterr().out
        assert "Title" in preview and "attachment:screen" in preview
        assert desc["sha256"] in preview and "screen.png" in preview
        assert not calls
        return True

    assert publish(p, yes=False, confirm=confirm) == 0
    assert len(calls) == 1
    assert p["create"].call_args.kwargs["body"] == f"![one]({URL}) ![two]({URL})"


def test_preview_removes_terminal_controls_without_changing_published_text(
    source_publisher, monkeypatch, capsys
):
    p = source_publisher
    prepare(p)
    manifest = json.loads(p["files"]["m.json"])
    manifest["actions"][0]["body"] = "\x1b[2Jpayload ![image](attachment:screen)"
    p["files"]["m.json"] = json.dumps(manifest)
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    monkeypatch.setattr(attachment_github, "upload_to_target", lambda *args: URL)

    def confirm(question):
        assert "\x1b" not in capsys.readouterr().out
        return True

    assert publish(p, yes=False, confirm=confirm) == 0
    assert p["create"].call_args.kwargs["body"] == f"\x1b[2Jpayload ![image]({URL})"


def test_upload_failure_never_creates_pr(source_publisher, monkeypatch):
    p = source_publisher
    prepare(p)
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )

    def fail(*args):
        raise attachment_github.UploadRejectedError("rejected")

    monkeypatch.setattr(attachment_github, "upload_to_target", fail)
    assert publish(p) == 1
    p["create"].assert_not_called()
    assert "m.json" in p["files"]


def test_known_upload_is_reused_after_create_uncertainty_is_resolved(source_publisher, monkeypatch):
    from jailbee.branch_pr import source_journal
    from jailbee.outbox import publication_db

    p = source_publisher
    prepare(p)
    calls = []
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    monkeypatch.setattr(
        attachment_github, "upload_to_target", lambda *args: calls.append(args) or URL
    )
    p["create"].side_effect = [RuntimeError("connection lost"), p["create"].return_value]
    import pytest

    with pytest.raises(RuntimeError, match="connection lost"):
        publish(p)
    assert source_journal("uuid-full", "m.json").uncertain
    assert publish(p, confirm=lambda _: False) == 1
    assert len(calls) == 1
    store = publication_db.open_publications()
    journal = source_journal("uuid-full", "m.json")
    with store.claim("uuid-full", "pr", "m.json") as claim:
        store.resolve(claim, journal.digest, 0, applied=None, sent_outbox="source", now=2)
    assert publish(p) == 0
    assert len(calls) == 1
    assert URL in p["create"].call_args.kwargs["body"]
