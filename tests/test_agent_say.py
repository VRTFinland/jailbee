"""Host agent say command routes text and outcomes through the input seam."""

from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from jailbee import agent_input, cli
from jailbee.accounts.models import AgentSession


@pytest.fixture
def cli_seams(monkeypatch):
    monkeypatch.setattr(cli, "_load_or_exit", lambda _: Mock())
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Running"}]
    monkeypatch.setattr(cli, "_resolve_existing", lambda cfg, name: (incus, "box"))
    sessions = [AgentSession("claude", 42, 10, "idle", None, None, None, "abc")]
    monkeypatch.setattr(agent_input, "live_sessions", lambda *args: sessions)
    delivered = Mock(return_value=agent_input.Outcome("delivered", "Delivered"))
    monkeypatch.setattr(agent_input, "send", delivered)
    return delivered, sessions


def test_say_uses_one_live_session_and_stdin(cli_seams):
    delivered, _ = cli_seams
    result = CliRunner().invoke(cli.app, ["agent", "say", "box", "-"], input="hello\nworld")
    assert result.exit_code == 0, result.output
    assert delivered.call_args.args[3:] == ("abc", "hello\nworld")
    assert "Using session abc" in result.output


def test_say_requires_session_when_several_live(cli_seams):
    delivered, sessions = cli_seams
    sessions.append(AgentSession("claude", 43, 11, "idle", None, None, None, "def"))
    result = CliRunner().invoke(cli.app, ["agent", "say", "box", "hello"])
    assert result.exit_code == 2
    assert "abc" in result.output and "def" in result.output
    delivered.assert_not_called()


@pytest.mark.parametrize(("code", "exit_code"), [
    ("container_not_running", 3), ("no_live_session", 4), ("unsupported_agent", 5),
    ("no_pane", 6), ("waiting_at_dialog", 7), ("unsent_terminal_input", 8),
    ("delivery_error", 9),
])
def test_say_reports_distinct_refusal(cli_seams, code, exit_code):
    delivered, _ = cli_seams
    delivered.return_value = agent_input.Outcome(code, "refused")
    result = CliRunner().invoke(cli.app, ["agent", "say", "box", "hello", "--session", "abc"])
    assert result.exit_code == exit_code
    assert "refused" in result.output


def test_say_interactively_selects_session_and_asks_for_text(cli_seams, monkeypatch):
    from jailbee import prompting

    delivered, sessions = cli_seams
    sessions.append(AgentSession("claude", 43, 11, "idle", None, None, None, "def"))
    monkeypatch.setattr(prompting, "is_interactive", lambda: True)
    monkeypatch.setattr(prompting, "_select", lambda noun, options, **kw: "def")
    monkeypatch.setattr(prompting, "_ask", lambda *args, **kwargs: "chosen text")
    result = CliRunner().invoke(cli.app, ["agent", "say", "box"])
    assert result.exit_code == 0, result.output
    assert delivered.call_args.args[3:] == ("def", "chosen text")


def test_say_missing_text_noninteractive(cli_seams):
    delivered, _ = cli_seams
    result = CliRunner().invoke(cli.app, ["agent", "say", "box", "--session", "abc"])
    assert result.exit_code == 2
    assert "missing text" in result.output.lower()
    delivered.assert_not_called()


def test_say_missing_container_noninteractive_lists_candidates(monkeypatch):
    from jailbee import lifecycle
    from jailbee.incus import Incus

    monkeypatch.setattr(cli, "_load_or_exit", lambda _: Mock(container_prefix="p"))
    containers = [lifecycle.ContainerInfo("p-one", "Running", None, None, None),
                  lifecycle.ContainerInfo("p-two", "Running", None, None, None)]
    monkeypatch.setattr(lifecycle, "list_containers", lambda *args, **kw: containers)
    monkeypatch.setattr(Incus, "__init__", lambda self: None)
    result = CliRunner().invoke(cli.app, ["agent", "say"])
    assert result.exit_code == 2
    assert "one" in result.output and "two" in result.output


def test_say_missing_container_interactive_uses_selection(monkeypatch):
    from jailbee import lifecycle, prompting, tui
    from jailbee.incus import Incus

    monkeypatch.setattr(cli, "_load_or_exit", lambda _: Mock(container_prefix="p"))
    containers = [lifecycle.ContainerInfo("p-one", "Running", None, None, None),
                  lifecycle.ContainerInfo("p-two", "Running", None, None, None)]
    monkeypatch.setattr(lifecycle, "list_containers", lambda *args, **kw: containers)
    monkeypatch.setattr(Incus, "__init__", lambda self: None)
    monkeypatch.setattr(Incus, "list_containers", lambda self: [{"name": "p-two", "status": "Running"}])
    monkeypatch.setattr(prompting, "is_interactive", lambda: True)
    monkeypatch.setattr(tui, "pick_container", lambda rows: "p-two")
    monkeypatch.setattr(prompting, "_ask", lambda *args, **kwargs: "prompt text")
    monkeypatch.setattr(agent_input, "live_sessions", lambda *args: [
        AgentSession("claude", 42, 10, "idle", None, None, None, "abc")])
    delivered = Mock(return_value=agent_input.Outcome("delivered", "Delivered"))
    monkeypatch.setattr(agent_input, "send", delivered)
    result = CliRunner().invoke(cli.app, ["agent", "say"])
    assert result.exit_code == 0, result.output
    assert delivered.call_args.args[2:] == ("p-two", "abc", "prompt text")


def test_say_empty_stdin_is_refused(cli_seams):
    delivered, _ = cli_seams
    result = CliRunner().invoke(cli.app, ["agent", "say", "box", "-", "--session", "abc"], input="  ")
    assert result.exit_code == 2
    assert "empty" in result.output.lower()
    delivered.assert_not_called()
