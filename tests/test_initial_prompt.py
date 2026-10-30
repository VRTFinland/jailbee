from __future__ import annotations

import os
import subprocess

import pytest

from jailbee.incus import IncusError
from jailbee.initial_prompt import (
    CONSUMED,
    CONSUMED_PATH,
    INITIAL_PROMPT_LABEL,
    PENDING,
    PROMPT_PATH,
    PromptError,
    prompt_pending,
    prompt_problem,
    prompted_run,
    validate_prompt_text,
    write_prompt,
)
from tests.conftest import make_cfg


def test_validate_rejects_blank() -> None:
    with pytest.raises(PromptError):
        validate_prompt_text("  \n")


def test_validate_rejects_oversize() -> None:
    with pytest.raises(PromptError):
        validate_prompt_text("x" * 65537)


def test_validate_counts_utf8_bytes() -> None:
    with pytest.raises(PromptError):
        validate_prompt_text("ä" * 32769)


def test_validate_returns_text_unchanged() -> None:
    assert validate_prompt_text(" hi\n") == " hi\n"


def test_prompt_problem_none_for_claude(tmp_path) -> None:
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True, "autostart": True}})
    assert prompt_problem(cfg) is None


def test_prompt_problem_names_agent_without_prompt_args(tmp_path) -> None:
    cfg = make_cfg(tmp_path, agents={"aider": {"enabled": True, "autostart": True}})
    msg = prompt_problem(cfg)
    assert msg is not None and "aider" in msg and "prompt_args" in msg


def test_prompt_problem_no_agent(tmp_path) -> None:
    msg = prompt_problem(make_cfg(tmp_path))
    assert msg is not None and "no agent" in msg


def test_prompt_problem_reports_primary_agent_problem(tmp_path) -> None:
    cfg = make_cfg(
        tmp_path,
        primary_agent="codex",
        agents={"codex": {"enabled": False}, "claude": {"enabled": True}},
    )
    msg = prompt_problem(cfg)
    assert msg is not None and "codex" in msg and "not enabled" in msg


def test_prompted_run_renames_then_execs_with_flags() -> None:
    run = prompted_run("claude --model x", '"$(cat "$JAILBEE_INITIAL_PROMPT_FILE")"')
    assert run == (
        f"if [ -f {PROMPT_PATH} ]; then mv -f {PROMPT_PATH} {CONSUMED_PATH} && "
        f'exec claude --model x "$(cat "$JAILBEE_INITIAL_PROMPT_FILE")"; fi; exec claude --model x'
    )


def test_prompted_run_unchanged_without_report_exit() -> None:
    expected = (
        f"if [ -f {PROMPT_PATH} ]; then mv -f {PROMPT_PATH} {CONSUMED_PATH} && "
        f"exec claude P; fi; exec claude"
    )
    assert prompted_run("claude", "P") == expected
    assert prompted_run("claude", "P", report_exit=False, agent="claude") == expected


