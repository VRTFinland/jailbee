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
    incus.exec.side_effect = [
        process + "\n10 1 22 S 10 10 34816",
        "%1 10",
    ]
    assert agent_input.send(Mock(), incus, "box", "old", "text").code != "delivered"
    incus.exec_with_input.assert_not_called()


def test_latest_session_record_wins_before_session_id_lookup(setup_session):
    directory, incus = setup_session
    record(directory, "old.json")
    record(
        directory,
        "new.json",
        sessionId="new",
        status="waiting",
        waitingFor="permission",
        updatedAt=2,
    )
    incus.exec.return_value = "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816"
    assert agent_input.send(Mock(), incus, "box", "old", "text").code == "no_live_session"
    assert agent_input.send(Mock(), incus, "box", "new", "text").code == "no_pane"
    incus.exec_with_input.assert_not_called()


@pytest.mark.parametrize(("session_id", "pane"), [("old", "%1"), ("second", "%2")])
def test_multiple_sessions_route_to_their_nested_pane(setup_session, session_id, pane):
    directory, incus = setup_session
    record(directory, "first.json")
    record(directory, "second.json", pid=52, procStart=456, sessionId="second")
    tree = (
        "42 30 123 S 10 10 34816\n30 10 88 S 10 10 34816\n"
        "10 1 22 S 10 10 34816\n52 20 456 S 20 20 34817\n"
        "20 1 33 S 20 20 34817\n"
    )
    incus.exec.side_effect = [tree, "%1 10\n%2 20\n"] * 2 + ["", "", ""]
    text = "hello\nmaailma ä"
    assert agent_input.send(Mock(), incus, "box", session_id, text).code == "delivered"
    assert incus.exec_with_input.call_args.args[2].encode("utf-8") == text.encode("utf-8")
    commands = [call.args[1][-1] for call in incus.exec.call_args_list]
    assert "tmux paste-buffer -pr -b " in commands[4]
    assert commands[4].endswith(f" -t {pane}")
    assert commands[5] == f"tmux send-keys -t {pane} Enter"
    assert commands[6].startswith("tmux delete-buffer -b jailbee-")


def test_send_strips_terminal_control_sequences(setup_session):
    directory, incus = setup_session
    record(directory, "one.json")
    incus.exec.side_effect = [
        "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816",
        "%1 10",
    ] * 2 + ["", "", ""]
    assert (
        agent_input.send(
            Mock(), incus, "box", "old", "x\x1b[201~\r\x03\x1b[200~\n\tok\x7f\x85"
        ).code
        == "delivered"
    )
    assert incus.exec_with_input.call_args.args[2] == "x[201~[200~\n\tok"


def test_concurrent_sends_do_not_interleave(setup_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    directory, incus = setup_session
    record(directory, "one.json")
    entered, release, second_pasted = Event(), Event(), Event()
    operations = []

    def execute(_container, command):
        cmd = command[-1]
        if cmd.startswith("import os"):
            return "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816"
        if "list-panes" in cmd:
            return "%1 10"
        if "paste-buffer" in cmd:
            operations.append("paste")
            if len(operations) == 1:
                entered.set()
                assert release.wait(5)
            else:
                second_pasted.set()
        elif "send-keys" in cmd:
            operations.append("enter")
        return ""

    incus.exec.side_effect = execute
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(agent_input.send, Mock(), incus, "box", "old", "one")
        assert entered.wait(5)
        second = pool.submit(agent_input.send, Mock(), incus, "box", "old", "two")
        second_pasted.wait(0.5)
        release.set()
        assert first.result().code == second.result().code == "delivered"
    assert operations == ["paste", "enter", "paste", "enter"]


def test_send_retries_when_pane_changes_under_lock(setup_session):
    directory, incus = setup_session
    record(directory, "one.json")
    tree = "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816"
    incus.exec.side_effect = [
        tree,
        "%1 10",
        tree,
        "%2 10",
        tree,
        "%2 10",
        "",
        "",
        "",
    ]
    assert agent_input.send(Mock(), incus, "box", "old", "hello").code == "delivered"
    commands = [call.args[1][-1] for call in incus.exec.call_args_list]
    assert any("paste-buffer" in command and command.endswith("-t %2") for command in commands)
    assert not any("paste-buffer" in command and command.endswith("-t %1") for command in commands)


def test_lock_storage_is_bounded_across_many_panes(setup_session, monkeypatch, tmp_path):
    directory, incus = setup_session
    record(directory, "one.json")
    monkeypatch.setattr("jailbee.state_service.paths.ensure_runtime_dir", lambda: tmp_path)
    pane = [0]

    def execute(_container, command):
        cmd = command[-1]
        if cmd.startswith("import os"):
            return "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816"
        if "list-panes" in cmd:
            return f"%{pane[0]} 10"
        return ""

    incus.exec.side_effect = execute
    for pane[0] in range(300):
        assert agent_input.send(Mock(), incus, "box", "old", "text").code == "delivered"
    assert len(list(tmp_path.rglob("*.lock"))) <= 256


def test_dialog_delivery_warns_without_disabling_chat(setup_session):
    directory, incus = setup_session
    record(directory, "dialog.json", status="waiting", waitingFor="permission")
    tree = "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816"
    incus.exec.side_effect = [tree, "%1 10"] * 3 + ["", "", ""]
    assert agent_input.availability(Mock(), incus, "box", "old").code == "available"
    result = agent_input.send(Mock(), incus, "box", "old", "text")
    assert result.code == "delivered_with_warning"
    assert result.message == "The text went to an open terminal dialog; check it there."
    assert incus.exec_with_input.call_args.args[2] == "text"


def test_equal_age_conflicting_session_records_refuse(setup_session):
    directory, incus = setup_session
    record(directory, "old.json")
    record(directory, "new.json", sessionId="new", status="waiting", waitingFor="permission")
    incus.exec.return_value = "42 10 123 S 42 42 34816\n10 1 22 S 10 42 34816"
    assert agent_input.send(Mock(), incus, "box", "old", "text").code == "no_live_session"
    assert agent_input.send(Mock(), incus, "box", "new", "text").code == "no_live_session"
    incus.exec_with_input.assert_not_called()
