"""Delivery refuses unsafe or stale session records before touching tmux input."""

from unittest.mock import Mock

import pytest

from jailbee import agent_input
from jailbee.accounts.models import AgentSession
from jailbee.incus import IncusError


@pytest.mark.parametrize(
    ("sessions", "tree", "panes", "expected"),
    [
        ([], "42 10 123 S 42 42 34816\n", "%1 10 1", "no_live_session"),
        (
            [AgentSession("claude", 42, 99, "idle", None, None, None, "id")],
            "42 10 123 S 42 42 34816\n",
            "%1 10 1",
            "no_live_session",
        ),
        (
            [AgentSession("other", 42, 123, "idle", None, None, None, "id")],
            "42 10 123 S 42 42 34816\n",
            "%1 10 1",
            "unsupported_agent",
        ),
        (
            [AgentSession("claude", 42, 123, "waiting", "permission", None, None, "id")],
            "42 10 123 S 42 42 34816\n",
            "%1 10 1",
            "waiting_at_dialog",
        ),
        (
            [AgentSession("claude", 42, 123, "idle", None, None, None, "id")],
            "42 10 123 S 42 42 34816\n10 1 20 S 10 42 34816\n",
            "%2 11 1",
            "no_pane",
        ),
    ],
)
def test_refusals_do_not_paste(monkeypatch, sessions, tree, panes, expected):
    adapter = Mock()
    adapter.read_sessions.return_value = sessions
    monkeypatch.setattr(agent_input, "wired_adapters", lambda cfg: [adapter])
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Running"}]
    incus.exec.side_effect = [tree, panes]
    assert agent_input.send(Mock(), incus, "box", "id", "text").code == expected
    incus.exec_with_input.assert_not_called()


def test_successful_delivery_survives_buffer_cleanup_failure(monkeypatch):
    adapter = Mock()
    adapter.read_sessions.return_value = [
        AgentSession("claude", 42, 123, "idle", None, None, None, "id")
    ]
    monkeypatch.setattr(agent_input, "wired_adapters", lambda cfg: [adapter])
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Running"}]
    incus.exec.side_effect = [
        "42 10 123 S 42 42 34816\n10 1 20 S 10 42 34816\n",
        "%1 10 1",
        "────\n> \n────",
        "",
        "",
        IncusError("cleanup"),
    ]
    assert agent_input.send(Mock(), incus, "box", "id", "text").code == "delivered"


def test_cleanup_does_not_mask_delivery_failure(monkeypatch):
    adapter = Mock()
    adapter.read_sessions.return_value = [
        AgentSession("claude", 42, 123, "idle", None, None, None, "id")
    ]
    monkeypatch.setattr(agent_input, "wired_adapters", lambda cfg: [adapter])
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Running"}]
    incus.exec.side_effect = [
        "42 10 123 S 42 42 34816\n10 1 20 S 10 42 34816\n",
        "%1 10 1",
        "────\n> \n────",
        IncusError("paste failed"),
        IncusError("cleanup failed"),
    ]
    result = agent_input.send(Mock(), incus, "box", "id", "text")
    assert result.code == "delivery_error"
    assert "paste failed" in result.message


def test_missing_tmux_server_is_no_pane(monkeypatch):
    adapter = Mock()
    adapter.read_sessions.return_value = [
        AgentSession("claude", 42, 123, "idle", None, None, None, "id")
    ]
    monkeypatch.setattr(agent_input, "wired_adapters", lambda cfg: [adapter])
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Running"}]
    incus.exec.side_effect = ["42 10 123 S 42 42 34816\n", IncusError("no server")]
    assert agent_input.availability(Mock(), incus, "box", "id").code == "no_pane"
    incus.exec_with_input.assert_not_called()
