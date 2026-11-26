"""Agent input leaves prompt ownership and busy-turn queueing to Claude."""

from unittest.mock import Mock

import pytest

from jailbee.agent_input import send


@pytest.mark.parametrize("state", ["idle", "busy"])
def test_send_does_not_inspect_or_clear_terminal_input(tmp_path, monkeypatch, state):
    from jailbee.accounts.adapters.base import get_adapter

    adapter = get_adapter("claude")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "session.json").write_text(
        '{"pid":42,"procStart":123,"status":"' + state + '","sessionId":"abc"}'
    )
    monkeypatch.setattr(type(adapter), "session_home", lambda self, cfg, name: tmp_path)
    monkeypatch.setattr("jailbee.agent_input.wired_adapters", lambda cfg: [adapter])
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Running"}]

    def execute(_container, command):
        cmd = command[-1]
        if cmd.startswith("import os"):
            return "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816\n"
        if "list-panes" in cmd:
            return "%1 10\n"
        assert "capture-pane" not in cmd
        assert "send-keys" not in cmd or cmd == "tmux send-keys -t %1 Enter"
        return ""

    incus.exec.side_effect = execute
    result = send(Mock(), incus, "box", "abc", "hello\nmaailma ä")
    assert result.code == "delivered"
    assert incus.exec_with_input.call_args.args[2] == "hello\nmaailma ä"
    assert any("paste-buffer -pr" in call.args[1][-1] for call in incus.exec.call_args_list)
