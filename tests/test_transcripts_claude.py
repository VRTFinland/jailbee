"""The Claude transcript source, driven directly against JSONL fixtures.

The fixtures under `tests/fixtures/transcripts/claude/` are sanitized copies of
the record shapes Claude Code 2.1.3xx writes; no real conversation content.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jailbee.transcripts.base import (
    AssistantText,
    ReadResult,
    SubagentRef,
    Thinking,
    ToolCall,
    ToolResult,
    UserMessage,
)
from jailbee.transcripts.claude import ClaudeSource

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "claude"
SID = "11111111-1111-4111-8111-111111111111"
OTHER_SID = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "a1b2c3d4e5f6a7b8c"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A writable copy of the fixture config home."""
    target = tmp_path / "claude"
    shutil.copytree(FIXTURES, target)
    return target


@pytest.fixture
def source() -> ClaudeSource:
    return ClaudeSource()


def _transcript(home: Path) -> Path:
    return home / "projects" / "-work-repo" / f"{SID}.jsonl"


def test_reading_a_transcript_yields_user_and_assistant_entries_in_order(
    home: Path, source: ClaudeSource
) -> None:
    result = source.read(_transcript(home))

    users = [e for e in result.entries if isinstance(e, UserMessage)]
    answers = [e for e in result.entries if isinstance(e, AssistantText)]
    assert [u.text for u in users] == [
        "Fix the login bug\n  in the   auth module",
        "Thanks, now add a test",
    ]
    assert [(a.text, a.model) for a in answers] == [
        ("I'll read the file.", "claude-opus-5-5"),
        ("Done.", "claude-sonnet-5"),
    ]
    assert result.offset == _transcript(home).stat().st_size


def test_thinking_is_an_entry_with_its_model(home: Path, source: ClaudeSource) -> None:
    entries = source.read(_transcript(home)).entries

    (thinking,) = [e for e in entries if isinstance(e, Thinking)]
    assert (thinking.text, thinking.model) == ("Read auth.py first.", "claude-opus-5-5")


def test_a_tool_call_has_a_one_line_summary_and_its_input(home: Path, source: ClaudeSource) -> None:
    entries = source.read(_transcript(home)).entries

    read_call = next(e for e in entries if isinstance(e, ToolCall) and e.name == "Read")
    assert read_call.summary == "Read  /work/repo/auth.py"
    assert read_call.input == {"file_path": "/work/repo/auth.py"}
    assert read_call.model == "claude-opus-5-5"


def test_a_tool_result_is_linked_to_its_call(home: Path, source: ClaudeSource) -> None:
    entries = source.read(_transcript(home)).entries

    calls = {e.call_id: e for e in entries if isinstance(e, ToolCall)}
    results = [e for e in entries if isinstance(e, ToolResult)]
    assert [(calls[r.call_id].name, r.text) for r in results] == [
        ("Read", "def login():\n    pass\n"),
        ("Agent", "Looks fine."),
    ]
    assert not any(r.truncated for r in results)


def _write(path: Path, *records: object, tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail, encoding="utf-8")


def _user_text(uuid: str, text: str) -> dict[str, object]:
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}


def test_a_long_tool_result_is_truncated_and_says_so(tmp_path: Path, source: ClaudeSource) -> None:
    path = tmp_path / "t.jsonl"
    long_output = "x" * 10_000
    _write(
        path,
        {
            "type": "user",
            "uuid": "r1",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": long_output}
                ]
            },
        },
    )

    (result,) = source.read(path).entries

    assert isinstance(result, ToolResult)
    assert result.truncated
    assert result.full_length == 10_000
    assert 0 < len(result.text) < 10_000
    assert long_output.startswith(result.text)


def test_a_subagent_reference_follows_the_agent_tool_result(
    home: Path, source: ClaudeSource
) -> None:
    entries = source.read(_transcript(home)).entries

    (ref,) = [e for e in entries if isinstance(e, SubagentRef)]
    agent_call = next(e for e in entries if isinstance(e, ToolCall) and e.name == "Agent")
    assert ref.call_id == agent_call.call_id
    assert (ref.agent_id, ref.agent_type) == (AGENT_ID, "general-purpose")


