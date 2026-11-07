"""Dropping an issue proposal against its publication claim and journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox.publication_db import PublicationStore
from jailbee.outbox.store_db import OutboxStore, ProposalLockedError
from jailbee.outbox_io import proposal_digest


@pytest.fixture
def stores(tmp_path: Path) -> tuple[OutboxStore, PublicationStore]:
    broker = BrokerStore(store_path(tmp_path))
    return OutboxStore(broker), PublicationStore(broker)


def _stage(ob: OutboxStore) -> tuple[str, str]:
    row = ob.put_proposal(
        "u1", "issue", "a", lambda stem: ('{"version":1}', {f"{stem}-1.md": "b"}), now=1.0
    )
    return row.name, proposal_digest(row.name, row.manifest, row.bodies)


def test_a_proposal_being_published_cannot_be_dropped(stores) -> None:  # type: ignore[no-untyped-def]
    ob, ps = stores
    name, _ = _stage(ob)
    import threading

    errors: list[Exception] = []
    held = threading.Event()
    done = threading.Event()

    def publisher() -> None:
        with ps.claim("u1", "issue", name):
            held.set()
            done.wait(5)

    thread = threading.Thread(target=publisher)
    thread.start()
    held.wait(5)
    try:
        ob.drop_proposal("u1", "issue", name)
    except ProposalLockedError as exc:
        errors.append(exc)
    done.set()
    thread.join()
    assert errors and "being published" in str(errors[0])
    assert ob.get_proposal("u1", "issue", name) is not None


def test_an_untouched_journal_does_not_lock_the_proposal(stores) -> None:  # type: ignore[no-untyped-def]
    """An apply that stopped before its first action leaves an empty journal."""
    ob, ps = stores
    name, digest = _stage(ob)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=2, container_full="c", now=1.0)
    ob.drop_proposal("u1", "issue", name)
    assert ob.get_proposal("u1", "issue", name) is None
    assert ps.load("u1", "issue", name) is None


def test_settled_progress_is_archived_with_the_drop_but_never_uncertain(stores) -> None:  # type: ignore[no-untyped-def]
    ob, ps = stores
    name, digest = _stage(ob)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=2, container_full="c", now=1.0)
        ps.mark_prepared(claim, digest, 0, repo="o/r", now=1.0)
    # Prepared reads as uncertain: neither a plain nor an archiving drop is allowed.
    for archive in (False, True):
        with pytest.raises(ProposalLockedError):
            ob.edit_proposal("u1", "issue", name, None, None, archive_journal=archive)
    with ps.claim("u1", "issue", name) as claim:
        ps.resolve(
            claim,
            digest,
            0,
            applied=("https://github.com/o/r/issues/1", None),
            sent_outbox="issue",
            now=2.0,
        )
    with pytest.raises(ProposalLockedError, match="recorded publication progress"):
        ob.drop_proposal("u1", "issue", name)
    ob.edit_proposal("u1", "issue", name, None, None, archive_journal=True, now=3.0)
    assert ob.get_proposal("u1", "issue", name) is None
    assert ps.load("u1", "issue", name) is None
