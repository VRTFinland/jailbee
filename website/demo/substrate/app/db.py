"""SQLite storage for the demo app.

The database path comes from $DEMO_DB so tests can point it at tmp_path.
Its default lives under the repo's own var/ directory, which means each
jailbee container gets its own copy by construction -- that is the point
the site's "the database is shared" claim is about.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path


def _db_path() -> Path:
    configured = os.environ.get("DEMO_DB")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "var" / "app.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    """Create the schema if it is not there yet. Safe to call repeatedly."""
    # `closing(...)` as well as the connection's own context manager:
    # Connection.__exit__ commits on success and rolls back on failure, but it
    # never closes. Without this the connections survive in reference cycles
    # until the cyclic collector runs — measured at 157 live connections and
    # 161 open fds after 400 operations.
    with closing(_connect()) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)"
        )


def add_item(name: str) -> int:
    with closing(_connect()) as connection, connection:
        cursor = connection.execute("INSERT INTO items (name) VALUES (?)", (name,))
        return int(cursor.lastrowid or 0)


def list_items() -> list[dict[str, str | int]]:
    with closing(_connect()) as connection, connection:
        rows = connection.execute("SELECT id, name FROM items ORDER BY id").fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]
