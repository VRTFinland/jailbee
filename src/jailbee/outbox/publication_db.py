"""Publication state in the host outbox store: claims, journals and sent records.

Sibling of `jailbee.outbox.store_db` (proposals); this module owns every query
on ``outbox_journal``, ``outbox_journal_action`` and ``outbox_sent``, which the
outbox migration in `jailbee.orchestration.store` created.

Crash semantics. Publishing one action is two transactions around the GitHub
mutation: `PublicationStore.mark_prepared` commits a ``prepared`` row first,
then `PublicationStore.settle` commits the outcome, the sent record and — when
that outcome completes the proposal — the proposal's removal and the journal's
archiving, all in one transaction. A crash between the two leaves ``prepared``,
which every read reports as ``uncertain`` (`load`); a human resolves it. An
active journal holding an uncertain or prepared action is never archived.

Single flight. A publication claim is one row in the broker's ``state`` table
(``outbox.claim:<uuid>:<kind>:<name>``) naming the owning process. Claiming a
proposal another live process holds raises `ClaimBusyError`; a claim whose
owner is gone is taken over. Every write below re-checks its claim inside its
own transaction, so a taken-over claim can never interleave writes.

Host side only (broker and host commands); no Incus call.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jailbee.orchestration.store import StoreError, store_path
from jailbee.outbox.store_db import Kind
from jailbee.outbox_io import (
    JournalAction,
    JournalError,
    proposal_digest,
    safe_mutation_detail,
)

if TYPE_CHECKING:
    from jailbee.orchestration.store import BrokerStore

_BUSY_TIMEOUT_S = 5.0
_CLAIM_PREFIX = "outbox.claim:"
RECOVERED_DETAIL = "a previous run prepared this action; its outcome is uncertain"

SentState = Literal["applied", "uncertain"]


class ClaimBusyError(JournalError):
    """Another live run is publishing (or resolving) this proposal."""


@dataclass(frozen=True)
class Claim:
    """A held publication claim; pass it to every write of that proposal."""

    uuid: str
    kind: Kind
    name: str
    token: str


@dataclass(frozen=True)
class Journal:
    """The active journal of one proposal, ``prepared`` recovered as ``uncertain``."""

    uuid: str
    kind: Kind
    name: str
    digest: str
    container_full: str
    action_count: int
    actions: tuple[JournalAction, ...]

    @property
    def uncertain(self) -> bool:
        return any(action.state == "uncertain" for action in self.actions)

    @property
    def complete(self) -> bool:
        return (
            self.action_count > 0
            and len(self.actions) == self.action_count
            and all(action.state == "applied" for action in self.actions)
        )


@dataclass(frozen=True)
class SentEntry:
    """One sent record as written: what an action sent and where it came from."""

    uuid: str
    container_prefix: str
    container: str  # the full instance name
    publisher: str
    outbox: str  # issue, review or source
    manifest: str
    digest: str
    index: int
    kind: str
    repo: str
    target: int | None
    url: str | None
    title: str | None
    content: str


@dataclass(frozen=True)
class SentRecord:
    """One stored sent record, as read back."""

    id: int
    sent_at: float
    uuid: str
    container_prefix: str
    container: str
    publisher: str
    state: str
    outbox: str
    manifest: str
    index: int
    kind: str
    repo: str
    target: int | None
    url: str | None
    title: str | None
    content: str


@dataclass(frozen=True)
class Settled:
    """What `settle` committed: the journal after it and whether the proposal went."""

    journal: Journal
    finalized: bool
    changed: bool = False  # complete, but the stored proposal no longer matched


# -- process liveness ------------------------------------------------------


def _start_time(pid: int) -> str | None:
    """The kernel start time of ``pid`` (Linux), so a reused pid is not mistaken for it."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = raw.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def _alive(pid: object, start: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    current = _start_time(pid)
    return current is None or start is None or current == start


_SELF_START = _start_time(os.getpid())


def _claim_key(uuid: str, kind: str, name: str) -> str:
    return f"{_CLAIM_PREFIX}{uuid}:{kind}:{name}"


