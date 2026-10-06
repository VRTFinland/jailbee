"""Claude's transcript side of the agent activity lines: finding the transcript
of a live session and reading what the agent last did. No /proc, no network:
every test works under `tmp_path`."""

from __future__ import annotations

import json
from pathlib import Path

from jailbee.accounts.adapters.claude import read_session_files

SID = "569e3205-9e79-44d7-8aea-f91ebd716f8c"


def _session_file(home: Path, **fields: object) -> None:
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    data = {"pid": 10, "procStart": "500", "status": "busy", **fields}
    (sessions / "10.json").write_text(json.dumps(data), encoding="utf-8")


def test_session_id_is_read_from_the_session_file(tmp_path: Path) -> None:
    _session_file(tmp_path, sessionId=SID)

    (session,) = read_session_files(tmp_path)

    assert session.session_id == SID


def test_a_missing_or_mistyped_session_id_is_none(tmp_path: Path) -> None:
    for fields in ({}, {"sessionId": 5}, {"sessionId": ""}, {"sessionId": None}):
        _session_file(tmp_path, **fields)

        (session,) = read_session_files(tmp_path)

        assert session.session_id is None, fields


def test_a_long_session_id_is_cut_not_trusted(tmp_path: Path) -> None:
    _session_file(tmp_path, sessionId="a" * 500)

    (session,) = read_session_files(tmp_path)

    assert session.session_id is not None
    assert len(session.session_id) == 64
