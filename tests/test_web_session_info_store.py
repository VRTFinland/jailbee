"""`DbSessionInfoStore` under a `SessionInfoCache`, against a real (in-memory) database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from sqlmodel import Session

from jailbee.db.models import SessionInfoRow
from jailbee.transcripts.base import SESSION_INFO_FORMAT, SessionInfo, SessionInfoCache, Stamp
from jailbee.transcripts.claude import ClaudeSource
from jailbee.web.session_info_store import DbSessionInfoStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Engine

SID_A = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"


def _session(home: Path, project: str, session_id: str, prompt: str) -> Path:
    path = home / "projects" / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "cwd": "/home/dev/p",
        "timestamp": "2026-11-20T10:00:00.000Z",
        "message": {"role": "user", "content": prompt},
    }
    path.write_text(json.dumps(record) + "\n")
    return path


def _list(home: Path, engine: Engine) -> list[SessionInfo]:
    cache = SessionInfoCache(DbSessionInfoStore(engine))
    found = ClaudeSource().list_sessions(home, cache)
    cache.flush()
    return found


def test_a_row_stored_by_an_older_reader_is_read_again(tmp_path: Path, db_engine: Engine) -> None:
    path = _session(tmp_path, "-home-dev-p", SID_A, "the real title")
    st = path.stat()
    with Session(db_engine) as db:
        db.add(
            SessionInfoRow(
                path=str(path),
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                format=SESSION_INFO_FORMAT - 1,
                session_id=SID_A,
                title="what an older reader derived",
            )
        )
        db.commit()

    (info,) = _list(tmp_path, db_engine)

    assert (info.title, info.cwd) == ("the real title", "/home/dev/p")


def test_a_stored_row_answers_with_its_cwd_while_the_file_is_unchanged(
    tmp_path: Path, db_engine: Engine
) -> None:
    _session(tmp_path, "-home-dev-p", SID_A, "first")
    _list(tmp_path, db_engine)

    (info,) = _list(tmp_path, db_engine)

    assert (info.title, info.cwd) == ("first", "/home/dev/p")


def test_pruning_forgets_the_rows_of_deleted_transcripts(tmp_path: Path, db_engine: Engine) -> None:
    kept = _session(tmp_path, "-home-dev-p", SID_A, "kept")
    gone = _session(tmp_path, "-home-dev-p", SID_B, "gone")
    _list(tmp_path, db_engine)
    gone.unlink()
    store = DbSessionInfoStore(db_engine)

    SessionInfoCache(store).prune()

    assert list(store.load_dir(kept.parent)) == [kept]


def test_a_directory_loads_only_its_own_rows(tmp_path: Path, db_engine: Engine) -> None:
    mine = _session(tmp_path, "-home-dev-p", SID_A, "mine")
    _session(tmp_path, "-home-dev-p2", SID_B, "a sibling's")
    _list(tmp_path, db_engine)
    store = DbSessionInfoStore(db_engine)
    below = mine.parent / SID_A / "subagents" / "agent-a.jsonl"
    store.save([(below, (1, 1), SessionInfo("a", below, "below it", None, None, ()))])

    assert list(store.load_dir(mine.parent)) == [mine]


class _RecordingStore:
    """Delegates to a real store and records which directories were loaded."""

    def __init__(self, inner: DbSessionInfoStore) -> None:
        self.inner = inner
        self.loaded: list[Path] = []

    def load_dir(self, directory: Path) -> dict[Path, tuple[Stamp, SessionInfo]]:
        self.loaded.append(directory)
        return self.inner.load_dir(directory)

    def save(self, entries: Sequence[tuple[Path, Stamp, SessionInfo]]) -> bool:
        return self.inner.save(entries)

    def prune_missing(self) -> None:
        self.inner.prune_missing()


def test_listing_one_repo_loads_no_other_repos_rows(tmp_path: Path, db_engine: Engine) -> None:
    _session(tmp_path, "-home-dev-alpha", SID_A, "alpha's")
    _session(tmp_path, "-home-dev-beta", SID_B, "beta's")
    _list(tmp_path, db_engine)
    store = _RecordingStore(DbSessionInfoStore(db_engine))

    ClaudeSource().list_sessions(tmp_path, SessionInfoCache(store), cwds=["/home/dev/alpha"])

    assert store.loaded == [tmp_path / "projects" / "-home-dev-alpha"]


def test_the_warm_up_prunes_the_rows_of_deleted_transcripts(
    tmp_path: Path, db_engine: Engine
) -> None:
    import threading

    from jailbee.web.repo_sessions import RepoSessions

    gone = _session(tmp_path, "-home-dev-p", SID_B, "gone")
    _list(tmp_path, db_engine)
    gone.unlink()
    store = DbSessionInfoStore(db_engine)
    sessions = RepoSessions(lambda _prefix: [("claude", tmp_path)], store)

    sessions.warm([], lambda _prefix: None, None, threading.Event())

    assert store.load_dir(gone.parent) == {}


def test_pruning_drops_older_readers_rows_but_keeps_newer_ones(
    tmp_path: Path, db_engine: Engine
) -> None:
    from sqlmodel import select

    path = _session(tmp_path, "-home-dev-p", SID_A, "kept")
    with Session(db_engine) as db:
        for fmt in (SESSION_INFO_FORMAT - 1, SESSION_INFO_FORMAT + 1):
            db.add(
                SessionInfoRow(
                    path=f"{path}.{fmt}", size=1, mtime_ns=1, format=fmt, session_id="s", title=""
                )
            )
        db.commit()
    for fmt in (SESSION_INFO_FORMAT - 1, SESSION_INFO_FORMAT + 1):
        Path(f"{path}.{fmt}").touch()

    DbSessionInfoStore(db_engine).prune_missing()

    with Session(db_engine) as db:
        formats = db.exec(select(SessionInfoRow.format)).all()
    assert formats == [SESSION_INFO_FORMAT + 1]
