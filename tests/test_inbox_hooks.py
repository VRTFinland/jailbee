import json

import pytest
from typer.testing import CliRunner

from jailbee.inbox import role
from jailbee.inbox.container_cmd import inbox_app


@pytest.fixture
def set_role(monkeypatch, tmp_path):
    marker = tmp_path / "role"
    monkeypatch.setattr(role, "ROLE_FILE", marker)
    monkeypatch.setattr(role, "LEGACY_CHILD_FILE", tmp_path / "legacy")
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)

    def set_value(value):
        marker.write_text(value)

    return set_value


@pytest.mark.parametrize("source", ["resume", "fork", "startup", "clear", "compact"])
@pytest.mark.parametrize("container_role", ["child", "orchestrator", "plain"])
def test_session_start(set_role, source, container_role):
    set_role(container_role)
    result = CliRunner().invoke(
        inbox_app, ["hook", "session-start"], input=json.dumps({"source": source})
    )
    assert result.exit_code == 0
    if source in {"resume", "fork"} and container_role != "plain":
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "restarted automatically" in context
        assert "task list" in context
    else:
        assert result.stdout == ""


@pytest.mark.parametrize("container_role", ["child", "orchestrator", "plain"])
@pytest.mark.parametrize(
    "payload,block",
    [
        ({"stop_hook_active": True, "background_tasks": []}, False),
        ({}, False),
        ({"background_tasks": [{"command": "jailbee inbox follow", "status": "running"}]}, False),
        (
            {
                "background_tasks": [
                    {"command": "jailbee inbox wait --timeout none", "status": "running"}
                ]
            },
            False,
        ),
        ({"background_tasks": []}, True),
        ({"background_tasks": [{"command": "jailbee inbox follow", "status": "completed"}]}, True),
    ],
)
def test_stop(set_role, container_role, payload, block):
    set_role(container_role)
    result = CliRunner().invoke(inbox_app, ["hook", "stop"], input=json.dumps(payload))
    assert result.exit_code == 0
    if container_role == "child" and block:
        output = json.loads(result.stdout)
        assert output["decision"] == "block"
        assert "jailbee inbox wait --timeout none" in output["reason"]
    else:
        assert result.stdout == ""


@pytest.mark.parametrize("command", ["stop", "session-start"])
def test_malformed_input(set_role, command):
    set_role("child")
    result = CliRunner().invoke(inbox_app, ["hook", command], input="{")
    assert result.exit_code == 0
    assert result.stdout == ""
