"""Seed PR proposals into the host outbox store the way old tests faked container files.

PR proposals live in ``broker.db`` (keyed by the instance uuid) since the outbox
moved to the broker. Tests written for the container file layout described an
outbox as one ``{name: text}`` dict — manifests, ``.md`` bodies and
``<manifest>.progress.json`` sidecars. `seed_pr_store` turns such a dict into
store rows (manifests keep their names; bodies go with each manifest that
references them) and each sidecar into a publication journal with those actions
recorded ``applied``. `instance` builds the ``incus list`` row the host reads the
uuid from.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jailbee.db import state_dir
from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import publication_db
from jailbee.outbox_io import proposal_digest
from jailbee.pr_outbox import _body_references

UUID = "uuid-test"


def root() -> Any:
    return state_dir() / "orchestration"


def instance(name: str, created_at: str = "2026-01-01T00:00:00Z", uuid: str = UUID) -> dict:
    """One ``incus list`` row with the ``volatile.uuid`` the outbox store is keyed by."""
    return {"name": name, "created_at": created_at, "config": {"volatile.uuid": uuid}}


def seed_pr_store(
    files: Mapping[str, str], *, uuid: str = UUID, container: str = "c"
) -> dict[str, str]:
    """Store every manifest of ``files`` (with its bodies) and its sidecar progress.

    Returns the digests by manifest name. Non-JSON manifests are stored as given
    (they are then invalid proposals, as an unparsable file was).
    """
    store = BrokerStore(store_path(root()))
    manifests = sorted(n for n in files if n.endswith(".json") and not n.endswith(".progress.json"))
    digests: dict[str, str] = {}
    with store.transaction() as con:
        (start,) = con.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM outbox_proposal WHERE uuid=? AND kind='pr'",
            (uuid,),
        ).fetchone()
        for seq, name in enumerate(manifests, int(start) + 1):
            text = files[name]
            try:
                # Only action bodies, as the broker stores them (not extension keys).
                bodies = {n: files[n] for n in _body_references(text) if n in files}
            except (ValueError, RecursionError):
                bodies = {}
            con.execute(
                "INSERT INTO outbox_proposal(uuid, kind, name, seq, manifest, created_at, "
                "revision) VALUES(?, 'pr', ?, ?, ?, 1.0, 1)",
                (uuid, name, seq, text),
            )
            con.executemany(
                "INSERT INTO outbox_body(uuid, kind, proposal_name, body_name, text) "
                "VALUES(?, 'pr', ?, ?, ?)",
                [(uuid, name, body, body_text) for body, body_text in bodies.items()],
            )
            digests[name] = proposal_digest(name, text, bodies)
    pubs = publication_db.PublicationStore(store)
    for name in manifests:
        sidecar = files.get(f"{name}.progress.json")
        if sidecar is None:
            continue
        progress = json.loads(sidecar)
        applied = progress.get("applied", [])
        urls = progress.get("urls", {})
        count = len(json.loads(files[name]).get("actions", []))
        with pubs.claim(uuid, "pr", name) as claim:
            pubs.begin(
                claim, digest=digests[name], action_count=count, container_full=container, now=1.0
            )
            for index in applied:
                pubs.mark_prepared(claim, digests[name], index, repo="r", now=1.0)
                pubs.settle(
                    claim,
                    digests[name],
                    index,
                    outcome="applied",
                    url=urls.get(str(index)) or "https://github.com/x/pull/0",
                    now=1.0,
                )
    return digests


def stored_names(uuid: str = UUID) -> list[str]:
    from jailbee.outbox import store_db

    return [p.name for p in store_db.read_proposals(root(), uuid, "pr")]


def journal(name: str, uuid: str = UUID) -> publication_db.Journal | None:
    return publication_db.read_journals(root(), uuid, "pr").get(("pr", name))


def sent_records(uuid: str = UUID) -> list[publication_db.SentRecord]:
    return publication_db.read_sent(root(), uuid=uuid)
