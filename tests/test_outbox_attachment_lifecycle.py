import hashlib
import json

import pytest

from jailbee.orchestration.store import BrokerStore
from jailbee.outbox.attachment_store import AttachmentError
from jailbee.outbox.publication_db import PublicationStore
from jailbee.outbox.store_db import OutboxStore, ProposalLockedError

DATA = b"\x89PNG\r\n\x1a\nimage"


def capture(store):
    attachments = store.attachments
    blob = attachments.begin(
        "owner", "image.png", "image/png", len(DATA), hashlib.sha256(DATA).hexdigest(), 1
    )
    attachments.append("owner", blob, 0, DATA, 2)
    return attachments.finish("owner", blob, 3)


def put(store, descriptor):
    manifest = json.dumps({"attachments": {"image": descriptor}})
    return store.put_proposal("owner", "issue", "test", lambda _: (manifest, {}), now=1)


def test_shared_capture_released_only_after_last_drop(tmp_path):
    store = OutboxStore(BrokerStore(tmp_path / "broker.db"))
    descriptor = capture(store)
    untouched = capture(store)
    first = put(store, descriptor)
    second = put(store, descriptor)
    store.drop_proposal("owner", "issue", first.name)
    assert b"".join(store.attachments.iter_bytes("owner", descriptor["blob"])) == DATA
    store.drop_proposal("owner", "issue", second.name)
    with pytest.raises(AttachmentError):
        store.attachments.metadata("owner", descriptor["blob"])
    assert store.attachments.metadata("owner", untouched["blob"]) == untouched


def test_invalid_replacement_rolls_back_pins_and_manifest(tmp_path):
    store = OutboxStore(BrokerStore(tmp_path / "broker.db"))
    descriptor = capture(store)
    proposal = put(store, descriptor)
    invalid = {**descriptor, "sha256": "0" * 64}
    with pytest.raises(AttachmentError):
        store.edit_proposal(
            "owner",
            "issue",
            proposal.name,
            proposal.revision,
            json.dumps({"attachments": {"image": invalid}}),
        )
    assert store.get_proposal("owner", "issue", proposal.name) == proposal
    assert store.attachments.metadata("owner", descriptor["blob"]) == descriptor


def test_claimed_proposal_keeps_capture(tmp_path):
    broker = BrokerStore(tmp_path / "broker.db")
    store = OutboxStore(broker)
    descriptor = capture(store)
    proposal = put(store, descriptor)
    with PublicationStore(broker).claim("owner", "issue", proposal.name):
        with pytest.raises(ProposalLockedError):
            store.drop_proposal("owner", "issue", proposal.name)
    assert store.attachments.metadata("owner", descriptor["blob"]) == descriptor


@pytest.mark.parametrize("state", ["prepared", "uncertain"])
def test_unresolved_upload_prevents_edit_drop_and_survives_destroy(tmp_path, state):
    broker = BrokerStore(tmp_path / "broker.db")
    store = OutboxStore(broker)
    descriptor = capture(store)
    proposal = put(store, descriptor)
    draft_only = capture(store)
    store.draft_append(
        "owner",
        "issue",
        "draft",
        {},
        {},
        now=1,
        attachments={"shared": descriptor, "draft_only": draft_only},
    )
    with broker.transaction() as con:
        con.execute(
            "INSERT INTO outbox_attachment_upload VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "owner",
                "issue",
                proposal.name,
                "digest",
                descriptor["blob"],
                "github.com",
                1,
                "image.png",
                "image/png",
                len(DATA),
                descriptor["sha256"],
                state,
                None,
            ),
        )
    with pytest.raises(ProposalLockedError):
        store.drop_proposal("owner", "issue", proposal.name)
    with pytest.raises(ProposalLockedError):
        store.edit_proposal("owner", "issue", proposal.name, proposal.revision, proposal.manifest)
    assert store.delete_container("owner") == 0
    assert store.get_proposal("owner", "issue", proposal.name) == proposal
    assert store.attachments.metadata("owner", descriptor["blob"]) == descriptor
    with pytest.raises(AttachmentError):
        store.attachments.metadata("owner", draft_only["blob"])
    with broker.transaction(write=False) as con:
        refs = con.execute(
            "SELECT proposal, id FROM outbox_attachment_ref WHERE uuid=?", ("owner",)
        ).fetchall()
    assert refs == [(proposal.name, descriptor["blob"])]


def test_cli_lists_upload_after_proposal_is_dropped(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jailbee import cli_outbox
    from jailbee.outbox import publication_db

    broker = BrokerStore(tmp_path / "broker.db")
    store = OutboxStore(broker)
    proposal = put(store, capture(store))
    with broker.transaction() as con:
        con.execute(
            "INSERT INTO outbox_attachment_upload VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "owner",
                "issue",
                proposal.name,
                "digest",
                "blob",
                "github.com",
                1,
                "image.png",
                "image/png",
                len(DATA),
                "a" * 64,
                "applied",
                "https://example.test/image",
            ),
        )
    store.drop_proposal("owner", "issue", proposal.name)
    monkeypatch.setattr(publication_db, "default_root", lambda: tmp_path)
    monkeypatch.setattr(cli_outbox, "_run", lambda ctx, config, operation: operation(None, None))
    result = CliRunner().invoke(cli_outbox.app, ["attachments", "container@owner", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["proposal"] == proposal.name
    assert rows[0]["state"] == "applied"
    assert rows[0]["url"] == "https://example.test/image"


def test_destroy_releases_only_referenced_images(tmp_path):
    store = OutboxStore(BrokerStore(tmp_path / "broker.db"))
    descriptor = capture(store)
    untouched = capture(store)
    put(store, descriptor)
    assert store.delete_container("owner") == 1
    with pytest.raises(AttachmentError):
        store.attachments.metadata("owner", descriptor["blob"])
    assert store.attachments.metadata("owner", untouched["blob"]) == untouched
