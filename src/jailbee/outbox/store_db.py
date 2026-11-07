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

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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


# Given the proposal stem, the stored draft's manifest JSON text and its bodies,
# return the final manifest text and bodies, as a `Builder` does.
DraftBuilder = Callable[[str, str, Mapping[str, str]], tuple[str, Mapping[str, str]]]
# Given the draft manifest and its bodies and the action being appended, return
# the action to store. Called under the draft's row lock.
DraftPrepare = Callable[[dict[str, Any], Mapping[str, str], dict[str, Any]], dict[str, Any]]
# Given the draft's would-be manifest and bodies; raising aborts the append.
DraftValidate = Callable[[dict[str, Any], Mapping[str, str]], None]
MAX_DRAFTS = 20  # open draft batches per container and kind


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


class DraftNotFoundError(Exception):
    """No such draft for this container."""


class DraftCapError(Exception):
    """The container already has the cap of open drafts of that kind."""


@dataclass(frozen=True)
class Draft:
    uuid: str
    kind: Kind
    name: str
    manifest: str
    bodies: Mapping[str, str]
    created_at: float
    updated_at: float
    revision: int

    @property
    def action_count(self) -> int:
        try:
            actions = json.loads(self.manifest)["actions"]
            return len(actions) if isinstance(actions, list) else 0
        except (ValueError, KeyError, TypeError):
            return 0


_DRAFT_COLS = "uuid, kind, name, manifest, bodies, created_at, updated_at, revision"


def _draft_from_row(row: tuple[Any, ...]) -> Draft:
    return Draft(
        uuid=str(row[0]),
        kind=_kind(str(row[1])),
        name=str(row[2]),
        manifest=str(row[3]),
        bodies={str(k): str(v) for k, v in json.loads(row[4]).items()},
        created_at=float(row[5]),
        updated_at=float(row[6]),
        revision=int(row[7]),
    )


def _load_draft(con: sqlite3.Connection, uuid: str, kind: Kind, name: str) -> Draft | None:
    row = con.execute(
        f"SELECT {_DRAFT_COLS} FROM outbox_draft WHERE uuid=? AND kind=? AND name=?",
        (uuid, kind, name),
    ).fetchone()
    return None if row is None else _draft_from_row(row)


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


