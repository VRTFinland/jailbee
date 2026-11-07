"""The host outbox store: pending GitHub proposals in the broker's ``broker.db``.

One module owns the outbox tables (created by one migration in
`jailbee.orchestration.store`) and every query on them. Both the broker (socket
ops on its query workers) and host commands use it; host reads go through
`read_proposals`, which opens the file read-only and never creates or migrates
it, so `jb outbox` works while the broker is down.

Rows are keyed by the container's Incus ``volatile.uuid``. A proposal name is
``NNN-slug.json`` per container and kind; numbers come from ``outbox_counter``
under ``BEGIN IMMEDIATE`` and are never reused, and ``UNIQUE(uuid, kind, seq)``
backs that up. ``revision`` is the row version, 1 when staged.

Standard library only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jailbee.orchestration.store import StoreError, store_path

if TYPE_CHECKING:
    from jailbee.orchestration.store import BrokerStore

Kind = Literal["issue", "pr"]
KINDS: tuple[Kind, ...] = ("issue", "pr")
MAX_PENDING = 20  # pending proposals per container and kind
_BUSY_TIMEOUT_S = 5.0

# Given the proposal stem (``NNN-slug``), return the final manifest text and its
# bodies under their final names. Called inside the numbering transaction;
# raising aborts the put with nothing stored.
Builder = Callable[[str], tuple[str, Mapping[str, str]]]


class ProposalCapError(Exception):
    """The container already has the cap of pending proposals of that kind.

    Not a `StoreError`: the store worked, the request is refused."""


class ProposalChangedError(Exception):
    """The proposal is gone or its row version moved since the caller read it."""


class ProposalLockedError(Exception):
    """Publication already recorded progress for the proposal; it cannot be dropped."""


@dataclass(frozen=True)
class Proposal:
    uuid: str
    kind: Kind
    name: str
    seq: int
    manifest: str
    created_at: float
    revision: int
    bodies: Mapping[str, str] = field(default_factory=dict)


def _kind(kind: str) -> Kind:
    if kind not in KINDS:
        raise ValueError(f"outbox kind must be issue or pr, not {kind!r}")
    return "issue" if kind == "issue" else "pr"


_COLS = "uuid, kind, name, seq, manifest, created_at, revision"


def _select(
    con: sqlite3.Connection, uuid: str, kind: Kind | None, name: str | None
) -> list[Proposal]:
    sql = f"SELECT {_COLS} FROM outbox_proposal WHERE uuid=?"
    params: list[object] = [uuid]
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    if name is not None:
        sql += " AND name=?"
        params.append(name)
    rows = con.execute(sql + " ORDER BY kind, seq", params).fetchall()
    result = []
    for row in rows:
        bodies = dict(
            con.execute(
                "SELECT body_name, text FROM outbox_body "
                "WHERE uuid=? AND kind=? AND proposal_name=? ORDER BY body_name",
                (row[0], row[1], row[2]),
            ).fetchall()
        )
        result.append(
            Proposal(
                uuid=str(row[0]),
                kind=_kind(str(row[1])),
                name=str(row[2]),
                seq=int(row[3]),
                manifest=str(row[4]),
                created_at=float(row[5]),
                revision=int(row[6]),
                bodies={str(k): str(v) for k, v in bodies.items()},
            )
        )
    return result


class OutboxStore:
    """Read/write access for the broker; each method is one transaction."""

    def __init__(self, store: BrokerStore) -> None:
        self._store = store

    def put_proposal(
        self,
        uuid: str,
        kind: Kind,
        slug: str,
        build: Builder,
        *,
        now: float,
        cap: int = MAX_PENDING,
    ) -> Proposal:
        """Number, build and store one proposal; `ProposalCapError` past `cap`."""
        kind = _kind(kind)
        with self._store.transaction() as con:
            (pending,) = con.execute(
                "SELECT COUNT(*) FROM outbox_proposal WHERE uuid=? AND kind=?", (uuid, kind)
            ).fetchone()
            if pending >= cap:
                raise ProposalCapError(
                    f"{kind} outbox already has the cap of {cap} pending proposals"
                )
            row = con.execute(
                "SELECT last_seq FROM outbox_counter WHERE uuid=? AND kind=?", (uuid, kind)
            ).fetchone()
            (highest,) = con.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM outbox_proposal WHERE uuid=? AND kind=?",
                (uuid, kind),
            ).fetchone()
            seq = max(int(row[0]) if row else 0, int(highest)) + 1
            stem = f"{seq:03d}-{slug}"
            name = f"{stem}.json"
            manifest, bodies = build(stem)
            con.execute(
                "INSERT INTO outbox_counter(uuid, kind, last_seq) VALUES(?,?,?) "
                "ON CONFLICT(uuid, kind) DO UPDATE SET last_seq=excluded.last_seq",
                (uuid, kind, seq),
            )
            con.execute(
                f"INSERT INTO outbox_proposal({_COLS}) VALUES(?,?,?,?,?,?,1)",
                (uuid, kind, name, seq, manifest, now),
            )
            con.executemany(
                "INSERT INTO outbox_body(uuid, kind, proposal_name, body_name, text) "
                "VALUES(?,?,?,?,?)",
                [(uuid, kind, name, body_name, text) for body_name, text in bodies.items()],
            )
        return Proposal(uuid, kind, name, seq, manifest, now, 1, dict(bodies))

    def list_proposals(self, uuid: str, kind: Kind | None = None) -> list[Proposal]:
        """This container's pending proposals, by kind and number, with bodies."""
        with self._store.transaction(write=False) as con:
            return _select(con, uuid, None if kind is None else _kind(kind), None)

    def get_proposal(self, uuid: str, kind: Kind, name: str) -> Proposal | None:
        with self._store.transaction(write=False) as con:
            found = _select(con, uuid, _kind(kind), name)
        return found[0] if found else None

    def drop_proposal(
        self, uuid: str, kind: Kind, name: str, revision: int | None = None
    ) -> Proposal:
        """Delete one proposal with its bodies and return the deleted row.

        With ``revision``, the row version must still match (`ProposalChangedError`
        otherwise, also when the row is gone). A proposal with recorded publication
        progress (a live journal with actions) is refused (`ProposalLockedError`).
        """
        return self.edit_proposal(uuid, kind, name, revision, None)

    def edit_proposal(
        self,
        uuid: str,
        kind: Kind,
        name: str,
        revision: int | None,
        manifest: str | None,
    ) -> Proposal:
        """Replace a proposal's manifest (bumping its row version), or delete it
        when ``manifest`` is None. Same checks as `drop_proposal`."""
        kind = _kind(kind)
        with self._store.transaction() as con:
            found = _select(con, uuid, kind, name)
            if not found or (revision is not None and found[0].revision != revision):
                raise ProposalChangedError(f"{kind}/{name} changed or is gone")
            (live,) = con.execute(
                "SELECT COUNT(*) FROM outbox_journal WHERE uuid=? AND kind=? "
                "AND proposal_name=? AND archived_at IS NULL AND action_count>0",
                (uuid, kind, name),
            ).fetchone()
            if live:
                raise ProposalLockedError(
                    f"{kind}/{name} has recorded publication progress and cannot be removed"
                )
            key = (uuid, kind, name)
            if manifest is None:
                con.execute(
                    "DELETE FROM outbox_body WHERE uuid=? AND kind=? AND proposal_name=?", key
                )
                con.execute("DELETE FROM outbox_proposal WHERE uuid=? AND kind=? AND name=?", key)
            else:
                con.execute(
                    "UPDATE outbox_proposal SET manifest=?, revision=revision+1 "
                    "WHERE uuid=? AND kind=? AND name=?",
                    (manifest, *key),
                )
        return found[0]

    def delete_container(self, uuid: str) -> int:
        """Destroy semantics: delete the uuid's pending proposals (with bodies) and
        drafts. Journals and sent records are never touched, so an uncertain
        journal stays resolvable. Returns the number of proposals deleted."""
        with self._store.transaction() as con:
            (count,) = con.execute(
                "SELECT COUNT(*) FROM outbox_proposal WHERE uuid=?", (uuid,)
            ).fetchone()
            for table in ("outbox_body", "outbox_proposal", "outbox_draft"):
                con.execute(f"DELETE FROM {table} WHERE uuid=?", (uuid,))
        return int(count)