# -- sent-record kind mapping ----------------------------------------------
#
# ``outbox_sent.outbox`` is issue or pr. The PR family keeps its sub-outbox
# (review / source) in ``action_kind`` as ``<outbox>:<kind>``.


def _stored_kind(outbox: str, kind: str) -> tuple[str, str]:
    if outbox == "issue":
        return "issue", kind
    return "pr", f"{outbox}:{kind}"


def _loaded_kind(outbox: str, action_kind: str) -> tuple[str, str]:
    if outbox == "issue":
        return "issue", action_kind
    sub, sep, kind = action_kind.partition(":")
    return (sub, kind) if sep else ("pr", action_kind)


def sent_dedup_key(uuid: str, outbox: str, manifest: str, digest: str, index: int) -> str:
    """One action of one exact proposal of one instance; a retried publish updates it."""
    return f"{uuid}|{outbox}|{manifest}|{digest}|{index}"


_SENT_COLS = (
    "id, sent_at, uuid, container_prefix, container, publisher, state, outbox, "
    "manifest, action_index, action_kind, repo, target, url, title, content"
)


def _sent_row(row: Sequence[object]) -> SentRecord:
    outbox, kind = _loaded_kind(str(row[7]), str(row[10]))
    target = row[12]
    return SentRecord(
        id=int(str(row[0])),
        sent_at=float(str(row[1])),
        uuid=str(row[2]),
        container_prefix=str(row[3]),
        container=str(row[4]),
        publisher=str(row[5]),
        state=str(row[6]),
        outbox=outbox,
        manifest=str(row[8]),
        index=int(str(row[9])),
        kind=kind,
        repo=str(row[11]),
        target=None if target is None else int(str(target)),
        url=None if row[13] is None else str(row[13]),
        title=None if row[14] is None else str(row[14]),
        content=str(row[15]),
    )


def upsert_sent(con: sqlite3.Connection, entry: SentEntry, state: SentState, now: float) -> None:
    """Insert or update one sent record; an applied record is never downgraded."""
    key = sent_dedup_key(entry.uuid, entry.outbox, entry.manifest, entry.digest, entry.index)
    outbox, kind = _stored_kind(entry.outbox, entry.kind)
    existing = con.execute(
        "SELECT state, url, target, title, content FROM outbox_sent WHERE dedup_key=?", (key,)
    ).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO outbox_sent(dedup_key, sent_at, uuid, container_prefix, container, "
            "publisher, state, outbox, manifest, action_index, action_kind, repo, target, url, "
            "title, content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                now,
                entry.uuid,
                entry.container_prefix,
                entry.container,
                entry.publisher,
                state,
                outbox,
                entry.manifest,
                entry.index,
                kind,
                entry.repo,
                entry.target,
                entry.url,
                entry.title,
                entry.content,
            ),
        )
        return
    old_state, old_url, old_target, old_title, old_content = existing
    promote = old_state == "uncertain" and state == "applied"
    con.execute(
        "UPDATE outbox_sent SET state=?, url=?, target=?, title=?, content=?"
        + (", publisher=?, sent_at=?" if promote else "")
        + " WHERE dedup_key=?",
        (
            "applied" if old_state == "applied" else state,
            entry.url or old_url,
            entry.target if entry.target is not None else old_target,
            entry.title or old_title,
            entry.content or old_content,
            *((entry.publisher, now) if promote else ()),
            key,
        ),
    )


def mark_sent_applied(
    con: sqlite3.Connection,
    *,
    uuid: str,
    outbox: str,
    manifest: str,
    digest: str,
    index: int,
    url: str | None,
    target: int | None,
) -> None:
    key = sent_dedup_key(uuid, outbox, manifest, digest, index)
    con.execute(
        "UPDATE outbox_sent SET state='applied', url=COALESCE(?, url), "
        "target=COALESCE(?, target) WHERE dedup_key=?",
        (url or None, target, key),
    )


def forget_sent(
    con: sqlite3.Connection, *, uuid: str, outbox: str, manifest: str, digest: str, index: int
) -> None:
    """Delete an `uncertain` record of a dropped action; applied records stay."""
    key = sent_dedup_key(uuid, outbox, manifest, digest, index)
    con.execute("DELETE FROM outbox_sent WHERE dedup_key=? AND state='uncertain'", (key,))


