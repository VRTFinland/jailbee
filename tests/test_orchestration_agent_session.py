from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jailbee import initial_prompt
from jailbee.incus import IncusError
from jailbee.orchestration import agent_session
from jailbee.orchestration.runner import Runner
from tests.conftest import make_config, with_agent


@pytest.fixture
def cfg(tmp_path):
    return with_agent(make_config(tmp_path), "claude", enabled=True, prompt_args="prompt")


def test_kill_transport_error_prevents_prompt_overwrite(cfg, monkeypatch):
    incus = MagicMock()
    incus.config_get.return_value = "orch"
    incus.exec.side_effect = IncusError("transport lost")
    monkeypatch.setattr(agent_session.lifecycle, "container_repo_dir", lambda *a: "/repo")
    write = MagicMock()
    launch = MagicMock()
    monkeypatch.setattr(initial_prompt, "write_prompt", write)
    monkeypatch.setattr(agent_session.tmux, "launch_step", launch)
    with pytest.raises(IncusError, match="transport lost"):
        agent_session.relaunch(cfg, incus, "child", "task", runner=None, old_agent="codex")
    write.assert_not_called()
    launch.assert_not_called()


def test_relaunch_restores_session_after_last_window_is_killed(cfg, monkeypatch):
    incus = MagicMock()
    incus.config_get.return_value = "orch"
    monkeypatch.setattr(agent_session.lifecycle, "container_repo_dir", lambda *a: "/repo")
    monkeypatch.setattr(initial_prompt, "write_prompt", MagicMock())
    exists = True

    def kill(*args, **kwargs):
        nonlocal exists
        exists = False

    def ensure(*args, **kwargs):
        nonlocal exists
        exists = True

    def launch(*args, **kwargs):
        assert exists, "tmux session disappeared with last window"

    monkeypatch.setattr(agent_session.tmux, "kill_window", kill)
    monkeypatch.setattr(agent_session.tmux, "ensure_session", ensure)
    monkeypatch.setattr(agent_session.tmux, "launch_step", launch)
    agent_session.relaunch(cfg, incus, "child", "task", runner=None, old_agent="claude")


def test_relaunch_only_prompted_agent_with_shared_environment(tmp_path, monkeypatch):
    cfg = with_agent(
        make_config(tmp_path),
        "claude",
        enabled=True,
        prompt_args='"$(cat "$JAILBEE_INITIAL_PROMPT_FILE")"',
        env={"AGENT_VAR": "agent"},
    )
    cfg = with_agent(cfg, "codex", enabled=True, autostart=True)
    incus = MagicMock()
    incus.config_get.return_value = "orch"
    monkeypatch.setattr(agent_session.lifecycle, "container_repo_dir", lambda *args: "/repo")
    calls = []
    monkeypatch.setattr(
        agent_session.tmux, "kill_window", lambda *args, **kwargs: calls.append(("kill", args[-1]))
    )
    monkeypatch.setattr(
        initial_prompt, "write_prompt", lambda *args, **kwargs: calls.append(("prompt", args[2]))
    )
    launch = MagicMock(side_effect=lambda *args, **kwargs: calls.append(("launch", kwargs)))
    monkeypatch.setattr(agent_session.tmux, "launch_step", launch)
    agent_session.relaunch(
        cfg,
        incus,
        "proj-child",
        "new task",
        runner=Runner(agent="claude", model="opus"),
        old_agent="codex",
    )
    assert calls[0:3] == [("kill", "codex"), ("kill", "claude"), ("prompt", "new task")]
    launch.assert_called_once()
    kw = launch.call_args.kwargs
    assert kw["name"] == "claude" and kw["cwd"] == "/repo" and kw["background"]
    assert "--model opus" in kw["command"]
    assert initial_prompt.AGENT_PID_PATH in kw["command"]
    assert kw["env"]["JAILBEE_ROLE"] == "child"
    assert kw["env"]["AGENT_VAR"] == "agent"
    assert kw["env"]["REPO_DIR"] == "/repo"
    assert kw["env"][initial_prompt.PROMPT_ENV] == initial_prompt.CONSUMED_PATH


def test_relaunch_passes_resume(cfg, monkeypatch):
    incus = MagicMock()
    incus.config_get.return_value = "orch"
    monkeypatch.setattr(agent_session.lifecycle, "container_repo_dir", lambda *a: "/repo")
    monkeypatch.setattr(initial_prompt, "write_prompt", MagicMock())
    monkeypatch.setattr(agent_session.tmux, "kill_window", MagicMock())
    monkeypatch.setattr(agent_session.tmux, "ensure_session", MagicMock())
    launch = MagicMock()
    monkeypatch.setattr(agent_session.tmux, "launch_step", launch)
    session = "12345678-1234-1234-1234-123456789abc"
    agent_session.relaunch(
        cfg, incus, "child", "task", runner=None, old_agent="claude", resume=session
    )
    assert f"--resume {session}" in launch.call_args.kwargs["command"]
