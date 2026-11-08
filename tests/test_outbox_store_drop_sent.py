"""Dropping a proposal forgets the `uncertain` Sent rows its retried actions left.

An uncertain action resolved for retry loses its journal record but keeps its
`uncertain` Sent row; if the proposal is then dropped instead of published,
that row would claim forever that something may have been sent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import publication_db as pdb
from jailbee.outbox.publication_db import PublicationStore, SentEntry
from jailbee.outbox.store_db import OutboxStore
from jailbee.outbox_io import proposal_digest

MANIFEST = '{"version":1,"actions":[]}'


def _entry(outbox: str, name: str, digest: str, index: int) -> SentEntry:
    return SentEntry(
        uuid="u1",
        container_prefix="acme",
        container="acme-feat",
        publisher="user",
        outbox=outbox,
        manifest=name,
        digest=digest,
        index=index,
        kind="comment",
        repo="o/r",
        target=7,
        url=None,
        title=None,
        content="hello",
    )


@pytest.mark.parametrize(("kind", "outbox"), [("issue", "issue"), ("pr", "review")])
def test_drop_after_retry_forgets_the_uncertain_sent_row(
    tmp_path: Path, kind: str, outbox: str
) -> None:
    store = BrokerStore(store_path(tmp_path))
    proposals = OutboxStore(store)
    put = proposals.put_proposal(
        "u1", kind, "fix", lambda stem: (MANIFEST, {f"{stem}-1.md": "text"}), now=1.0
    )
    name = put.name
    digest = proposal_digest(name, put.manifest, put.bodies)
    ps = PublicationStore(store)
    with ps.claim("u1", kind, name) as claim:  # type: ignore[arg-type]  # parametrized Kind
        ps.begin(claim, digest=digest, action_count=2, container_full="acme-feat", now=1.0)
        ps.mark_prepared(claim, digest, 0, repo="o/r", now=1.0)
        ps.settle(
            claim,
            digest,
            0,
            outcome="uncertain",
            detail="timeout",
            sent=_entry(outbox, name, digest, 0),
            now=1.0,
        )
        # A human checked GitHub and chose retry: the journal record goes, the row stays.
        ps.resolve(claim, digest, 0, applied=None, sent_outbox=outbox, now=2.0)
    ps.record_sent(_entry(outbox, "other.json", digest, 0), "uncertain", now=2.0)
    assert sorted(r.manifest for r in pdb.read_sent(tmp_path)) == [name, "other.json"]

    proposals.drop_proposal("u1", kind, name)  # type: ignore[arg-type]  # parametrized Kind

    # Only the dropped proposal's row goes; another proposal's stays.
    assert [r.manifest for r in pdb.read_sent(tmp_path)] == ["other.json"]


def test_drop_keeps_applied_sent_rows(tmp_path: Path) -> None:
    store = BrokerStore(store_path(tmp_path))
    proposals = OutboxStore(store)
    put = proposals.put_proposal("u1", "issue", "fix", lambda stem: (MANIFEST, {}), now=1.0)
    digest = proposal_digest(put.name, put.manifest, put.bodies)
    ps = PublicationStore(store)
    ps.record_sent(_entry("issue", put.name, digest, 0), "applied", now=1.0)
    # A sibling uncertain row of the same proposal: the drop must tell the two
    # apart, so this test fails both when the forget is missing (the uncertain
    # row stays) and when it is over-broad (the applied row goes).
    ps.record_sent(_entry("issue", put.name, digest, 1), "uncertain", now=1.0)

    proposals.drop_proposal("u1", "issue", put.name)

    assert [r.state for r in pdb.read_sent(tmp_path)] == ["applied"]