# -- journal rows ------------------------------------------------------------


def _actions(
    con: sqlite3.Connection, uuid: str, kind: str, name: str, digest: str
) -> tuple[JournalAction, ...]:
    rows = con.execute(
        "SELECT idx, state, repo, url, issue, detail FROM outbox_journal_action "
        "WHERE uuid=? AND kind=? AND proposal_name=? AND digest=? ORDER BY idx",
        (uuid, kind, name, digest),
    ).fetchall()
    result = []
    for idx, state, repo, url, issue, detail in rows:
        if state not in ("prepared", "applied", "uncertain"):
            raise JournalError(f"{name}: invalid journal action state")
        result.append(
            JournalAction(
                index=int(idx),
                state=state,
                repo=str(repo),
                url=None if url is None else str(url),
                issue=None if issue is None else int(issue),
                detail=None if detail is None else str(detail),
            )
        )
    return tuple(result)


def _recover(action: JournalAction) -> JournalAction:
    if action.state != "prepared":
        return action
    return replace(action, state="uncertain", detail=RECOVERED_DETAIL)


def _active(
    con: sqlite3.Connection, uuid: str, kind: str, name: str, *, recover: bool = True
) -> Journal | None:
    rows = con.execute(
        "SELECT digest, container_full, action_count FROM outbox_journal "
        "WHERE uuid=? AND kind=? AND proposal_name=? AND archived_at IS NULL",
        (uuid, kind, name),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise JournalError(f"{name}: more than one active publication journal")
    digest, full, count = rows[0]
    actions = _actions(con, uuid, kind, name, str(digest))
    if recover:
        actions = tuple(_recover(a) for a in actions)
    return Journal(
        uuid,
        "issue" if kind == "issue" else "pr",
        name,
        str(digest),
        str(full),
        int(count),
        actions,
    )


def _active_in(
    con: sqlite3.Connection, uuid: str, kind: Kind | None
) -> dict[tuple[str, str], Journal]:
    sql = "SELECT kind, proposal_name FROM outbox_journal WHERE uuid=? AND archived_at IS NULL"
    params: list[object] = [uuid]
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    result = {}
    for k, name in con.execute(sql, params).fetchall():
        journal = _active(con, uuid, str(k), str(name))
        if journal is not None:
            result[(str(k), str(name))] = journal
    return result


def _stored_digest(con: sqlite3.Connection, uuid: str, kind: str, name: str) -> str | None:
    row = con.execute(
        "SELECT manifest FROM outbox_proposal WHERE uuid=? AND kind=? AND name=?",
        (uuid, kind, name),
    ).fetchone()
    if row is None:
        return None
    bodies = dict(
        con.execute(
            "SELECT body_name, text FROM outbox_body WHERE uuid=? AND kind=? AND proposal_name=?",
            (uuid, kind, name),
        ).fetchall()
    )
    return proposal_digest(name, str(row[0]), {str(k): str(v) for k, v in bodies.items()})


def _remove_proposal(con: sqlite3.Connection, uuid: str, kind: str, name: str) -> None:
    con.execute(
        "DELETE FROM outbox_body WHERE uuid=? AND kind=? AND proposal_name=?", (uuid, kind, name)
    )
    con.execute("DELETE FROM outbox_proposal WHERE uuid=? AND kind=? AND name=?", (uuid, kind, name))


def _archive(
    con: sqlite3.Connection, uuid: str, kind: str, name: str, digest: str, now: float
) -> None:
    con.execute(
        "UPDATE outbox_journal SET archived_at=?, revision=revision+1 "
        "WHERE uuid=? AND kind=? AND proposal_name=? AND digest=? AND archived_at IS NULL",
        (now, uuid, kind, name, digest),
    )


def _bump(con: sqlite3.Connection, uuid: str, kind: str, name: str, digest: str) -> None:
    con.execute(
        "UPDATE outbox_journal SET revision=revision+1 "
        "WHERE uuid=? AND kind=? AND proposal_name=? AND digest=?",
        (uuid, kind, name, digest),
    )


# -- the store -----------------------------------------------------------------


class PublicationStore:
    """Claims, journals and sent records; each method is one transaction."""

    _held = threading.local()

    def __init__(self, store: BrokerStore) -> None:
        self._store = store

    # claims

    def _held_claims(self) -> dict[tuple[str, str, str, str], Claim]:
        held: dict[tuple[str, str, str, str], Claim] | None = getattr(self._held, "claims", None)
        if held is None:
            held = {}
            self._held.claims = held
        return held

    @contextlib.contextmanager
    def claim(self, uuid: str, kind: Kind, name: str) -> Iterator[Claim]:
        """Hold the single-flight claim on one proposal (reentrant per thread)."""
        slot = (str(self._store.path), uuid, kind, name)
        held = self._held_claims()
        if slot in held:
            yield held[slot]
            return
        token = secrets.token_hex(16)
        key = _claim_key(uuid, kind, name)
        value = json.dumps({"token": token, "pid": os.getpid(), "start": _SELF_START})
        with self._store.transaction() as con:
            row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            if row is not None:
                try:
                    owner = json.loads(row[0])
                except ValueError:
                    owner = {}
                if not isinstance(owner, dict):
                    owner = {}
                if _alive(owner.get("pid"), owner.get("start")):
                    raise ClaimBusyError(
                        f"{name}: another run (pid {owner.get('pid')}) is publishing this "
                        "proposal; retry after it finishes"
                    )
            con.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        claim = Claim(uuid, kind, name, token)
        held[slot] = claim
        try:
            yield claim
        finally:
            del held[slot]
            with contextlib.suppress(StoreError):
                with self._store.transaction() as con:
                    con.execute(
                        "DELETE FROM state WHERE key=? AND json_extract(value, '$.token')=?",
                        (key, token),
                    )

    @staticmethod
    def _check(con: sqlite3.Connection, claim: Claim) -> None:
        row = con.execute(
            "SELECT json_extract(value, '$.token') FROM state WHERE key=?",
            (_claim_key(claim.uuid, claim.kind, claim.name),),
        ).fetchone()
        if row is None or row[0] != claim.token:
            raise ClaimBusyError(f"{claim.name}: the publication claim was lost; nothing recorded")

    # reads

    def load(self, uuid: str, kind: Kind, name: str) -> Journal | None:
        with self._store.transaction(write=False) as con:
            return _active(con, uuid, kind, name)

    # journal writes

    def begin(
        self, claim: Claim, *, digest: str, action_count: int, container_full: str, now: float
    ) -> Journal:
        """The proposal's active journal for ``digest``, created when absent.

        A journal of another digest that recorded nothing is replaced; one with
        recorded progress refuses: the proposal changed after publication began.
        """
        with self._store.transaction() as con:
            self._check(con, claim)
            current = _active(con, claim.uuid, claim.kind, claim.name)
            if current is not None and current.digest != digest:
                if current.actions:
                    raise JournalError(f"{claim.name}: proposal changed after recorded progress")
                con.execute(
                    "DELETE FROM outbox_journal WHERE uuid=? AND kind=? AND proposal_name=? "
                    "AND digest=?",
                    (claim.uuid, claim.kind, claim.name, current.digest),
                )
                current = None
            if current is not None:
                if current.action_count != action_count:
                    raise JournalError(f"{claim.name}: journal action count differs from proposal")
                return current
            if con.execute(
                "SELECT 1 FROM outbox_journal WHERE uuid=? AND kind=? AND proposal_name=? "
                "AND digest=?",
                (claim.uuid, claim.kind, claim.name, digest),
            ).fetchone():
                raise JournalError(f"{claim.name}: this proposal was already published and archived")
            con.execute(
                "INSERT INTO outbox_journal(uuid, kind, proposal_name, digest, container_full, "
                "action_count, created_at) VALUES(?,?,?,?,?,?,?)",
                (claim.uuid, claim.kind, claim.name, digest, container_full, action_count, now),
            )
            return Journal(
                claim.uuid, claim.kind, claim.name, digest, container_full, action_count, ()
            )

    def _required(self, con: sqlite3.Connection, claim: Claim, digest: str) -> Journal:
        journal = _active(con, claim.uuid, claim.kind, claim.name, recover=False)
        if journal is None or journal.digest != digest:
            raise JournalError(f"{claim.name}: no active journal for this proposal")
        return journal

    @staticmethod
    def _action(journal: Journal, index: int) -> JournalAction | None:
        if type(index) is not int or not 0 <= index < journal.action_count:
            raise JournalError("journal action index is out of bounds")
        return next((a for a in journal.actions if a.index == index), None)

    def mark_prepared(
        self, claim: Claim, digest: str, index: int, *, repo: str, now: float
    ) -> None:
        """Commit ``prepared`` before the GitHub mutation is dispatched."""
        if not repo:
            raise JournalError("journal action repo must be non-empty")
        with self._store.transaction() as con:
            self._check(con, claim)
            journal = self._required(con, claim, digest)
            if self._action(journal, index) is not None:
                raise JournalError("illegal journal transition to prepared")
            con.execute(
                "INSERT INTO outbox_journal_action(uuid, kind, proposal_name, digest, idx, state, "
                "repo, at) VALUES(?,?,?,?,?,'prepared',?,?)",
                (claim.uuid, claim.kind, claim.name, digest, index, repo, now),
            )
            _bump(con, claim.uuid, claim.kind, claim.name, digest)

    def clear_prepared(self, claim: Claim, digest: str, index: int) -> None:
        """Forget a ``prepared`` action whose mutation was definitely not made."""
        with self._store.transaction() as con:
            self._check(con, claim)
            journal = self._required(con, claim, digest)
            current = self._action(journal, index)
            if current is None or current.state != "prepared":
                raise JournalError("illegal journal transition while clearing prepared action")
            self._delete_action(con, claim, digest, index)

    @staticmethod
    def _delete_action(con: sqlite3.Connection, claim: Claim, digest: str, index: int) -> None:
        con.execute(
            "DELETE FROM outbox_journal_action WHERE uuid=? AND kind=? AND proposal_name=? "
            "AND digest=? AND idx=?",
            (claim.uuid, claim.kind, claim.name, digest, index),
        )
        _bump(con, claim.uuid, claim.kind, claim.name, digest)

    def _set_action(
        self, con: sqlite3.Connection, claim: Claim, digest: str, action: JournalAction, now: float
    ) -> None:
        con.execute(
            "UPDATE outbox_journal_action SET state=?, url=?, issue=?, detail=?, at=? "
            "WHERE uuid=? AND kind=? AND proposal_name=? AND digest=? AND idx=?",
            (
                action.state,
                action.url,
                action.issue,
                action.detail,
                now,
                claim.uuid,
                claim.kind,
                claim.name,
                digest,
                action.index,
            ),
        )
        _bump(con, claim.uuid, claim.kind, claim.name, digest)

    def _finalize(
        self, con: sqlite3.Connection, claim: Claim, digest: str, now: float
    ) -> tuple[bool, bool]:
        """(finalized, changed): remove a completed proposal and archive its journal."""
        journal = _active(con, claim.uuid, claim.kind, claim.name, recover=False)
        if journal is None or journal.digest != digest or not journal.complete:
            return False, False
        if _stored_digest(con, claim.uuid, claim.kind, claim.name) != digest:
            return False, True
        _remove_proposal(con, claim.uuid, claim.kind, claim.name)
        _archive(con, claim.uuid, claim.kind, claim.name, digest, now)
        return True, False

    def settle(
        self,
        claim: Claim,
        digest: str,
        index: int,
        *,
        outcome: Literal["applied", "uncertain"],
        url: str | None = None,
        issue: int | None = None,
        detail: str | None = None,
        sent: SentEntry | None = None,
        now: float,
    ) -> Settled:
        """Record one mutation's outcome, its sent record and — when that completes the
        proposal — the proposal's removal, in one transaction."""
        with self._store.transaction() as con:
            self._check(con, claim)
            journal = self._required(con, claim, digest)
            current = self._action(journal, index)
            if current is None or current.state != "prepared":
                raise JournalError(f"illegal journal transition to {outcome}")
            if outcome == "applied":
                if not url:
                    raise JournalError("an applied journal action needs its receipt URL")
                action = replace(current, state="applied", url=url, issue=issue, detail=None)
            else:
                action = replace(
                    current,
                    state="uncertain",
                    url=None,
                    issue=None,
                    detail=safe_mutation_detail(detail or ""),
                )
            self._set_action(con, claim, digest, action, now)
            if sent is not None:
                if outcome == "applied":
                    sent = replace(sent, url=sent.url or url)
                upsert_sent(con, sent, outcome, now)
            finalized, changed = (
                self._finalize(con, claim, digest, now) if outcome == "applied" else (False, False)
            )
            after = _active(con, claim.uuid, claim.kind, claim.name)
            if after is None:
                actions = tuple(
                    action if a.index == index else a for a in journal.actions
                )
                after = replace(journal, actions=actions)
            return Settled(after, finalized, changed)

    def finalize(self, claim: Claim, digest: str, *, now: float) -> Settled:
        """Remove a proposal whose every action is already applied (a resumed run)."""
        with self._store.transaction() as con:
            self._check(con, claim)
            journal = self._required(con, claim, digest)
            finalized, changed = self._finalize(con, claim, digest, now)
            return Settled(replace(journal, actions=tuple(map(_recover, journal.actions))),
                           finalized, changed)

    def resolve(
        self,
        claim: Claim,
        digest: str,
        index: int,
        *,
        applied: tuple[str, int | None] | None,
        sent_outbox: str,
        now: float,
    ) -> Settled:
        """A human's resolution of one uncertain (or recovered prepared) action.

        ``applied`` is ``(url, issue)`` for "it landed" and None for "retry". An
        applied resolution marks the sent record applied and, when it completes
        the proposal, removes it — one transaction.
        """
        with self._store.transaction() as con:
            self._check(con, claim)
            journal = self._required(con, claim, digest)
            current = self._action(journal, index)
            if current is None or current.state not in ("prepared", "uncertain"):
                raise JournalError("only an uncertain action can be reconciled")
            if applied is None:
                self._delete_action(con, claim, digest, index)
                finalized, changed = False, False
            else:
                url, issue = applied
                action = replace(current, state="applied", url=url, issue=issue, detail=None)
                self._set_action(con, claim, digest, action, now)
                mark_sent_applied(
                    con,
                    uuid=claim.uuid,
                    outbox=sent_outbox,
                    manifest=claim.name,
                    digest=digest,
                    index=index,
                    url=url,
                    target=issue,
                )
                finalized, changed = self._finalize(con, claim, digest, now)
            after = _active(con, claim.uuid, claim.kind, claim.name) or replace(
                journal, actions=()
            )
            return Settled(after, finalized, changed)

    def archive(self, uuid: str, kind: Kind, name: str, *, now: float) -> bool:
        """Archive the proposal's active journal; refused while any outcome is unknown.

        Returns whether a journal was archived (False when there is none).
        """
        with self._store.transaction() as con:
            journal = _active(con, uuid, kind, name, recover=False)
            if journal is None:
                return False
            if any(a.state in ("prepared", "uncertain") for a in journal.actions):
                raise JournalError(f"{name}: cannot archive a journal containing uncertain progress")
            _archive(con, uuid, kind, name, journal.digest, now)
            return True

    # sent records (host writers outside a publication transaction)

    def record_sent(self, entry: SentEntry, state: SentState, *, now: float) -> None:
        with self._store.transaction() as con:
            upsert_sent(con, entry, state, now)

    def mark_sent(
        self,
        *,
        uuid: str,
        outbox: str,
        manifest: str,
        digest: str,
        index: int,
        url: str | None,
        target: int | None,
    ) -> None:
        with self._store.transaction() as con:
            mark_sent_applied(
                con,
                uuid=uuid,
                outbox=outbox,
                manifest=manifest,
                digest=digest,
                index=index,
                url=url,
                target=target,
            )

    def forget_sent(
        self, *, uuid: str, outbox: str, manifest: str, digest: str, index: int
    ) -> None:
        with self._store.transaction() as con:
            forget_sent(con, uuid=uuid, outbox=outbox, manifest=manifest, digest=digest, index=index)

    def clear_sent(self, *, container_prefix: str, before: float | None) -> int:
        sql = "DELETE FROM outbox_sent WHERE container_prefix=?"
        params: list[object] = [container_prefix]
        if before is not None:
            sql += " AND sent_at < ?"
            params.append(before)
        with self._store.transaction() as con:
            return int(con.execute(sql, params).rowcount)

    def sent_for(
        self, uuid: str, *, outbox: str | None = None, limit: int | None = None
    ) -> list[SentRecord]:
        """One instance's sent records, newest first (the container's own view)."""
        with self._store.transaction(write=False) as con:
            return _select_sent(con, uuid=uuid, outbox=outbox, limit=limit)


def _select_sent(
    con: sqlite3.Connection,
    *,
    container_prefix: str | None = None,
    uuid: str | None = None,
    outbox: str | None = None,
    record_id: int | None = None,
    limit: int | None = None,
) -> list[SentRecord]:
    sql = f"SELECT {_SENT_COLS} FROM outbox_sent WHERE 1=1"
    params: list[object] = []
    if container_prefix is not None:
        sql += " AND container_prefix=?"
        params.append(container_prefix)
    if uuid is not None:
        sql += " AND uuid=?"
        params.append(uuid)
    if outbox is not None:
        sql += " AND outbox=?"
        params.append("issue" if outbox == "issue" else "pr")
    if record_id is not None:
        sql += " AND id=?"
        params.append(record_id)
    sql += " ORDER BY sent_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_sent_row(row) for row in con.execute(sql, params).fetchall()]


# -- read-only host reads -------------------------------------------------------


@contextlib.contextmanager
def _read_only(root: Path) -> Iterator[sqlite3.Connection | None]:
    """A read-only snapshot connection, or None when there is no store yet."""
    path = store_path(root)
    if not path.exists():
        yield None
        return
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)
        try:
            con.execute("BEGIN")
            yield con
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            raise _NoTable from exc
        raise StoreError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise StoreError(str(exc)) from exc


