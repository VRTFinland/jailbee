"""Host-side SQLite message store for the orchestration broker.

Stdlib only. Every method opens a short-lived connection (open, run one
transaction, close), so broker threads and separate worker processes may write
concurrently through SQLite's own locking (WAL + ``busy_timeout``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_BUSY_TIMEOUT_MS = 5000
_log = logging.getLogger(__name__)

# (path, migration count) pairs already migrated by this process. Only a cache
# that saves the migration transaction on every new `BrokerStore` instance (one
# is made per delivery); it holds no behavioural state and is dropped for a path
# whose file has vanished. Other processes migrate for themselves.
_MIGRATED: set[tuple[str, int]] = set()
_MIGRATED_LOCK = threading.Lock()

# Ordered schema migrations. Append new scripts; never edit an applied one.
_MIGRATIONS: list[str] = [
    """
    CREATE TABLE items (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_uuid TEXT NOT NULL,
        recipient_full TEXT NOT NULL,
        id TEXT NOT NULL,
        type TEXT NOT NULL,
        kind_or_state TEXT,
        ref TEXT,
        from_name TEXT,
        created_at REAL NOT NULL,
        item_json TEXT NOT NULL,
        acked_at REAL,
        UNIQUE(recipient_uuid, id)
    );
    CREATE INDEX items_unacked ON items(recipient_uuid, acked_at, seq);
    CREATE TABLE requests (
        sender_uuid TEXT NOT NULL,
        id TEXT NOT NULL,
        digest TEXT,
        state TEXT NOT NULL,
        detail TEXT NOT NULL,
        item_json TEXT,
        at REAL NOT NULL,
        PRIMARY KEY(sender_uuid, id)
    );
    CREATE TABLE recipients (
        full TEXT PRIMARY KEY,
        uuid TEXT NOT NULL,
        attached_at REAL NOT NULL
    );
    CREATE TABLE digest (
        uuid TEXT PRIMARY KEY,
        at REAL NOT NULL
    );
    """,
    """
    CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    """
    CREATE TABLE journal (
        container_key TEXT NOT NULL, digest TEXT NOT NULL,
        container_full TEXT NOT NULL, manifest_id TEXT NOT NULL,
        outcome TEXT NOT NULL, at TEXT NOT NULL,
        PRIMARY KEY(container_key, digest)
    );
    """,
]


class StoreError(OSError):
    """The broker store failed (corrupt file, locked too long, ...)."""


class PendingImportError(StoreError):
    """A legacy document import is still pending; the stored value is not authoritative."""


@dataclass(frozen=True)
class StoredItem:
    seq: int
    recipient_uuid: str
    recipient_full: str
    id: str
    type: str
    kind_or_state: str | None
    ref: str | None
    from_name: str | None
    created_at: float
    item: dict[str, object]
    acked_at: float | None


@dataclass(frozen=True)
class ItemMeta:
    """A message row without its JSON body (all the wake rules read)."""

    id: str
    type: str
    kind_or_state: str | None
    from_name: str | None
    created_at: float
    acked_at: float | None


@dataclass(frozen=True)
class RecordedRequest:
    state: str
    detail: str
    item: dict[str, object] | None


_ITEM_COLS = (
    "seq, recipient_uuid, recipient_full, id, type, kind_or_state, ref, "
    "from_name, created_at, item_json, acked_at"
)


def store_path(root: Path) -> Path:
    return root / "broker.db"


def recipient_uuid(root: Path, full: str) -> str | None:
    """Cheap read-only lookup: never creates, migrates or writes the database.

    Returns None only when the file or the ``recipients`` table is missing (no
    store inbox exists yet). Any other SQLite error (locked, corrupt) raises
    `StoreError`: the answer is unknown, and treating it as "no store" would
    send an item to the file inbox of a store recipient.
    """
    path = store_path(root)
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            row = con.execute("SELECT uuid FROM recipients WHERE full=?", (full,)).fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise StoreError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise StoreError(str(exc)) from exc
    return None if row is None else str(row[0])