def _run_reporting(tmp_path, monkeypatch, rc: int, *, prompt: bool):
    import jailbee.initial_prompt as ip

    prompt_file = tmp_path / "prompt.md"
    monkeypatch.setattr(ip, "PROMPT_PATH", str(prompt_file))
    monkeypatch.setattr(ip, "CONSUMED_PATH", str(tmp_path / "consumed.md"))
    if prompt:
        prompt_file.write_text("hi")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "inbox.log"
    inbox = bindir / "jailbee-inbox"
    inbox.write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> {log}\necho noise\n'
    )
    inbox.chmod(0o755)
    agent = tmp_path / "agent"
    agent.write_text("#!/bin/sh\nexit $STUB_RC\n")
    agent.chmod(0o755)
    monkeypatch.setattr(ip, "AGENT_PID_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setattr(ip, "AGENT_EXIT_PATH", str(tmp_path / "agent.exit"))
    run = ip.prompted_run(str(agent), "ARG", report_exit=True, agent="claude")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_RC": str(rc)}
    proc = subprocess.run(
        ["bash", "-c", run], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    sent = log.read_text().splitlines() if log.exists() else []
    return proc, sent


@pytest.mark.parametrize("rc", [0, 1, 3, 127, 128, 137, 143])
def test_prompted_run_report_exit_runs_bash(tmp_path, monkeypatch, rc) -> None:
    import json

    proc, sent = _run_reporting(tmp_path, monkeypatch, rc, prompt=True)
    assert proc.returncode == rc
    assert proc.stdout == ""
    assert sent == []  # The broker owns delivery and intentional-stop suppression.
    assert (tmp_path / "agent.exit").read_text().strip() == str(rc)
    runtime = json.loads((tmp_path / "agent.json").read_text())
    assert runtime["pid"] > 1
    assert runtime["start"]
    assert runtime["boot"]
    assert runtime["agent"] == "claude"


def test_prompted_run_report_exit_without_prompt_file_execs_plain(tmp_path, monkeypatch) -> None:
    proc, sent = _run_reporting(tmp_path, monkeypatch, 3, prompt=False)
    assert proc.returncode == 3
    assert sent == []


def test_prompted_run_report_exit_preserves_terminal_input(tmp_path, monkeypatch) -> None:
    run = prompted_run("c", "P", report_exit=True, agent="claude")
    assert "&" not in run.replace("&&", "")
    assert 'exit "$rc"' in run


def test_prompted_run_report_exit_quotes_agent_name() -> None:
    run = prompted_run("c", "P", report_exit=True, agent="we ird")
    assert "'we ird'" in run


def test_write_prompt_sends_text_on_stdin_and_sets_label(mocker) -> None:
    incus = mocker.MagicMock()
    text = 'say "hi" $(rm -rf /) `x`\nline2'
    write_prompt(incus, "c1", text, uid=1000, gid=1001)
    args, kwargs = incus.exec_with_input.call_args
    assert args[0] == "c1"
    argv = args[1]
    assert text not in " ".join(argv)
    assert "umask 077" in argv[2] and "mv -f" in argv[2]
    assert argv[-1] == PROMPT_PATH
    assert args[2] == text
    assert kwargs == {"uid": 1000, "gid": 1001, "timeout": 30}
    incus.config_set.assert_called_once_with("c1", INITIAL_PROMPT_LABEL, PENDING)
    names = [c[0] for c in incus.mock_calls]
    assert names.index("exec_with_input") < names.index("config_set")


def test_pending_false_without_label_and_no_exec(mocker) -> None:
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert prompt_pending(incus, "c1", uid=1000) is False
    incus.exec.assert_not_called()


def test_pending_true_when_label_and_file(mocker) -> None:
    incus = mocker.MagicMock()
    incus.config_get.return_value = PENDING
    incus.exec.return_value = "yes\n"
    assert prompt_pending(incus, "c1", uid=1000) is True
    incus.config_set.assert_not_called()


def test_pending_flips_label_when_file_gone(mocker) -> None:
    incus = mocker.MagicMock()
    incus.config_get.return_value = PENDING
    incus.exec.return_value = "no\n"
    assert prompt_pending(incus, "c1", uid=1000) is False
    incus.config_set.assert_called_once_with("c1", INITIAL_PROMPT_LABEL, CONSUMED)


def test_pending_false_for_consumed_label(mocker) -> None:
    incus = mocker.MagicMock()
    incus.config_get.return_value = CONSUMED
    assert prompt_pending(incus, "c1", uid=1000) is False
    incus.exec.assert_not_called()


def test_pending_false_when_probe_fails(mocker) -> None:
    incus = mocker.MagicMock()
    incus.config_get.return_value = PENDING
    incus.exec.side_effect = IncusError("stopped")
    assert prompt_pending(incus, "c1", uid=1000) is False
    incus.config_set.assert_not_called()


def test_prompt_problem_checks_claude_under_runner(tmp_path) -> None:
    from jailbee.orchestration.runner import Runner

    cfg = make_cfg(
        tmp_path,
        primary_agent="codex",
        agents={"codex": {"enabled": True}, "claude": {"enabled": True}},
    )
    runner = Runner(agent="claude")
    assert prompt_problem(cfg, runner) is None
    from tests.conftest import with_agent

    no_args = with_agent(cfg, "claude", prompt_args="")
    msg = prompt_problem(no_args, runner)
    assert msg is not None and "claude" in msg and "prompt_args" in msg
    no_codex_args = with_agent(cfg, "codex", prompt_args="")
    assert prompt_problem(no_codex_args, runner) is None
    assert prompt_problem(no_codex_args) is not None


def test_read_runner_parses_label(mocker) -> None:
    from unittest.mock import MagicMock

    from jailbee.orchestration import labels
    from jailbee.orchestration.runner import Runner, read_runner

    incus = MagicMock()
    incus.config_get.return_value = Runner(agent="claude", model="haiku").to_label()
    assert read_runner(incus, "c1") == Runner(agent="claude", model="haiku")
    incus.config_get.assert_called_once_with("c1", labels.RUNNER)
    incus.config_get.return_value = None
    assert read_runner(incus, "c1") is None
