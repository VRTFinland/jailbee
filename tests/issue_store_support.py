"""Seed and inspect issue proposals in the host outbox store (broker.db) for tests.

The store lives under the per-test isolated ``XDG_STATE_HOME``; these helpers
write rows directly so tests can use any proposal name (``a.json``) and body
files a manifest references, as the old file layout allowed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import publication_db
from jailbee.outbox_io import JournalStore

UUID = "0f9c3c7e-test-box"


def broker() -> BrokerStore:
    return BrokerStore(store_path(publication_db.default_root()))


def _body_refs(value: object) -> set[str]:
    found: set[str] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            ref = item.get("body_file")
            if isinstance(ref, str):
                found.add(ref)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def seed(files: Mapping[str, str], *, uuid: str = UUID) -> None:
    """Stage every ``*.json`` in ``files`` with the bodies it references (replacing)."""
    with broker().transaction() as con:
        (seq,) = con.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM outbox_proposal WHERE uuid=?", (uuid,)
        ).fetchone()
        for name, text in sorted(files.items()):
            if not name.endswith(".json"):
                continue
            con.execute(
                "DELETE FROM outbox_body WHERE uuid=? AND kind='issue' AND proposal_name=?",
                (uuid, name),
            )
            exists = con.execute(
                "SELECT 1 FROM outbox_proposal WHERE uuid=? AND kind='issue' AND name=?",
                (uuid, name),
            ).fetchone()
            if exists:
                con.execute(
                    "UPDATE outbox_proposal SET manifest=? "
                    "WHERE uuid=? AND kind='issue' AND name=?",
                    (text, uuid, name),
                )
            else:
                seq += 1
                con.execute(
                    "INSERT INTO outbox_proposal(uuid, kind, name, seq, manifest, created_at) "
                    "VALUES(?, 'issue', ?, ?, ?, 0)",
                    (uuid, name, seq, text),
                )
            try:
                refs = _body_refs(json.loads(text))
            except ValueError:
                refs = set()
            for body in sorted(refs):
                if body in files:
                    con.execute(
                        "INSERT INTO outbox_body(uuid, kind, proposal_name, body_name, text) "
                        "VALUES(?, 'issue', ?, ?, ?)",
                        (uuid, name, body, files[body]),
                    )


def instance(
    name: str = "test-box", created: str = "2026-09-18", uuid: str = UUID
) -> dict[str, Any]:
    """A raw `incus list` entry carrying the instance uuid."""
    return {"name": name, "created_at": created, "config": {"volatile.uuid": uuid}}


class LiveFiles(MutableMapping[str, str]):
    """The instance's staged issue files, read from and written to the store."""

    def __init__(self, uuid: str = UUID) -> None:
        self.uuid = uuid

    def _snapshot(self) -> dict[str, str]:
        # Straight from the store module, so tests may patch issue_outbox's reader.
        from jailbee.outbox import store_db

        files: dict[str, str] = {}
        for row in store_db.read_proposals(publication_db.default_root(), self.uuid, "issue"):
            files[row.name] = row.manifest
            files.update(row.bodies)
        return files

    def __getitem__(self, key: str) -> str:
        return self._snapshot()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._snapshot())

    def __len__(self) -> int:
        return len(self._snapshot())

    def __setitem__(self, key: str, value: str) -> None:
        with broker().transaction() as con:
            if key.endswith(".json"):
                con.execute(
                    "UPDATE outbox_proposal SET manifest=? WHERE uuid=? AND name=?",
                    (value, self.uuid, key),
                )
            else:
                con.execute(
                    "UPDATE outbox_body SET text=? WHERE uuid=? AND body_name=?",
                    (value, self.uuid, key),
                )

    def __delitem__(self, key: str) -> None:
        with broker().transaction() as con:
            if key.endswith(".json"):
                con.execute("DELETE FROM outbox_proposal WHERE uuid=? AND name=?", (self.uuid, key))
                con.execute(
                    "DELETE FROM outbox_body WHERE uuid=? AND proposal_name=?", (self.uuid, key)
                )
            else:
                con.execute(
                    "DELETE FROM outbox_body WHERE uuid=? AND body_name=?", (self.uuid, key)
                )


