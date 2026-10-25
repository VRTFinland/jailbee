from __future__ import annotations

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