def test_a_subagent_id_resolves_to_its_transcript(home: Path, source: ClaudeSource) -> None:
    path = source.subagent_transcript(home, SID, AGENT_ID)

    assert path is not None
    sub_entries = source.read(path).entries
    assert [type(e).__name__ for e in sub_entries] == ["UserMessage", "AssistantText"]
    assert isinstance(sub_entries[1], AssistantText)
    assert sub_entries[1].model == "claude-haiku-5"


@pytest.mark.parametrize("agent_id", ["../x", "a/b", "", "nope", "a" * 200])
def test_an_unknown_or_unsafe_subagent_id_resolves_to_nothing(
    home: Path, source: ClaudeSource, agent_id: str
) -> None:
    assert source.subagent_transcript(home, SID, agent_id) is None


def test_locate_finds_a_session_and_its_subagents(home: Path, source: ClaudeSource) -> None:
    files = source.locate(home, SID)

    assert files is not None
    assert files.transcript == _transcript(home)
    assert list(files.subagents) == [AGENT_ID]
    assert files.subagents[AGENT_ID].name == f"agent-{AGENT_ID}.jsonl"


def test_locate_refuses_a_non_uuid_session_id(home: Path, source: ClaudeSource) -> None:
    assert source.locate(home, "../../etc/passwd") is None
    assert source.locate(home, "33333333-3333-4333-8333-333333333333") is None


def test_a_symlinked_subagent_directory_is_not_followed(
    home: Path, tmp_path: Path, source: ClaudeSource
) -> None:
    session_dir = home / "projects" / "-work-repo" / SID
    moved = tmp_path / "elsewhere"
    session_dir.rename(moved)
    session_dir.symlink_to(moved)

    assert source.subagent_transcript(home, SID, AGENT_ID) is None
    files = source.locate(home, SID)
    assert files is not None
    assert files.subagents == {}


def test_a_partial_trailing_line_waits_for_the_next_read(
    tmp_path: Path, source: ClaudeSource
) -> None:
    path = tmp_path / "t.jsonl"
    whole = json.dumps(_user_text("r1", "first")) + "\n"
    second = json.dumps(_user_text("r2", "second")) + "\n"
    path.write_text(whole + second[:10], encoding="utf-8")

    first_read = source.read(path)

    assert [e.text for e in first_read.entries if isinstance(e, UserMessage)] == ["first"]
    assert first_read.offset == len(whole.encode())

    path.write_text(whole + second, encoding="utf-8")
    next_read = source.read_from(path, first_read.offset)

    assert [e.text for e in next_read.entries if isinstance(e, UserMessage)] == ["second"]
    assert next_read.offset == path.stat().st_size


def test_a_read_from_the_end_returns_nothing_new(home: Path, source: ClaudeSource) -> None:
    path = _transcript(home)
    end = path.stat().st_size

    assert source.read_from(path, end) == ReadResult((), end)


def test_entry_ids_are_the_same_whole_or_incremental(home: Path, source: ClaudeSource) -> None:
    path = _transcript(home)
    data = path.read_bytes()
    middle = data.index(b"\n", len(data) // 2) + 1

    whole = source.read(path)
    path.write_bytes(data[:middle])
    head = source.read(path)
    path.write_bytes(data)
    tail = source.read_from(path, head.offset)

    assert [e.id for e in head.entries + tail.entries] == [e.id for e in whole.entries]
    assert len({e.id for e in whole.entries}) == len(whole.entries)


def test_a_malformed_line_is_skipped(tmp_path: Path, source: ClaudeSource) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(_user_text("r1", "before"))
        + "\n{not json\n[1, 2]\n"
        + json.dumps({"type": "assistant", "message": {"content": "not a list"}})
        + "\n"
        + json.dumps(_user_text("r2", "after"))
        + "\n",
        encoding="utf-8",
    )

    result = source.read(path)

    assert [e.text for e in result.entries if isinstance(e, UserMessage)] == ["before", "after"]
    assert result.offset == path.stat().st_size