def read_items(root: Path, full: str | None = None) -> list[StoredItem]:
    """Every stored item (optionally one recipient's), read-only, in ``seq`` order.

    Never creates, migrates or writes the database. A missing file or table
    reads as no items; any other SQLite error raises `StoreError`.
    """
    path = store_path(root)
    if not path.exists():
        return []
    sql = f"SELECT {_ITEM_COLS} FROM items"
    params: tuple[object, ...] = ()
    if full is not None:
        sql += " WHERE recipient_full=?"
        params = (full,)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            return [_row_item(r) for r in con.execute(sql + " ORDER BY seq", params).fetchall()]
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise StoreError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise StoreError(str(exc)) from exc


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _row_item(row: tuple[Any, ...]) -> StoredItem:
    loaded = json.loads(row[9])
    return StoredItem(
        seq=int(row[0]),
        recipient_uuid=str(row[1]),
        recipient_full=str(row[2]),
        id=str(row[3]),
        type=str(row[4]),
        kind_or_state=_opt_str(row[5]),
        ref=_opt_str(row[6]),
        from_name=_opt_str(row[7]),
        created_at=float(row[8]),
        item=loaded if isinstance(loaded, dict) else {},
        acked_at=None if row[10] is None else float(row[10]),
    )


class BrokerStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextlib.contextmanager
    def _tx(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        key = (str(self.path), len(_MIGRATIONS))
        try:
            if not self.path.exists():
                with _MIGRATED_LOCK:
                    _MIGRATED.discard(key)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(FileExistsError):
                    os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            con = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
            try:
                con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                _enable_wal(con)
                with _MIGRATED_LOCK:
                    migrated = key in _MIGRATED
                if not migrated:
                    self._migrate(con)
                    with _MIGRATED_LOCK:
                        _MIGRATED.add(key)
                con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield con
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        con.execute("ROLLBACK")
                    raise
                con.execute("COMMIT")
            finally:
                con.close()
        except (sqlite3.DatabaseError, OSError) as exc:
            raise StoreError(str(exc)) from exc

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row[0]) if row else 0
            if current > len(_MIGRATIONS):
                raise StoreError("broker.db is newer than this jailbee")
            for n, script in enumerate(_MIGRATIONS[current:], start=current + 1):
                _run_script(con, script)
                con.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(n),),
                )
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")

    # -- state ---------------------------------------------------------

    def kv_get(self, key: str) -> object | None:
        with self._tx(write=False) as con:
            row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def kv_put(self, key: str, value: object) -> None:
        with self._tx() as con:
            con.execute(
                "INSERT INTO state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def kv_delete(self, key: str) -> None:
        with self._tx() as con:
            con.execute("DELETE FROM state WHERE key=?", (key,))

    def kv_keys(self, prefix: str) -> list[str]:
        with self._tx(write=False) as con:
            rows = con.execute(
                "SELECT key FROM state WHERE substr(key, 1, ?) = ? ORDER BY key",
                (len(prefix), prefix),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def import_once(self, name: str, loader: Callable[[sqlite3.Connection], None]) -> bool:
        """Run loader and record its marker in the same write transaction.

        The loader must use the supplied connection, not methods opening another
        transaction. Return whether the loader ran.
        """
        marker = f"imported:{name}"
        with self._tx() as con:
            if con.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone():
                return False
            loader(con)
            con.execute("INSERT INTO meta(key, value) VALUES (?, '1')", (marker,))
        return True

    def journal_seen(self, container_key: str, digest: str) -> bool:
        with self._tx(write=False) as con:
            return (
                con.execute(
                    "SELECT 1 FROM journal WHERE container_key=? AND digest=?",
                    (container_key, digest),
                ).fetchone()
                is not None
            )

    def journal_record(
        self,
        container_key: str,
        digest: str,
        container_full: str,
        manifest_id: str,
        outcome: str,
        at: str,
    ) -> None:
        with self._tx() as con:
            con.execute(
                "INSERT OR IGNORE INTO journal VALUES (?, ?, ?, ?, ?, ?)",
                (container_key, digest, container_full, manifest_id, outcome, at),
            )

    # -- items ---------------------------------------------------------

    def insert_item(
        self,
        recipient_uuid: str,
        recipient_full: str,
        item: Mapping[str, object],
        *,
        now: float,
        acked: bool = False,
        acked_at: float | None = None,
    ) -> bool:
        """Insert unless the id exists; `now` is `created_at`.

        `acked_at` (implies acked) stamps the ack separately, for history
        imported with an older `created_at`.
        """
        ack_time = acked_at if acked_at is not None else (now if acked else None)
        with self._tx() as con:
            return _insert(con, recipient_uuid, recipient_full, item, now, ack_time)

    def insert_for_recipient(self, full: str, item: Mapping[str, object], *, now: float) -> bool:
        """Insert `item` unacked for `full`'s recorded uuid, in one transaction.

        False (nothing inserted) when `full` is not a store recipient, so a
        teardown that removes the recipient row can never strand an item that
        was inserted after its last export.
        """
        with self._tx() as con:
            row = con.execute("SELECT uuid FROM recipients WHERE full=?", (full,)).fetchone()
            if row is None:
                return False
            _insert(con, str(row[0]), full, item, now, None)
            return True

    def _items(self, sql: str, params: tuple[object, ...]) -> list[StoredItem]:
        with self._tx(write=False) as con:
            return [_row_item(r) for r in con.execute(sql, params).fetchall()]

    def unacked(self, uuid: str, *, after: int = 0, limit: int = -1) -> list[StoredItem]:
        """Unacked items oldest first; ``after``/``limit`` page by ``seq``."""
        return self._items(
            f"SELECT {_ITEM_COLS} FROM items "
            "WHERE recipient_uuid=? AND acked_at IS NULL AND seq>? ORDER BY seq LIMIT ?",
            (uuid, after, limit),
        )

    def history(self, uuid: str) -> list[StoredItem]:
        return self._items(
            f"SELECT {_ITEM_COLS} FROM items WHERE recipient_uuid=? ORDER BY seq", (uuid,)
        )

    def history_meta(self, uuid: str) -> list[ItemMeta]:
        """Every message row's metadata, oldest first, without `item_json`."""
        with self._tx(write=False) as con:
            rows = con.execute(
                "SELECT id, type, kind_or_state, from_name, created_at, acked_at FROM items "
                "WHERE recipient_uuid=? AND type='message' ORDER BY seq",
                (uuid,),
            ).fetchall()
        return [
            ItemMeta(
                id=str(r[0]),
                type=str(r[1]),
                kind_or_state=_opt_str(r[2]),
                from_name=_opt_str(r[3]),
                created_at=float(r[4]),
                acked_at=None if r[5] is None else float(r[5]),
            )
            for r in rows
        ]

    def get(self, uuid: str, id: str) -> StoredItem | None:
        rows = self._items(
            f"SELECT {_ITEM_COLS} FROM items WHERE recipient_uuid=? AND id=?", (uuid, id)
        )
        return rows[0] if rows else None

    def ack(self, uuid: str, id: str, *, now: float) -> bool:
        with self._tx() as con:
            con.execute(
                "UPDATE items SET acked_at=? WHERE recipient_uuid=? AND id=? AND acked_at IS NULL",
                (now, uuid, id),
            )
            row = con.execute(
                "SELECT 1 FROM items WHERE recipient_uuid=? AND id=?", (uuid, id)
            ).fetchone()
            return row is not None

    # -- requests ------------------------------------------------------

    def record_request(
        self,
        uuid: str,
        id: str,
        *,
        digest: str | None,
        state: str,
        detail: str,
        item: Mapping[str, object] | None,
        now: float,
    ) -> None:
        with self._tx() as con:
            con.execute(
                "INSERT OR REPLACE INTO requests(sender_uuid, id, digest, state, detail, "
                "item_json, at) VALUES(?,?,?,?,?,?,?)",
                (uuid, id, digest, state, detail, None if item is None else json.dumps(item), now),
            )

    def lookup_request(self, uuid: str, id: str) -> RecordedRequest | None:
        with self._tx(write=False) as con:
            row = con.execute(
                "SELECT state, detail, item_json FROM requests WHERE sender_uuid=? AND id=?",
                (uuid, id),
            ).fetchone()
        if row is None:
            return None
        loaded = None if row[2] is None else json.loads(row[2])
        return RecordedRequest(
            state=str(row[0]),
            detail=str(row[1]),
            item=loaded if isinstance(loaded, dict) else None,
        )

    # -- recipients ----------------------------------------------------

    def set_recipient(self, full: str, uuid: str, *, now: float) -> None:
        with self._tx() as con:
            con.execute(
                "INSERT OR REPLACE INTO recipients(full, uuid, attached_at) VALUES(?,?,?)",
                (full, uuid, now),
            )

    def remove_recipient(self, full: str) -> None:
        with self._tx() as con:
            con.execute("DELETE FROM recipients WHERE full=?", (full,))

    def recipient_uuid(self, full: str) -> str | None:
        with self._tx(write=False) as con:
            row = con.execute("SELECT uuid FROM recipients WHERE full=?", (full,)).fetchone()
        return None if row is None else str(row[0])

    def has_recipient_uuid(self, uuid: str) -> bool:
        with self._tx(write=False) as con:
            row = con.execute("SELECT 1 FROM recipients WHERE uuid=? LIMIT 1", (uuid,)).fetchone()
        return row is not None

    def recipients(self) -> dict[str, str]:
        with self._tx(write=False) as con:
            return {str(f): str(u) for f, u in con.execute("SELECT full, uuid FROM recipients")}

    # -- digest --------------------------------------------------------

    def get_digest_at(self, uuid: str) -> float | None:
        with self._tx(write=False) as con:
            row = con.execute("SELECT at FROM digest WHERE uuid=?", (uuid,)).fetchone()
        return None if row is None else float(row[0])

    def set_digest_at(self, uuid: str, at: float) -> None:
        with self._tx() as con:
            con.execute("INSERT OR REPLACE INTO digest(uuid, at) VALUES(?,?)", (uuid, at))

    # -- purging -------------------------------------------------------

    def known_uuids(self) -> set[str]:
        """Every container uuid any table holds a row for."""
        with self._tx(write=False) as con:
            rows = con.execute(
                "SELECT recipient_uuid FROM items UNION SELECT sender_uuid FROM requests "
                "UNION SELECT uuid FROM digest UNION SELECT uuid FROM recipients"
            ).fetchall()
        return {str(r[0]) for r in rows}

    def purge_uuids(self, gone: Collection[str], *, keep_unacked_after: float) -> int:
        """Delete the rows of containers known to be gone; return the row count.

        Unacked items created at or after ``keep_unacked_after`` stay: an unread
        message is not dropped before its retention ran out, however sure the
        caller is that its recipient is gone.
        """
        uuids = list(gone)
        if not uuids:
            return 0
        removed = 0
        with self._tx() as con:
            con.execute("CREATE TEMP TABLE gone(uuid TEXT PRIMARY KEY)")
            con.executemany("INSERT OR IGNORE INTO gone VALUES(?)", [(u,) for u in uuids])
            cur = con.execute(
                "DELETE FROM items WHERE recipient_uuid IN (SELECT uuid FROM gone) "
                "AND (acked_at IS NOT NULL OR created_at<?)",
                (keep_unacked_after,),
            )
            removed += max(cur.rowcount, 0)
            for table, col in (
                ("requests", "sender_uuid"),
                ("digest", "uuid"),
                ("recipients", "uuid"),
            ):
                cur = con.execute(f"DELETE FROM {table} WHERE {col} IN (SELECT uuid FROM gone)")
                removed += max(cur.rowcount, 0)
            con.execute("DROP TABLE gone")
        return removed

    def purge_acked_older_than(self, uuid: str, cutoff: float) -> int:
        """Delete acked items acked before ``cutoff``; return the item count.

        ``requests`` rows are aged out by their ``at`` regardless of ack.
        """
        with self._tx() as con:
            cur = con.execute(
                "DELETE FROM items WHERE recipient_uuid=? AND acked_at IS NOT NULL AND acked_at<?",
                (uuid, cutoff),
            )
            con.execute("DELETE FROM requests WHERE sender_uuid=? AND at<?", (uuid, cutoff))
            return max(cur.rowcount, 0)


def _insert(
    con: sqlite3.Connection,
    recipient_uuid: str,
    recipient_full: str,
    item: Mapping[str, object],
    now: float,
    ack_time: float | None,
) -> bool:
    item_type = str(item.get("type", ""))
    detail = item.get("state") if item_type == "status" else item.get("kind")
    cur = con.execute(
        "INSERT OR IGNORE INTO items(recipient_uuid, recipient_full, id, type, "
        "kind_or_state, ref, from_name, created_at, item_json, acked_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            recipient_uuid,
            recipient_full,
            str(item["id"]),
            item_type,
            _opt_str(detail),
            _opt_str(item.get("ref")),
            _opt_str(item.get("from")),
            now,
            json.dumps(item),
            ack_time,
        ),
    )
    return cur.rowcount > 0


_STATE_STORES: dict[Path, BrokerStore] = {}
_STATE_STORES_LOCK = threading.Lock()


def open_state_store(root: Path) -> BrokerStore:
    """Return a shared stateless handle; connections remain per operation."""
    path = store_path(root).absolute()
    with _STATE_STORES_LOCK:
        if path not in _STATE_STORES:
            _STATE_STORES[path] = BrokerStore(path)
        return _STATE_STORES[path]


class _LegacyImportRetryError(Exception):
    """A legacy source could not be read or safely quarantined yet."""


def import_json_document(
    root: Path,
    name: str,
    validate: Callable[[object], None],
    *,
    for_write: bool = False,
    strict: bool = False,
) -> BrokerStore:
    """Import a legacy JSON document once; validate raises ValueError on bad shape.

    The document key and marker use the legacy filename. Future document users
    can reuse this helper before either reading or saving that key. Set
    for_write to prevent overwriting state when the legacy import must retry.
    Set strict to raise PendingImportError instead of returning a store whose
    document may be missing only because the legacy import is still pending.
    """
    store = open_state_store(root)
    path = root / name

    def load(con: sqlite3.Connection) -> None:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            validate(document)
        except FileNotFoundError:
            return
        except (ValueError, TypeError, UnicodeError) as exc:
            _log.warning("ignoring corrupt orchestration state %s: %s", path, exc)
            try:
                path.replace(path.with_name(path.name + ".corrupt"))
            except FileNotFoundError:
                pass
            except OSError as rename_exc:
                _log.warning("cannot quarantine orchestration state %s: %s", path, rename_exc)
                raise _LegacyImportRetryError from rename_exc
            return
        except OSError as exc:
            _log.warning("ignoring unreadable orchestration state %s: %s", path, exc)
            raise _LegacyImportRetryError from exc
        con.execute(
            "INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            (name, json.dumps(document)),
        )

    marker = f"imported:{name}"
    pending = f"pending:{name}"
    blocked = False
    with store._tx() as con:
        # A writer retries a pending import once too, so it never waits for a reader.
        if not con.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone():
            try:
                load(con)
            except _LegacyImportRetryError:
                con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, '1')", (pending,))
                blocked = True
            else:
                con.execute("INSERT INTO meta(key, value) VALUES (?, '1')", (marker,))
                con.execute("DELETE FROM meta WHERE key=?", (pending,))
    if blocked:
        if for_write:
            raise StoreError(f"cannot save state while legacy import is pending: {path}")
        if strict:
            raise PendingImportError(f"legacy import is pending: {path}")
        return store
    # TODO(state-db-cleanup): remove migrated files after the compatibility window.
    try:
        path.replace(path.with_name(path.name + ".migrated"))
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("cannot rename migrated orchestration state %s: %s", path, exc)
    return store


def read_state_snapshot(root: Path, key: str) -> tuple[bool, object | None]:
    """Read a state value and import marker together without creating a store."""
    path = store_path(root)
    if not path.exists():
        return False, None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            con.execute("BEGIN")
            row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            marker = con.execute("SELECT 1 FROM meta WHERE key=?", (f"imported:{key}",)).fetchone()
            return bool(row or marker), None if row is None else json.loads(row[0])
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False, None
        raise StoreError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise StoreError(str(exc)) from exc


def _enable_wal(con: sqlite3.Connection) -> None:
    """Switch to WAL; the switch ignores busy_timeout, so retry while others race it."""
    deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() == "wal":
                return
            con.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _run_script(con: sqlite3.Connection, script: str) -> None:
    """Run a multi-statement script inside the caller's open transaction."""
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    if statement.strip():
        con.execute(statement)
