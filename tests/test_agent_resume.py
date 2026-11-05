import json

import pytest

from jailbee.agent_resume import ResumeEntry, parse_snapshot, replay_argv, resume_run

VALID_ID = "12345678-abcd-1234-abcd-1234567890ab"


def test_parse_drops_bad_entries_and_keeps_good() -> None:
    snapshot = {
        "version": 1,
        "source": "capture",
        "sessions": [
            {
                "agent": "claude",
                "session_id": VALID_ID,
                "cwd": "/work/project",
                "launcher": ["/usr/bin/claude", "--model", "opus"],
                "window": "3",
            },
            {
                "agent": "claude",
                "session_id": "../x",
                "cwd": "/work",
                "launcher": ["claude"],
                "window": None,
            },
            {
                "agent": "claude",
                "session_id": VALID_ID,
                "cwd": "relative",
                "launcher": ["claude"],
                "window": None,
            },
            {
                "agent": "claude",
                "session_id": VALID_ID,
                "cwd": "/work",
                "launcher": ["rm"],
                "window": None,
            },
            {
                "agent": "claude",
                "session_id": VALID_ID,
                "cwd": "/work",
                "launcher": [],
                "window": None,
            },
            {
                "agent": "codex",
                "session_id": VALID_ID,
                "cwd": "/work",
                "launcher": ["claude"],
                "window": None,
            },
            {
                "agent": "claude",
                "session_id": VALID_ID,
                "cwd": "/work",
                "launcher": ["claude"],
                "window": 5,
            },
        ],
    }

    assert parse_snapshot(json.dumps(snapshot)) == [
        ResumeEntry(VALID_ID, "/work/project", ("/usr/bin/claude", "--model", "opus"), "3")
    ]


@pytest.mark.parametrize(
    "raw",
    ["", "not json", '{"version":2,"sessions":[]}', "{" + '"version":1,"sessions":' + "[" * 2000],
)
def test_parse_invalid_snapshot_returns_empty(raw: str) -> None:
    assert parse_snapshot(raw) == []


@pytest.mark.parametrize(
    ("launcher", "expected"),
    [
        (["claude", "--agent", "reviewer"], ["claude", "--agent", "reviewer"]),
        (["claude", "--effort", "high"], ["claude", "--effort", "high"]),
        (["claude", "--plugin-dir", "/plugins"], ["claude", "--plugin-dir", "/plugins"]),
        (["claude", "--add-dir", "/a", "/b"], ["claude", "--add-dir", "/a", "/b"]),
        (["claude", "--mcp-config", "a", "b"], ["claude", "--mcp-config", "a", "b"]),
        (["claude", "--allowedTools", "X", "Y"], ["claude", "--allowedTools", "X", "Y"]),
        (["claude", "--session-id", "--resume"], ["claude"]),
        (["claude", "-n", "work"], ["claude", "-n", "work"]),
        (["claude", "--allowed-tools", "X", "Y"], ["claude", "--allowed-tools", "X", "Y"]),
        (["claude", "--betas", "one", "two"], ["claude", "--betas", "one", "two"]),
        (["claude", "--file", "a", "b"], ["claude", "--file", "a", "b"]),
        (["claude", "--from-pr", "123", "--model", "opus"], ["claude", "--model", "opus"]),
        (["claude", "--from-pr=123", "--model", "opus"], ["claude", "--model", "opus"]),
        (["claude", "--system-prompt", "--resume"], ["claude", "--system-prompt", "--resume"]),
        (
            ["claude-jb", "-P", "codex", "-C", "1m", "--model", "opus"],
            ["claude-jb", "-P", "codex", "-C", "1m", "--model", "opus"],
        ),
        (["claude", "--resume", "abc", "--model", "opus"], ["claude", "--model", "opus"]),
        (["claude", "-r"], ["claude"]),
        (["claude", "-c", "--model", "haiku"], ["claude", "--model", "haiku"]),
        (["claude", "--session-id=X", "--fork-session"], ["claude"]),
        (["claude", "--model", "opus", "fix the bug"], ["claude", "--model", "opus"]),
        (["claude", "fix bug", "--model", "opus"], ["claude", "--model", "opus"]),
        (["claude", "--", "fix bug"], ["claude"]),
        (
            ["claude", "--system-prompt", "--resume", "--model", "opus"],
            ["claude", "--system-prompt", "--resume", "--model", "opus"],
        ),
        (
            ["claude", "--session-id", "abc", "--tools", "Read,Write", "--permission-mode", "plan"],
            ["claude", "--tools", "Read,Write", "--permission-mode", "plan"],
        ),
        (["claude", "-p", "hi"], ["claude"]),
        (["claude-jb", "-P", "deep"], ["claude-jb", "-P", "deep"]),
    ],
)
def test_replay_argv_removes_session_and_prompt_args(
    launcher: list[str], expected: list[str]
) -> None:
    assert replay_argv(launcher) == expected


def test_resume_run_quotes_directory_and_adds_resume_args() -> None:
    entry = ResumeEntry(VALID_ID, "/home/dev/my repo", ("claude", "--model", "opus"), None)
    assert (
        resume_run(entry)
        == f"cd '/home/dev/my repo' && exec claude --model opus --resume {VALID_ID}"
    )
