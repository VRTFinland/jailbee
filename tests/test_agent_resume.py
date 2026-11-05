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


def test_restore_matches_and_deduplicates_windows(make_cfg, tmp_path):
    from jailbee.agent_resume import restore_steps
    from jailbee.config.models_agents import AutostartStep

    cfg = make_cfg(tmp_path)
    step = AutostartStep(name="claude", run="exec claude", env={"keep": "yes"})
    entries = [ResumeEntry(VALID_ID, "/work", ("claude",), "claude"),
               ResumeEntry(VALID_ID, "/work", ("claude",), "claude")]
    result = restore_steps(cfg, [step], entries, prompted=None, extra_env={"ROLE": "child"})
    assert result[0].run == resume_run(entries[0])
    assert result[0].env == {"keep": "yes"}
    assert result[1].name == "claude-2"
    assert result[1].env["ROLE"] == "child"


def test_restore_drops_prompted_window(make_cfg, tmp_path):
    from jailbee.agent_resume import restore_steps
    from jailbee.config.models_agents import AutostartStep

    step = AutostartStep(name="claude", run="prompt")
    entry = ResumeEntry(VALID_ID, "/work", ("claude",), "claude")
    assert restore_steps(make_cfg(tmp_path), [step], [entry], prompted="claude", extra_env={}) == [step]


def test_boot_entries_checks_transcripts(make_cfg, tmp_path, mocker):
    from jailbee.agent_resume import boot_entries
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.exec_capped.return_value = json.dumps({"version": 1, "source": "capture", "sessions": [
        {"agent": "claude", "session_id": VALID_ID, "cwd": "/work", "launcher": ["claude"], "window": None}]})
    exists = mocker.patch("jailbee.agent_resume.ClaudeAdapter.session_exists", return_value=False)
    assert boot_entries(cfg, incus, "box") == []
    exists.assert_called_once_with(cfg, "box", VALID_ID)
    incus.exec_capped.assert_called_once_with("box", ["jailbee", "_agent-snapshot", "--boot"], max_bytes=65536, uid=cfg.container_user.uid, timeout=15)


def test_boot_and_capture_handle_incus_errors(make_cfg, tmp_path, mocker):
    from jailbee.agent_resume import boot_entries, capture
    from jailbee.incus import IncusError
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.exec.side_effect = IncusError("offline")
    incus.exec_capped.side_effect = IncusError("offline")
    capture(cfg, incus, "box")
    assert boot_entries(cfg, incus, "box") == []
    incus.exec.assert_called_once_with("box", ["jailbee", "_agent-snapshot", "--capture"], uid=cfg.container_user.uid, timeout=15)


def test_restore_inherits_matching_custom_launcher_env(make_cfg, tmp_path):
    from jailbee.agent_resume import restore_steps
    cfg = make_cfg(tmp_path)
    from jailbee.config.models_agents import ClaudeAgentConfig
    agent = ClaudeAgentConfig(command="claude-jb -P codex", env={"TOKEN": "value"}, autostart=False)
    cfg = cfg.model_copy(update={"agents": {"claude": agent}})
    entry = ResumeEntry(VALID_ID, "/work", ("claude-jb", "-P", "codex"), None)
    result = restore_steps(cfg, [], [entry], prompted=None, extra_env={"ROLE": "child"})
    assert result[0].env == {"ROLE": "child", "TOKEN": "value"}
    assert result[0].background and result[0].continue_on_error


def test_restore_deduplicates_sanitized_window_names(make_cfg, tmp_path):
    from jailbee.agent_resume import restore_steps
    from jailbee.config.models_agents import AutostartStep
    step = AutostartStep(name="work space", run="other")
    entry = ResumeEntry(VALID_ID, "/work", ("claude",), "work space")
    result = restore_steps(make_cfg(tmp_path), [step], [entry], prompted=None, extra_env={})
    assert result[1].name == "work_space-2"


def test_restore_reserves_configured_step_windows(make_cfg, tmp_path):
    from jailbee.agent_resume import restore_steps
    from jailbee.config.models_agents import Autostart, AutostartStep
    cfg = make_cfg(tmp_path).model_copy(update={"autostart": Autostart(on_start=[AutostartStep(name="work space", run="service")])})
    entry = ResumeEntry(VALID_ID, "/work", ("claude",), "work_space")
    result = restore_steps(cfg, [], [entry], prompted=None, extra_env={})
    assert result[0].name == "work_space-2"


@pytest.mark.parametrize("option", ["--autocompact", "--remote-control-session-name-prefix", "--prompt-suggestions", "--worktree", "-w", "--debug", "-d", "--remote-control", "--permission-prompts", "--environment"])
def test_replay_preserves_current_option_values(option):
    assert replay_argv(["claude", option, "value"]) == ["claude", option, "value"]


def test_restore_hostile_window_is_safe(make_cfg, tmp_path):
    from jailbee.agent_resume import restore_steps
    entry = ResumeEntry(VALID_ID, "/work", ("claude",), "[/]\x1b[31m")
    name = restore_steps(make_cfg(tmp_path), [], [entry], prompted=None, extra_env={})[0].name
    assert "[" not in name and "\x1b" not in name and len(name) <= 64
