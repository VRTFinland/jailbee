"""Failure handling for inbox send publication."""

import fcntl
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jailbee.inbox import cli, send


def test_help_documents_model_suffix(capsys):
    assert cli.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "Listed model tiers also accept the [1m] suffix." in output
    assert "haiku (optional [1m])" in output


# The Python implementation intentionally accepts [1m]; the shell reference does not.
@pytest.mark.parametrize("agent", ["claude", "claude-jb"])
@pytest.mark.parametrize("model", ["opus[1m]", "sonnet[1m]"])
def test_send_accepts_suffixed_model(tmp_path, monkeypatch, agent, model):
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_DIR", str(tmp_path))
    assert cli.main(["send", "spawn", "--branch", "b", "--text", "x", "--agent", agent, "--model", model]) == 0
    manifest = next((tmp_path / "outbox").glob("*.json"))
    assert json.loads(manifest.read_text())["runner"] == {"agent": agent, "model": model}


@pytest.mark.parametrize("model", ["OPUS[1m]", "opus[2m]", "opus[1M]", "opus [1m]"])
def test_send_rejects_invalid_model_suffix(tmp_path, monkeypatch, model):
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_DIR", str(tmp_path))
    assert cli.main(["send", "spawn", "--branch", "b", "--text", "x", "--agent", "claude", "--model", model]) == 2
    assert not list((tmp_path / "outbox").glob("*"))


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
def test_send_publish_failure_cleans_files_and_releases_lock(tmp_path, monkeypatch, capsys, stage):
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


class BoundedReader(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 65536, f"unbounded read: {size}"
        return super().read(size)


@pytest.mark.parametrize("source", ["stdin", "file"])
def test_send_reads_large_body_in_bounded_chunks(tmp_path, monkeypatch, capsys, source):
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_DIR", str(tmp_path))
    payload = b"a" * 150001
    if source == "stdin":
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BoundedReader(payload)))
        args = ["-"]
    else:
        path = tmp_path / "input"
        path.write_bytes(payload)
        original = Path.open

        def bounded_open(p, *args, **kwargs):
            if p == path:
                return BoundedReader(payload)
            return original(p, *args, **kwargs)

        monkeypatch.setattr(Path, "open", bounded_open)
        args = ["--file", str(path)]
    assert cli.main(["send", "message", *args]) == 2
    assert "the message text is 150001 bytes; the limit is 65536" in capsys.readouterr().err
    assert list((tmp_path / "outbox").iterdir()) == []


def test_send_reserves_and_publishes_body_before_manifest(tmp_path, monkeypatch):
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    monkeypatch.setattr(send, "new_id", lambda _: "fixed")
    original = Path.replace
    observed = []

    def check_order(path, target):
        if path.name == ".fixed.json.tmp":
            assert (settings.outbox / "fixed.md").read_bytes() == b"hello"
            fd = os.open(settings.base / "status-waits" / "fixed.lock", os.O_WRONLY)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            observed.append(True)
        return original(path, target)

    monkeypatch.setattr(Path, "replace", check_order)
    before = len(send._reservations)
    try:
        assert (
            send.publish(settings, send.parse_send(["message", "--text", "hello", "--wait"]))
            == "fixed"
        )
        assert observed == [True]
    finally:
        for fd in send._reservations[before:]:
            os.close(fd)
        del send._reservations[before:]
