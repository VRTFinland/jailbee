from pathlib import Path

from jailbee.container_outbox.role import is_child


def test_role_detection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    marker = tmp_path / "role"
    assert is_child({"JAILBEE_ROLE": "child"}, marker)
    assert not is_child({}, marker)
    marker.write_text("child\n")
    assert is_child({}, marker)
    marker.write_text("orchestrator\n")
    assert not is_child({}, marker)