def read_pending_counts(root: Path, kind: Kind = "issue") -> dict[str, int]:
    """Pending proposals per container uuid, read-only (never creates or migrates).

    Journal-aware: a proposal whose live journal records every action as applied
    is finished and no longer pending. A missing file or table reads as empty.
    """
    path = store_path(root)
    if not path.exists():
        return {}
    sql = (
        "SELECT p.uuid, COUNT(*) FROM outbox_proposal p WHERE p.kind=? AND NOT EXISTS ("
        " SELECT 1 FROM outbox_journal j WHERE j.uuid=p.uuid AND j.kind=p.kind"
        " AND j.proposal_name=p.name AND j.archived_at IS NULL AND j.action_count>0"
        " AND (SELECT COUNT(*) FROM outbox_journal_action a WHERE a.uuid=j.uuid"
        "  AND a.kind=j.kind AND a.proposal_name=j.proposal_name AND a.digest=j.digest"
        "  AND a.state='applied') >= j.action_count) GROUP BY p.uuid"
    )
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)
        try:
            return {str(u): int(n) for u, n in con.execute(sql, (_kind(kind),)).fetchall()}
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise StoreError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise StoreError(str(exc)) from exc


def store_path_exists(root: Path) -> bool:
    return store_path(root).exists()


def host_store(root: Path) -> OutboxStore:
    """The store for host-side writes (drop, clear, destroy); migrates on first use."""
    from jailbee.orchestration.store import BrokerStore

    return OutboxStore(BrokerStore(store_path(root)))


def read_proposals(root: Path, uuid: str, kind: Kind | None = None) -> list[Proposal]:
    """Host read of one container's proposals; never creates, migrates or writes.

    A missing file or outbox table reads as no proposals; any other SQLite error
    raises `StoreError`.
    """
    path = store_path(root)
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)
        try:
            con.execute("BEGIN")
            return _select(con, uuid, None if kind is None else _kind(kind), None)
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise StoreError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise StoreError(str(exc)) from exc
