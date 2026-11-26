"""Host agent say command routes text and outcomes through the input seam."""

from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from jailbee import agent_input, cli
from jailbee.accounts.models import AgentSession


@pytest.fixture
def cli_seams(monkeypatch):
    monkeypatch.setattr(cli, "_load_or_exit", lambda _: Mock())
    monkeypatch.setattr(cli, "_resolve_existing", lambda cfg, name: (Mock(), "box"))
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


def test_say_reports_distinct_refusal(cli_seams):
    delivered, _ = cli_seams
    delivered.return_value = agent_input.Outcome("no_pane", "No pane")
    result = CliRunner().invoke(cli.app, ["agent", "say", "box", "hello", "--session", "abc"])
    assert result.exit_code == 6
    assert "No pane" in result.output