def test_a_missing_file_or_fifo_reads_as_nothing(tmp_path: Path, source: ClaudeSource) -> None:
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)

    assert source.read(tmp_path / "missing.jsonl").entries == ()
    assert source.read(fifo).entries == ()


def test_an_offset_past_a_shrunken_file_reads_it_from_the_start(
    tmp_path: Path, source: ClaudeSource
) -> None:
    path = tmp_path / "t.jsonl"
    _write(path, _user_text("r1", "replaced"))

    result = source.read_from(path, 10_000)

    assert [e.text for e in result.entries if isinstance(e, UserMessage)] == ["replaced"]
    assert result.offset == path.stat().st_size


def test_a_cache_skips_an_unchanged_transcript_and_rereads_a_grown_one(
    home: Path, source: ClaudeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.transcripts import claude
    from jailbee.transcripts.base import SessionInfoCache

    reads: list[str] = []
    real = claude.session_info

    def counting(session_id: str, path: Path) -> object:
        reads.append(session_id)
        return real(session_id, path)

    monkeypatch.setattr(claude, "session_info", counting)
    cache = SessionInfoCache()
    source.list_sessions(home, cache)
    assert sorted(reads) == [SID, OTHER_SID]
    reads.clear()
    source.list_sessions(home, cache)
    assert reads == []  # nothing changed
    path = home / "projects" / "-work-repo" / f"{SID}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(_user_text("late", "later question")) + "\n")
    source.list_sessions(home, cache)
    assert reads == [SID]


def test_sessions_are_listed_newest_activity_first(home: Path, source: ClaudeSource) -> None:
    sessions = source.list_sessions(home)

    assert [s.session_id for s in sessions] == [SID, OTHER_SID]
    assert sessions[0].path == _transcript(home)


def test_session_metadata_comes_from_the_transcript(home: Path, source: ClaudeSource) -> None:
    info = source.list_sessions(home)[0]

    assert info.title == "Fix the login bug in the auth module"
    assert info.started == datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
    assert info.last_activity == datetime(2026, 11, 1, 10, 5, 1, tzinfo=UTC)
    assert info.models == ("claude-opus-5-5", "claude-sonnet-5")


def test_session_title_uses_the_typed_prompt_after_local_command_records(
    home: Path, source: ClaudeSource
) -> None:
    _write(
        _transcript(home),
        _user_text("command", "<command-name>/clear</command-name>"
                   " <command-message>clear</command-message> <command-args></command-args>"),
        _user_text("caveat", "<local-command-caveat>Ignore local output</local-command-caveat>"),
        _user_text("stdout", "<local-command-stdout>Cleared</local-command-stdout>"),
        _user_text("prompt", "Fix the session titles"),
    )

    info = next(s for s in source.list_sessions(home) if s.session_id == SID)

    assert info.title == "Fix the session titles"


def test_a_command_only_session_uses_its_first_command_as_the_title(
    home: Path, source: ClaudeSource
) -> None:
    _write(
        _transcript(home),
        _user_text("caveat", "<local-command-caveat>Ignore local output</local-command-caveat>"),
        _user_text("command", "<command-name>/model</command-name>"
                   " <command-message>model</command-message> <command-args>opus</command-args>"),
        _user_text("stdout", "<local-command-stdout>Model changed</local-command-stdout>"),
        _user_text("next", "<command-name>/clear</command-name>"),
    )

    info = next(s for s in source.list_sessions(home) if s.session_id == SID)

    assert info.title == "/model"


@pytest.mark.parametrize(
    "preamble",
    [
        _user_text("caveat", "<local-command-caveat>Local only</local-command-caveat>"),
        _user_text("stdout", "<local-command-stdout>Output</local-command-stdout>"),
        _user_text("reminder", "  <system-reminder>Injected\ncontext</system-reminder>"),
        _user_text("mixed", "<system-reminder>Injected</system-reminder>\nFix the session titles"),
        {"type": "user", "isMeta": True, "message": {"content": "Injected context"}},
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "<command-name>/model</command-name>"},
            {"type": "text", "text": "Fix the session titles"},
        ]}},
    ],
)
def test_session_title_skips_each_kind_of_agent_preamble(
    home: Path, source: ClaudeSource, preamble: object
) -> None:
    _write(_transcript(home), preamble, _user_text("prompt", "Fix the session titles"),
           _user_text("later", "A later prompt"))

    info = next(s for s in source.list_sessions(home) if s.session_id == SID)

    assert info.title == "Fix the session titles"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<example>broken</example>", "<example>broken</example>"),
        ("<system-reminder>Injected</system-reminder>\nFix the session titles",
         "Fix the session titles"),
        ([{"type": "text", "text": "<command-name>/model</command-name>"},
          {"type": "text", "text": "Fix the session titles"}], "Fix the session titles"),
    ],
)
def test_the_first_typed_text_survives_markup_and_text_block_boundaries(
    home: Path, source: ClaudeSource, content: object, expected: str
) -> None:
    _write(_transcript(home), {"type": "user", "message": {"content": content}},
           _user_text("later", "A later prompt"))

    info = next(s for s in source.list_sessions(home) if s.session_id == SID)

    assert info.title == expected