def replace_all(files: Mapping[str, str], *, uuid: str = UUID) -> None:
    """Make ``files`` the instance's only staged issue proposals."""
    with broker().transaction() as con:
        con.execute("DELETE FROM outbox_body WHERE uuid=? AND kind='issue'", (uuid,))
        con.execute("DELETE FROM outbox_proposal WHERE uuid=? AND kind='issue'", (uuid,))
    seed(files, uuid=uuid)


class StoreSnapshots(dict[str, Any]):
    """``{"issue": StoreSnapshot, "pr": StoreSnapshot}`` whose issue entry is the store.

    Assigning an issue snapshot stages exactly its files; reading it back
    reflects what publication left there.
    """

    def __setitem__(self, kind: str, snapshot: Any) -> None:
        if kind == "issue":
            replace_all(snapshot.as_dict())
            return
        super().__setitem__(kind, snapshot)

    def __getitem__(self, kind: str) -> Any:
        if kind == "issue":
            from jailbee.outbox.models import StoreSnapshot

            return StoreSnapshot("issue", tuple(sorted(LiveFiles().items())), (), ())
        return super().__getitem__(kind)


class StoreJournals(JournalStore):
    """A `JournalStore` whose issue journals are the real host outbox store's.

    `load` reads the store for this instance; the file-journal machinery
    (locks, paths) stays for the paths that still pass a journal store around.
    `rows` proves nothing was written; the seeding helpers write directly.
    """

    def __init__(self, identity: Any, uuid: str = UUID) -> None:
        from jailbee.outbox.publication_db import open_publications

        super().__init__()
        self.identity = identity
        self.uuid = uuid
        self.pubs = open_publications()

    def load(self, key: Any) -> Any:
        from jailbee import issue_outbox

        return issue_outbox.store_journals(self.identity, self.uuid).get(key.manifest_name)

    def rows(self) -> tuple[list[Any], list[Any]]:
        with broker().transaction(write=False) as con:
            return (
                con.execute("SELECT * FROM outbox_journal ORDER BY proposal_name").fetchall(),
                con.execute(
                    "SELECT * FROM outbox_journal_action ORDER BY proposal_name, idx"
                ).fetchall(),
            )

    def claimed(self, name: str) -> bool:
        with broker().transaction(write=False) as con:
            return (
                con.execute(
                    "SELECT 1 FROM state WHERE key=?",
                    (f"outbox.claim:{self.uuid}:issue:{name}",),
                ).fetchone()
                is not None
            )

    def create(self, name: str, digest: str, count: int) -> None:
        with self.pubs.claim(self.uuid, "issue", name) as claim:
            self.pubs.begin(claim, digest=digest, action_count=count, container_full="t", now=0)

    def mark_prepared(self, key: Any, index: int, *, repo: str) -> None:
        name = key.manifest_name
        journal = self.pubs.load(self.uuid, "issue", name)
        assert journal is not None
        with self.pubs.claim(self.uuid, "issue", name) as claim:
            self.pubs.mark_prepared(claim, journal.digest, index, repo=repo, now=0)

    def record(
        self,
        name: str,
        digest: str,
        count: int,
        index: int,
        *,
        state: str,
        repo: str,
        issue: int | None = None,
        url: str | None = None,
    ) -> None:
        """Leave action ``index`` in ``state`` (prepared, applied or uncertain)."""
        with self.pubs.claim(self.uuid, "issue", name) as claim:
            if self.pubs.load(self.uuid, "issue", name) is None:
                self.pubs.begin(claim, digest=digest, action_count=count, container_full="t", now=0)
            self.pubs.mark_prepared(claim, digest, index, repo=repo, now=0)
            if state == "applied":
                with broker().transaction() as con:
                    con.execute(
                        "UPDATE outbox_journal_action SET state='applied', url=?, issue=? "
                        "WHERE uuid=? AND proposal_name=? AND idx=?",
                        (
                            url or f"https://github.com/{repo}/issues/{issue or 7}",
                            issue,
                            self.uuid,
                            name,
                            index,
                        ),
                    )
            elif state == "uncertain":
                self.pubs.settle(claim, digest, index, outcome="uncertain", detail="x", now=0)
