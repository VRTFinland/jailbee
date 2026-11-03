"""Failure handling for inbox send publication."""

import fcntl
import os
from pathlib import Path

import pytest

from jailbee.inbox import cli, send


def test_send_id_exhaustion_exits_one_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_DIR", str(tmp_path))
    monkeypatch.setattr(send, "new_id", lambda _: "fixed")
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "fixed.json").write_text("existing")

    assert cli.main(["send", "message", "--text", "hello"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "jailbee inbox: could not find a free manifest id\n"
    assert (outbox / "fixed.json").read_text() == "existing"


@pytest.mark.parametrize("stage", ["body", "manifest"])
def test_send_publish_failure_cleans_files_and_releases_lock(
    tmp_path, monkeypatch, capsys, stage
):
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_DIR", str(tmp_path))
    monkeypatch.setattr(send, "new_id", lambda _: "fixed")
    original = Path.replace

    def fail_replace(path, target):
        if path.name == f".fixed.{'md' if stage == 'body' else 'json'}.tmp":
            raise OSError(13, "Permission denied", str(target))
        return original(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    assert cli.main(["send", "message", "--text", "hello", "--wait"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "Permission denied" in captured.err
    if stage == "manifest":
        assert "jailbee inbox: could not write the manifest\n" in captured.err
    assert list((tmp_path / "outbox").iterdir()) == []
    fd = os.open(tmp_path / "status-waits" / "fixed.lock", os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
