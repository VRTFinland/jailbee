"""A destroy or an edit forgets the `uncertain` Sent rows a retried action left.

An uncertain action resolved for retry loses its journal record but keeps its
`uncertain` Sent row, keyed by the proposal's digest. A retry under the same
digest would replace it; once the proposal is deleted by a destroy, or its
manifest is replaced (a new digest), nothing ever can, and the row would claim
forever that something may have been sent.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import publication_db as pdb
from jailbee.outbox.publication_db import PublicationStore, SentEntry
from jailbee.outbox.store_db import OutboxStore
from jailbee.outbox_io import proposal_digest

MANIFEST = '{"version":1,"actions":[]}'


def _entry(name: str, digest: str) -> SentEntry:
    return SentEntry(
        uuid="u1",
        container_prefix="acme",
        container="acme-feat",
        publisher="user",
        outbox="issue",
        manifest=name,
        digest=digest,
        index=0,
        kind="comment",
        repo="o/r",
        target=7,
        url=None,
        title=None,
        content="hello",
    )


def _retried(tmp_path: Path) -> tuple[OutboxStore, str]:
    """A proposal whose only action went uncertain and was resolved for retry."""
    store = BrokerStore(store_path(tmp_path))
    proposals = OutboxStore(store)
    put = proposals.put_proposal(
        "u1", "issue", "fix", lambda stem: (MANIFEST, {f"{stem}-1.md": "text"}), now=1.0
    )
    digest = proposal_digest(put.name, put.manifest, put.bodies)
    ps = PublicationStore(store)
    with ps.claim("u1", "issue", put.name) as claim:
        ps.begin(claim, digest=digest, action_count=1, container_full="acme-feat", now=1.0)
        ps.mark_prepared(claim, digest, 0, repo="o/r", now=1.0)
        ps.settle(
            claim,
            digest,
            0,
            outcome="uncertain",
            detail="timeout",
            sent=_entry(put.name, digest),
            now=1.0,
        )
        ps.resolve(claim, digest, 0, applied=None, sent_outbox="issue", now=2.0)
    # Another container's row must survive both operations.
    ps.record_sent(
        replace(_entry("other.json", digest), uuid="u2"), "uncertain", now=2.0
    )
    assert sorted(r.manifest for r in pdb.read_sent(tmp_path)) == [put.name, "other.json"]
    return proposals, put.name


def test_destroy_forgets_the_uncertain_sent_row_of_a_retried_proposal(tmp_path: Path) -> None:
    proposals, _name = _retried(tmp_path)

    assert proposals.delete_container("u1") == 1

    assert [r.manifest for r in pdb.read_sent(tmp_path)] == ["other.json"]


def test_editing_the_manifest_forgets_the_old_digests_uncertain_sent_row(tmp_path: Path) -> None:
    proposals, name = _retried(tmp_path)

    proposals.edit_proposal("u1", "issue", name, None, '{"version":1,"actions":[{}]}')

    assert [r.manifest for r in pdb.read_sent(tmp_path)] == ["other.json"]
