"""Agent input refuses to overwrite terminal drafts."""

from unittest.mock import Mock

import pytest

from jailbee.agent_input import send


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ("────────\n\u276f \n────────\nstatus", "delivered"),
        ("────────\n\u276f draft\n────────\nstatus", "unsent_terminal_input"),
        ("────────\n\u276f first\nsecond\n────────\nstatus", "unsent_terminal_input"),
        ("────────\n\u276f \nwrapped draft\n────────\nstatus", "unsent_terminal_input"),
        (
            "────────\n\u276f \n────────\nother\n────────\nunknown\n────────",
            "unsent_terminal_input",
        ),
        ("unknown screen", "unsent_terminal_input"),
    ],
)
def test_terminal_input_is_preserved(tmp_path, monkeypatch, frame, expected):
    from jailbee.accounts.adapters.base import get_adapter

    adapter = get_adapter("claude")
    home = tmp_path
    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / "session.json").write_text(
        '{"pid":42,"procStart":123,"status":"idle","sessionId":"abc"}'
    )
    monkeypatch.setattr(type(adapter), "session_home", lambda self, cfg, name: home)
    monkeypatch.setattr("jailbee.agent_input.wired_adapters", lambda cfg: [adapter])
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Running"}]
    incus.exec.side_effect = ["42 10 123\n10 1 22\n", "%1 10\n", frame, "", "", ""]
    result = send(Mock(), incus, "box", "abc", "hello\nmaailma ä")
    assert result.code == expected
    if expected == "delivered":
        assert incus.exec_with_input.call_args.args[2] == "hello\nmaailma ä"
        assert any("paste-buffer -pr" in call.args[1][-1] for call in incus.exec.call_args_list)
    else:
        incus.exec_with_input.assert_not_called()
