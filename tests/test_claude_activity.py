"""Claude's transcript side of the agent activity lines: finding the transcript
of a live session and reading what the agent last did. No /proc, no network:
every test works under `tmp_path`."""

from __future__ import annotations

import json
import os
from pathlib import Path

from jailbee.accounts.adapters import claude_activity as ca
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


def _assistant(*blocks: dict[str, object]) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def _tool(name: str, **tool_input: object) -> dict[str, object]:
    return {"type": "tool_use", "name": name, "input": tool_input}


def _text(text: str) -> dict[str, object]:
    return {"type": "text", "text": text}


def _tail(*lines: str) -> bytes:
    return "\n".join(lines).encode()


def test_the_latest_tool_and_the_latest_message_win_even_from_different_records() -> None:
    raw = _tail(
        _assistant(_tool("Read", file_path="/a.py")),
        _assistant(_text("first thoughts")),
        _assistant(_tool("Bash", command="uv run pytest -x")),
        _assistant(_text("all\n  green now")),
        _assistant(_tool("Edit", file_path="/b.py")),
    )

    assert ca.parse_tail(raw) == ("Edit  /b.py", "all green now")


def test_only_assistant_records_count() -> None:
    raw = _tail(
        _assistant(_text("the real message")),
        json.dumps({"type": "user", "message": {"content": [_text("a prompt")]}}),
        json.dumps({"type": "attachment", "attachment": {"text": "x"}}),
    )

    assert ca.parse_tail(raw) == (None, "the real message")


def test_garbage_and_oddly_shaped_lines_are_skipped() -> None:
    raw = _tail(
        _assistant(_text("kept")),
        "not json",
        "[1, 2]",
        json.dumps({"type": "assistant"}),
        json.dumps({"type": "assistant", "message": {"content": "str"}}),
        json.dumps({"type": "assistant", "message": {"content": [7, None]}}),
    )

    assert ca.parse_tail(raw) == (None, "kept")


def test_a_tool_without_a_known_argument_shows_its_name_only() -> None:
    assert ca.parse_tail(_tail(_assistant(_tool("Whatever", x=1)))) == ("Whatever", None)
    assert ca.parse_tail(_tail(_assistant(_tool("Bash", command=5)))) == ("Bash", None)
    bad_input = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": "x"}]},
        }
    )
    assert ca.parse_tail(_tail(bad_input)) == ("Bash", None)


def test_long_arguments_and_messages_are_cut_with_an_ellipsis() -> None:
    raw = _tail(
        _assistant(_tool("Bash", command="x" * 500)),
        _assistant(_text("y" * 500)),
    )

    tool, message = ca.parse_tail(raw)

    assert tool is not None and message is not None
    assert tool.split("  ", 1)[1] == "x" * (ca.ARG_CHARS - 1) + "…"
    assert message == "y" * (ca.MESSAGE_CHARS - 1) + "…"


def test_control_characters_never_reach_the_result() -> None:
    raw = _tail(_assistant(_text("a\x1b[31mred\x07\x00 b")))

    assert ca.parse_tail(raw) == (None, "a[31mred b")


def test_an_empty_tail_has_nothing() -> None:
    assert ca.parse_tail(b"") == (None, None)


def test_a_tool_input_that_is_all_whitespace_shows_the_name_only() -> None:
    assert ca.parse_tail(_tail(_assistant(_tool("Bash", command="  \n ")))) == ("Bash", None)


def test_read_tail_of_a_small_file_is_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"AAAA\nBBBB\n")

    assert ca.read_tail(path, limit=100) == b"AAAA\nBBBB\n"


def test_read_tail_drops_the_line_the_cut_landed_in(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"AAAA\nBBBB\nCCCC\n")  # 15 bytes; last 9 start inside BBBB

    assert ca.read_tail(path, limit=9) == b"CCCC\n"


def test_read_tail_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert ca.read_tail(tmp_path / "nope.jsonl") is None


def test_read_tail_never_opens_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "t.jsonl"
    os.mkfifo(fifo)

    assert ca.read_tail(fifo) is None  # a blocking open would hang this test


def test_read_tail_does_not_follow_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "secret.jsonl"
    target.write_bytes(b"data\n")
    link = tmp_path / "t.jsonl"
    link.symlink_to(target)

    assert ca.read_tail(link) is None
