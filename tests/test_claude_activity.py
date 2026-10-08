"""Claude's transcript side of the agent activity lines: finding the transcript
of a live session and reading what the agent last did. No /proc, no network:
every test works under `tmp_path`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jailbee.accounts.adapters import claude_activity as ca
from jailbee.accounts.adapters.claude import ClaudeAdapter, read_session_files
from jailbee.accounts.models import RECENT_EVENTS, ActivityEvent, ActivityPaths, AgentSession

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


def _pair(raw: bytes) -> tuple[str | None, str | None]:
    tail = ca.parse_tail(raw)
    return tail.last_tool, tail.last_message


def test_the_latest_tool_and_the_latest_message_win_even_from_different_records() -> None:
    raw = _tail(
        _assistant(_tool("Read", file_path="/a.py")),
        _assistant(_text("first thoughts")),
        _assistant(_tool("Bash", command="uv run pytest -x")),
        _assistant(_text("all\n  green now")),
        _assistant(_tool("Edit", file_path="/b.py")),
    )

    assert _pair(raw) == ("Edit  /b.py", "all green now")


def test_only_assistant_records_count() -> None:
    raw = _tail(
        _assistant(_text("the real message")),
        json.dumps({"type": "user", "message": {"content": [_text("a prompt")]}}),
        json.dumps({"type": "attachment", "attachment": {"text": "x"}}),
    )

    assert _pair(raw) == (None, "the real message")


def test_garbage_and_oddly_shaped_lines_are_skipped() -> None:
    raw = _tail(
        _assistant(_text("kept")),
        "not json",
        "[1, 2]",
        json.dumps({"type": "assistant"}),
        json.dumps({"type": "assistant", "message": {"content": "str"}}),
        json.dumps({"type": "assistant", "message": {"content": [7, None]}}),
    )

    assert _pair(raw) == (None, "kept")


def test_a_tool_without_a_known_argument_shows_its_name_only() -> None:
    assert _pair(_tail(_assistant(_tool("Whatever", x=1)))) == ("Whatever", None)
    assert _pair(_tail(_assistant(_tool("Bash", command=5)))) == ("Bash", None)
    bad_input = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": "x"}]},
        }
    )
    assert _pair(_tail(bad_input)) == ("Bash", None)


def test_long_arguments_and_messages_are_cut_with_an_ellipsis() -> None:
    raw = _tail(
        _assistant(_tool("Bash", command="x" * 500)),
        _assistant(_text("y" * 1500)),
    )

    tail = ca.parse_tail(raw)

    assert tail.last_tool is not None and tail.last_message is not None
    assert tail.last_tool.split("  ", 1)[1] == "x" * (ca.ARG_CHARS - 1) + "…"
    assert tail.last_message == "y" * (ca.LAST_MESSAGE_CHARS - 1) + "…"
    assert ca.LAST_MESSAGE_CHARS == 1000
    # The history keeps the short cut.
    assert tail.recent[-1] == ActivityEvent("message", "y" * (ca.MESSAGE_CHARS - 1) + "…")


def test_recent_events_are_chronological_and_mix_tools_and_messages() -> None:
    raw = _tail(
        _assistant(_text("plan"), _tool("Read", file_path="/a.py")),
        "not json",
        json.dumps({"type": "user", "message": {"content": [_text("a prompt")]}}),
        _assistant(_tool("Bash", command="ls")),
        _assistant(_text("all\n green")),
    )

    assert ca.parse_tail(raw).recent == (
        ActivityEvent("message", "plan"),
        ActivityEvent("tool", "Read  /a.py"),
        ActivityEvent("tool", "Bash  ls"),
        ActivityEvent("message", "all green"),
    )


def test_recent_keeps_only_the_newest_twenty() -> None:
    raw = _tail(*(_assistant(_tool("Bash", command=f"step{i:02d}")) for i in range(25)))

    recent = ca.parse_tail(raw).recent

    assert RECENT_EVENTS == 20
    assert [e.text for e in recent] == [f"Bash  step{i:02d}" for i in range(5, 25)]


def test_a_full_history_does_not_stop_the_search_for_the_last_message() -> None:
    raw = _tail(
        _assistant(_text("the one message")),
        *(_assistant(_tool("Bash", command=f"s{i}")) for i in range(30)),
    )

    tail = ca.parse_tail(raw)

    assert tail.last_message == "the one message"
    assert len(tail.recent) == RECENT_EVENTS
    assert all(e.kind == "tool" for e in tail.recent)


def test_empty_texts_and_nameless_tools_make_no_event() -> None:
    raw = _tail(
        _assistant(_text("   ")),
        _assistant({"type": "tool_use", "input": {}}),
        _assistant(_text("kept")),
    )

    assert ca.parse_tail(raw).recent == (ActivityEvent("message", "kept"),)


def test_control_characters_never_reach_the_result() -> None:
    raw = _tail(_assistant(_text("a\x1b[31mred\x07\x00 b")))

    assert _pair(raw) == (None, "a[31mred b")


def test_control_characters_never_reach_recent_entries_or_the_last_message() -> None:
    dirty = "a\x1b[31mred\x07\x00 b\r\nc"
    raw = _tail(_assistant(_tool("Bash", command=dirty), _text(dirty)))

    tail = ca.parse_tail(raw)

    assert tail.recent  # premise: both events were recorded
    texts = [event.text for event in tail.recent] + [tail.last_message or ""]
    for text in texts:
        assert not any(ch < " " or ch == "\x7f" for ch in text), repr(text)
        assert "\x1b" not in text


def test_an_empty_tail_has_nothing() -> None:
    assert _pair(b"") == (None, None)


def test_a_tool_input_that_is_all_whitespace_shows_the_name_only() -> None:
    assert _pair(_tail(_assistant(_tool("Bash", command="  \n ")))) == ("Bash", None)


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


def _transcript(home: Path, project: str = "-home-dev-repo", sid: str = SID) -> Path:
    directory = home / "projects" / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sid}.jsonl"
    path.write_text(_assistant(_tool("Bash", command="ls")) + "\n", encoding="utf-8")
    return path


def test_locate_finds_the_transcript_in_whichever_project_holds_it(tmp_path: Path) -> None:
    _transcript(tmp_path, "-home-dev-other", sid="00000000-0000-0000-0000-000000000000")
    wanted = _transcript(tmp_path, "-home-dev-repo")

    paths = ca.locate(tmp_path, SID)

    assert paths == ActivityPaths(wanted, wanted.parent / SID / "subagents")


@pytest.mark.parametrize(
    "session_id",
    [None, "", "../../etc/passwd", "/etc/passwd", f"{SID}/../x", f"{SID}\x00", SID.upper() + "z"],
)
def test_locate_refuses_anything_that_is_not_a_uuid(tmp_path: Path, session_id: str | None) -> None:
    _transcript(tmp_path)

    assert ca.locate(tmp_path, session_id) is None


def test_locate_is_none_when_nothing_matches_or_projects_is_missing(tmp_path: Path) -> None:
    assert ca.locate(tmp_path, SID) is None
    _transcript(tmp_path, sid="00000000-0000-0000-0000-000000000000")
    assert ca.locate(tmp_path, SID) is None


def test_locate_skips_a_project_directory_that_is_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / f"{SID}.jsonl").write_text("{}\n", encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "-link").symlink_to(real, target_is_directory=True)

    assert ca.locate(tmp_path, SID) is None


def _subagent(directory: Path, name: str, age: float, now: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, (now - age, now - age))


def test_only_recently_written_subagent_files_count(tmp_path: Path) -> None:
    now = 1_000_000.0
    d = tmp_path / "subagents"
    _subagent(d, "agent-a.jsonl", 5, now)
    _subagent(d, "agent-b.jsonl", 29, now)
    _subagent(d, "agent-old.jsonl", 600, now)
    _subagent(d, "agent-a.meta.json", 1, now)
    _subagent(d, "notes.jsonl", 1, now)

    assert ca.count_fresh_subagents(d, now) == 2


def test_no_subagents_directory_means_zero_not_unknown(tmp_path: Path) -> None:
    assert ca.count_fresh_subagents(tmp_path / "subagents", 1.0) == 0


def test_a_subagent_symlink_is_not_counted(tmp_path: Path) -> None:
    now = 1_000_000.0
    d = tmp_path / "subagents"
    d.mkdir()
    target = tmp_path / "t.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    os.utime(target, (now, now))
    (d / "agent-x.jsonl").symlink_to(target)

    assert ca.count_fresh_subagents(d, now) == 0


def test_an_unreadable_subagents_directory_is_unknown(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "subagents"
    not_a_dir.write_text("x", encoding="utf-8")

    assert ca.count_fresh_subagents(not_a_dir, 1.0) is None


def test_read_activity_combines_the_tail_and_the_subagent_count(tmp_path: Path) -> None:
    now = 1_000_000.0
    transcript = _transcript(tmp_path)
    transcript.write_text(
        _assistant(_tool("Edit", file_path="/x.py")) + "\n" + _assistant(_text("done")) + "\n",
        encoding="utf-8",
    )
    paths = ActivityPaths(transcript, transcript.parent / SID / "subagents")
    _subagent(paths.subagents, "agent-a.jsonl", 3, now)

    activity = ca.read_activity(paths, now=now)

    assert activity is not None
    assert (activity.last_tool, activity.last_message) == ("Edit  /x.py", "done")
    assert activity.recent == (
        ActivityEvent("tool", "Edit  /x.py"),
        ActivityEvent("message", "done"),
    )
    assert activity.subagents == 1
    assert activity.shells is None  # not the adapter's to know
    assert activity.modified == paths.transcript.stat().st_mtime
    assert (activity.state, activity.since) == (None, None)  # the summary's job


def test_read_activity_without_a_readable_transcript_is_none(tmp_path: Path) -> None:
    paths = ActivityPaths(tmp_path / "gone.jsonl", tmp_path / "subagents")

    assert ca.read_activity(paths, now=1.0) is None


def test_the_adapter_locates_by_the_sessions_own_id(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    session = AgentSession("claude", 10, 500, "busy", None, None, None, session_id=SID)

    paths = ClaudeAdapter().locate_activity(tmp_path, session)

    assert paths is not None and paths.transcript == transcript


def test_locate_refuses_ids_whose_path_would_really_resolve(tmp_path: Path) -> None:
    """Positive controls: each hostile id points at a file that exists, so only
    the UUID check (not a missing file) can refuse it."""
    project = tmp_path / "projects" / "-home-dev-repo"
    (project / SID).mkdir(parents=True)
    (project / "x.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "passwd.jsonl").write_text("{}\n", encoding="utf-8")
    absolute = tmp_path / "abs"
    (tmp_path / "abs.jsonl").write_text("{}\n", encoding="utf-8")
    (project / f"{SID}x.jsonl").write_text("{}\n", encoding="utf-8")
    (project / f"{SID}\n.jsonl").write_text("{}\n", encoding="utf-8")
    extended = f"{SID}-0000"
    (project / f"{extended}.jsonl").write_text("{}\n", encoding="utf-8")
    upper_z = SID.upper() + "z"
    (project / f"{upper_z}.jsonl").write_text("{}\n", encoding="utf-8")

    for hostile in (
        f"{SID}/../x",
        "../../etc/passwd",
        str(absolute),
        f"{SID}x",
        f"{SID}\n",
        extended,
        upper_z,
    ):
        assert ca.locate(tmp_path, hostile) is None, hostile

    assert ca.locate(tmp_path, SID) is None  # no `<SID>.jsonl` itself


def test_read_activity_mtime_failure_is_unknown_not_an_exception(tmp_path, monkeypatch):
    transcript = _transcript(tmp_path)
    paths = ActivityPaths(transcript, tmp_path / "sub")

    def gone(path):
        raise OSError("gone")

    monkeypatch.setattr(Path, "lstat", gone)
    activity = ca.read_activity(paths, now=1.0)

    assert activity is not None
    assert activity.modified is None


def test_read_activity_does_not_follow_a_replacement_symlink_for_mtime(tmp_path, monkeypatch):
    transcript = _transcript(tmp_path)
    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    paths = ActivityPaths(transcript, tmp_path / "sub")
    read_tail = ca.read_tail

    def swapped(path):
        tail = read_tail(path)
        path.unlink()
        path.symlink_to(target)
        return tail

    monkeypatch.setattr(ca, "read_tail", swapped)
    activity = ca.read_activity(paths, now=1.0)

    assert activity is not None
    assert activity.modified is None