def test_listing_parses_only_candidate_lines_even_when_commands_precede_a_large_session(
    home: Path, source: ClaudeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        _transcript(home),
        _user_text("command", "<command-name>/model</command-name>"),
        *({"type": "assistant", "message": {"content": "x" * 10_000}} for _ in range(200)),
        _user_text("prompt", "Fix the session titles"),
    )
    loads = json.loads
    parsed: list[object] = []

    def count_loads(data: object) -> object:
        parsed.append(data)
        return loads(data)

    monkeypatch.setattr(json, "loads", count_loads)

    info = source.session_info(SID, _transcript(home))

    assert info is not None and info.title == "Fix the session titles"
    assert len(parsed) == 2


def test_a_claude_jb_session_reports_its_gateway_model_names(
    home: Path, source: ClaudeSource
) -> None:
    info = next(s for s in source.list_sessions(home) if s.session_id == OTHER_SID)

    assert info.models == ("jb.codex.standard",)
    assert info.title == "hello there"


def test_the_title_skips_meta_and_tool_results_and_is_cut(
    tmp_path: Path, source: ClaudeSource
) -> None:
    home = tmp_path / "claude"
    sid = "44444444-4444-4444-8444-444444444444"
    _write(
        home / "projects" / "-p" / f"{sid}.jsonl",
        {"type": "user", "isMeta": True, "message": {"content": "<system-reminder>"}},
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]},
        },
        _user_text("r1", "word " * 100),
    )

    (info,) = source.list_sessions(home)

    assert info.title.startswith("word word")
    assert info.title.endswith("…")
    assert len(info.title) < 200


def test_an_empty_session_has_an_empty_title_and_no_models(
    tmp_path: Path, source: ClaudeSource
) -> None:
    home = tmp_path / "claude"
    sid = "55555555-5555-4555-8555-555555555555"
    _write(home / "projects" / "-p" / f"{sid}.jsonl", {"type": "permission-mode"})

    (info,) = source.list_sessions(home)

    assert (info.title, info.models, info.started, info.last_activity) == ("", (), None, None)


def test_only_session_transcripts_are_listed(tmp_path: Path, source: ClaudeSource) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "-p"
    _write(project / "notes.jsonl", _user_text("r", "not a session"))
    (project / "linked").mkdir()
    (home / "projects" / "-link").symlink_to(project)

    assert source.list_sessions(home) == []
    assert source.list_sessions(tmp_path / "nowhere") == []


def test_a_synthetic_message_names_no_model(tmp_path: Path, source: ClaudeSource) -> None:
    home = tmp_path / "claude"
    sid = "66666666-6666-4666-8666-666666666666"
    path = home / "projects" / "-p" / f"{sid}.jsonl"
    _write(
        path,
        {
            "type": "assistant",
            "uuid": "r1",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "API Error: overloaded"}],
            },
        },
    )

    (info,) = source.list_sessions(home)
    (entry,) = source.read(path).entries

    assert info.models == ()
    assert isinstance(entry, AssistantText)
    assert entry.model is None
