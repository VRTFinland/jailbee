from pathlib import Path

from jailbee.container_outbox.role import CHILD_REFUSAL, is_child


def test_child_refusal_identifies_container_and_message_command() -> None:
    assert "child container" in CHILD_REFUSAL
    assert "jailbee inbox send message --text" in CHILD_REFUSAL


def test_default_environment_detects_child(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    assert is_child(role_file=tmp_path / "missing")
    monkeypatch.delenv("JAILBEE_ROLE")
    assert not is_child(role_file=tmp_path / "missing")


def test_role_detection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    marker = tmp_path / "role"
    assert is_child({"JAILBEE_ROLE": "child"}, marker)
    assert not is_child({}, marker)
    marker.write_text("child\n")
    assert is_child({}, marker)
    marker.write_text("orchestrator\n")
    assert not is_child({}, marker)