def _insert_proposal(
    con: sqlite3.Connection,
    uuid: str,
    kind: Kind,
    slug: str,
    build: Builder,
    *,
    now: float,
    cap: int,
) -> Proposal:
    """Number, build and insert one proposal inside the caller's write transaction."""
    (pending,) = con.execute(
        "SELECT COUNT(*) FROM outbox_proposal WHERE uuid=? AND kind=?", (uuid, kind)
    ).fetchone()
    if pending >= cap:
        raise ProposalCapError(f"{kind} outbox already has the cap of {cap} pending proposals")
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
        "INSERT INTO outbox_body(uuid, kind, proposal_name, body_name, text) VALUES(?,?,?,?,?)",
        [(uuid, kind, name, body_name, text) for body_name, text in bodies.items()],
    )
    return Proposal(uuid, kind, name, seq, manifest, now, 1, dict(bodies))


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
            return _insert_proposal(con, uuid, kind, slug, build, now=now, cap=cap)

    def list_proposals(self, uuid: str, kind: Kind | None = None) -> list[Proposal]:
        """This container's pending proposals, by kind and number, with bodies."""
        with self._store.transaction(write=False) as con:
            return _select(con, uuid, None if kind is None else _kind(kind), None)

    def get_proposal(self, uuid: str, kind: Kind, name: str) -> Proposal | None:
        with self._store.transaction(write=False) as con:
            found = _select(con, uuid, _kind(kind), name)
        return found[0] if found else None

    # -- draft batches -------------------------------------------------
    # Every draft write is one BEGIN IMMEDIATE transaction that reads the row,
    # derives from it and writes it back, so concurrent appends to one draft
    # serialise on the database write lock and none is lost.

    def draft_append(
        self,
        uuid: str,
        kind: Kind,
        name: str,
        action: dict[str, Any],
        bodies: Mapping[str, str],
        *,
        now: float,
        prepare: DraftPrepare | None = None,
        validate: DraftValidate | None = None,
        max_drafts: int = MAX_DRAFTS,
    ) -> Draft:
        """Append one action (and its new bodies) to the draft, creating it."""
        kind = _kind(kind)
        with self._store.transaction() as con:
            current = _load_draft(con, uuid, kind, name)
            if current is None:
                (open_drafts,) = con.execute(
                    "SELECT COUNT(*) FROM outbox_draft WHERE uuid=? AND kind=?", (uuid, kind)
                ).fetchone()
                if open_drafts >= max_drafts:
                    raise DraftCapError(
                        f"{kind} outbox already has the cap of {max_drafts} open drafts"
                    )
                manifest: dict[str, Any] = {"version": 1, "actions": []}
                existing: dict[str, str] = {}
            else:
                manifest = json.loads(current.manifest)
                existing = dict(current.bodies)
            if prepare is not None:
                action = prepare(manifest, existing, action)
            manifest = {**manifest, "actions": [*manifest["actions"], action]}
            combined = {**existing, **bodies}
            if validate is not None:
                validate(manifest, combined)
            text = json.dumps(manifest, ensure_ascii=False)
            if current is None:
                con.execute(
                    f"INSERT INTO outbox_draft({_DRAFT_COLS}) VALUES(?,?,?,?,?,?,?,1)",
                    (uuid, kind, name, text, json.dumps(combined), now, now),
                )
                revision = 1
            else:
                revision = current.revision + 1
                con.execute(
                    "UPDATE outbox_draft SET manifest=?, bodies=?, updated_at=?, revision=? "
                    "WHERE uuid=? AND kind=? AND name=?",
                    (text, json.dumps(combined), now, revision, uuid, kind, name),
                )
            created = now if current is None else current.created_at
        return Draft(uuid, kind, name, text, combined, created, now, revision)

    def get_draft(self, uuid: str, kind: Kind, name: str) -> Draft | None:
        with self._store.transaction(write=False) as con:
            return _load_draft(con, uuid, _kind(kind), name)

    def list_drafts(self, uuid: str, kind: Kind | None = None) -> list[Draft]:
        sql = f"SELECT {_DRAFT_COLS} FROM outbox_draft WHERE uuid=?"
        params: list[object] = [uuid]
        if kind is not None:
            sql += " AND kind=?"
            params.append(_kind(kind))
        with self._store.transaction(write=False) as con:
            rows = con.execute(sql + " ORDER BY kind, name", params).fetchall()
        return [_draft_from_row(row) for row in rows]

    def discard_draft(self, uuid: str, kind: Kind, name: str) -> None:
        """Remove the draft; `DraftNotFoundError` if there is none."""
        with self._store.transaction() as con:
            removed = con.execute(
                "DELETE FROM outbox_draft WHERE uuid=? AND kind=? AND name=?",
                (uuid, _kind(kind), name),
            ).rowcount
        if not removed:
            raise DraftNotFoundError(name)

    def submit_draft(
        self,
        uuid: str,
        kind: Kind,
        name: str,
        slug: str,
        build: DraftBuilder,
        *,
        now: float,
        cap: int = MAX_PENDING,
    ) -> Proposal:
        """Turn the draft into a pending proposal and delete it, in one transaction.

        A refusal (`DraftNotFoundError`, `ProposalCapError`, or whatever `build`
        raises) leaves the draft untouched.
        """
        kind = _kind(kind)
        with self._store.transaction() as con:
            draft = _load_draft(con, uuid, kind, name)
            if draft is None:
                raise DraftNotFoundError(name)
            found = draft

            def builder(stem: str) -> tuple[str, Mapping[str, str]]:
                return build(stem, found.manifest, found.bodies)

            proposal = _insert_proposal(con, uuid, kind, slug, builder, now=now, cap=cap)
            con.execute(
                "DELETE FROM outbox_draft WHERE uuid=? AND kind=? AND name=?", (uuid, kind, name)
            )
        return proposal

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