class _NoTable(Exception):
    pass


def read_journals(root: Path, uuid: str, kind: Kind | None = None) -> dict[tuple[str, str], Journal]:
    """Active journals of one instance keyed by (kind, name); never writes."""
    try:
        with _read_only(root) as con:
            return {} if con is None else _active_in(con, uuid, kind)
    except _NoTable:
        return {}


def read_sent(
    root: Path,
    *,
    container_prefix: str | None = None,
    uuid: str | None = None,
    record_id: int | None = None,
    limit: int | None = None,
) -> list[SentRecord]:
    """Sent records, newest first; never writes."""
    try:
        with _read_only(root) as con:
            if con is None:
                return []
            return _select_sent(
                con, container_prefix=container_prefix, uuid=uuid, record_id=record_id, limit=limit
            )
    except _NoTable:
        return []


def sent_counts(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Sent record counts (by uuid, by container prefix); never writes."""
    try:
        with _read_only(root) as con:
            if con is None:
                return {}, {}
            by_uuid = con.execute("SELECT uuid, COUNT(*) FROM outbox_sent GROUP BY uuid").fetchall()
            by_prefix = con.execute(
                "SELECT container_prefix, COUNT(*) FROM outbox_sent GROUP BY container_prefix"
            ).fetchall()
            return (
                {str(u): int(n) for u, n in by_uuid},
                {str(p): int(n) for p, n in by_prefix},
            )
    except _NoTable:
        return {}, {}


def default_root() -> Path:
    """The broker's store directory on this host."""
    from jailbee.db import state_dir

    return state_dir() / "orchestration"


def open_publications(root: Path | None = None) -> PublicationStore:
    from jailbee.orchestration.store import BrokerStore

    return PublicationStore(BrokerStore(store_path(root or default_root())))
