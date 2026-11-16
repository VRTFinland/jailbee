"""A manifest-file stage command captures bodies and images through the broker."""

import json

from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.container_outbox import broker


def test_stage_manifest_resolves_inputs_relative_to_manifest(monkeypatch, tmp_path):
    from jailbee.container_outbox import attachments

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    folder = tmp_path / "proposal"
    folder.mkdir()
    (folder / "body.md").write_text("![Evidence](attachment:screen)")
    value = {
        "version": 1,
        "attachments": {"screen": {"file": "screen.png"}},
        "actions": [{"type": "comment", "repo": ".", "issue": 7, "body_file": "body.md"}],
    }
    path = folder / "proposal.json"
    path.write_text(json.dumps(value))
    captures = []
    calls = []
    monkeypatch.setattr(
        attachments, "capture_inputs", lambda m, base_dir: captures.append(base_dir) or m
    )
    monkeypatch.setattr(broker, "put", lambda *args: calls.append(args) or "001-evidence.json")
    result = CliRunner().invoke(
        container_app, ["outbox", "stage", "issue", str(path), "--slug", "evidence"]
    )
    assert result.exit_code == 0, result.output
    assert captures == [folder]
    assert calls[0][0:2] == ("issue", "evidence")
    assert list(calls[0][3].values()) == ["![Evidence](attachment:screen)"]
    assert "001-evidence.json" in result.output


def test_stage_pr_manifest(monkeypatch, tmp_path):
    from jailbee.container_outbox import attachments

    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    monkeypatch.setattr(attachments, "capture_inputs", lambda value, base: value)
    calls = []
    monkeypatch.setattr(broker, "put", lambda *args: calls.append(args) or "001.json")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"repo": ".", "pr": 7, "head_sha": "abc"}))
    result = CliRunner().invoke(container_app, ["outbox", "stage", "pr", str(path)])
    assert result.exit_code == 0
    assert calls[0][0] == "pr"
    assert "pr/001.json" in result.output


def test_stage_can_append_images_to_an_existing_issue_draft(monkeypatch, tmp_path):
    from jailbee.container_outbox import attachments

    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "append.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "actions": [
                    {"type": "comment", "repo": ".", "issue": 7, "body": "![x](attachment:screen)"}
                ],
            }
        )
    )
    monkeypatch.setattr(attachments, "capture_inputs", lambda value, base: value)
    monkeypatch.setattr(broker, "draft_list", lambda kind: [{"name": "evidence"}])
    monkeypatch.setattr(broker, "draft_show", lambda *args: {"revision": 3})
    calls = []
    monkeypatch.setattr(
        broker, "draft_append_many", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    result = CliRunner().invoke(
        container_app, ["outbox", "stage", "issue", str(path), "--batch", "evidence"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1]["expected_revision"] == 3
    assert calls[0][0][1] == "evidence"


def test_child_cannot_stage_manifest_or_begin_capture(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    monkeypatch.setattr(
        broker, "put", lambda *args: (_ for _ in ()).throw(AssertionError("staged"))
    )
    result = CliRunner().invoke(
        container_app, ["outbox", "stage", "issue", str(tmp_path / "absent.json")]
    )
    assert result.exit_code == 1
    assert "child" in result.output.lower()


def test_invalid_inputs_do_not_begin_capture(monkeypatch, tmp_path):
    from jailbee.container_outbox import attachments

    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    monkeypatch.setattr(
        attachments,
        "capture_inputs",
        lambda *args: (_ for _ in ()).throw(AssertionError("captured")),
    )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"actions": [{"body_file": "missing"}]}))
    runner = CliRunner()
    assert runner.invoke(container_app, ["outbox", "stage", "issue", str(path)]).exit_code == 2
    assert (
        runner.invoke(
            container_app, ["outbox", "stage", "issue", str(path), "--slug", "BAD"]
        ).exit_code
        == 2
    )
    path.write_text(" " * (256 * 1024 + 1))
    assert runner.invoke(container_app, ["outbox", "stage", "issue", str(path)]).exit_code == 2


def test_body_symlink_and_fifo_rejected(monkeypatch, tmp_path):
    import os

    from jailbee.container_outbox import attachments

    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    monkeypatch.setattr(
        attachments,
        "capture_inputs",
        lambda *args: (_ for _ in ()).throw(AssertionError("captured")),
    )
    (tmp_path / "regular").write_text("text")
    (tmp_path / "link").symlink_to(tmp_path / "regular")
    os.mkfifo(tmp_path / "fifo")
    path = tmp_path / "manifest.json"
    for name in ("link", "fifo"):
        path.write_text(json.dumps({"actions": [{"body_file": name}]}))
        assert (
            CliRunner().invoke(container_app, ["outbox", "stage", "issue", str(path)]).exit_code
            == 2
        )
