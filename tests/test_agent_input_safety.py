"""Session identity and foreground ownership gate terminal writes."""

import json
from unittest.mock import Mock

import pytest

from jailbee import agent_input
from jailbee.accounts.adapters.base import get_adapter


@pytest.fixture
def setup_session(tmp_path, monkeypatch):
    adapter = get_adapter("claude")
    monkeypatch.setattr(type(adapter), "session_home", lambda self, cfg, name: tmp_path)
    monkeypatch.setattr(agent_input, "wired_adapters", lambda cfg: [adapter])
    directory = tmp_path / "sessions"
    directory.mkdir()
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Running"}]
    return directory, incus


def record(directory, filename, **extra):
    data = {"pid": 42, "procStart": 123, "status": "idle", "sessionId": "old", "updatedAt": 1}
    data.update(extra)
    (directory / filename).write_text(json.dumps(data))


@pytest.mark.parametrize("process", ["42 10 123 T 42 10 34816", "42 10 123 S 42 10 34816"])
def test_suspended_or_background_session_never_receives_input(setup_session, process):
    directory, incus = setup_session
    record(directory, "one.json")
    incus.exec.side_effect = [process + "\n10 1 22 S 10 10 34816", "%1 10 4", "────\n> \n────\nshell draft"]
    assert agent_input.send(Mock(), incus, "box", "old", "text").code != "delivered"
    incus.exec_with_input.assert_not_called()


def test_cursor_outside_prompt_refuses_stale_frame(setup_session):
    directory, incus = setup_session
    record(directory, "one.json")
    incus.exec.side_effect = ["42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816", "%1 10 3", "────\n> \n────\nshell draft"]
    assert agent_input.send(Mock(), incus, "box", "old", "text").code == "unsent_terminal_input"
    incus.exec_with_input.assert_not_called()


def test_latest_session_record_wins_before_session_id_lookup(setup_session):
    directory, incus = setup_session
    record(directory, "old.json")
    record(directory, "new.json", sessionId="new", status="waiting", waitingFor="permission", updatedAt=2)
    incus.exec.return_value = "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816"
    assert agent_input.send(Mock(), incus, "box", "old", "text").code == "no_live_session"
    assert agent_input.send(Mock(), incus, "box", "new", "text").code == "waiting_at_dialog"
    incus.exec_with_input.assert_not_called()


def test_equal_age_conflicting_session_records_refuse(setup_session):
    directory, incus = setup_session
    record(directory, "old.json")
    record(directory, "new.json", sessionId="new", status="waiting", waitingFor="permission")
    incus.exec.return_value = "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816"
    assert agent_input.send(Mock(), incus, "box", "old", "text").code == "no_live_session"
    assert agent_input.send(Mock(), incus, "box", "new", "text").code == "no_live_session"
    incus.exec_with_input.assert_not_called()
