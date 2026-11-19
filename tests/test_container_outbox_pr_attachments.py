"""Review commands capture local attachments into their draft or summary."""

from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.container_outbox import attachments, broker, pr_cmds


def test_review_add_captures_options_after_snapshot(monkeypatch, tmp_path):
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    monkeypatch.setattr(
        pr_cmds,
        "_view",
        lambda _: type("View", (), {"number": 12, "repo": "a/b", "head_sha": "a" * 40})(),
    )
    events = []
    monkeypatch.setattr(broker, "draft_list", lambda kind: [{"name": "review-12"}])
    monkeypatch.setattr(
        broker, "draft_show", lambda *args: events.append("snapshot") or {"revision": 3}
    )
    monkeypatch.setattr(
        attachments,
        "capture_inputs",
        lambda value, base: (
            events.append((value, base)) or {"attachments": {"screen": {"blob": "x"}}}
        ),
    )
    monkeypatch.setattr(broker, "draft_append", lambda *args, **kwargs: events.append(kwargs))
    result = CliRunner().invoke(
        container_app,
        [
            "pr",
            "review",
            "add",
            "12",
            "--file",
            "a.py",
            "--line",
            "2",
            "--body",
            "![x](attachment:screen)",
            "--attach",
            "screen=one.png",
        ],
    )
    assert result.exit_code == 0, result.output
    assert events[0] == "snapshot"
    assert events[1][0] == {"attachments": {"screen": {"file": "one.png"}}}
    assert events[2]["expected_revision"] == 3
    assert events[2]["attachments"] == {"screen": {"blob": "x"}}


def test_review_submit_captures_multiple_options(monkeypatch):
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    monkeypatch.setattr(
        pr_cmds,
        "_view",
        lambda _: type("View", (), {"number": 12, "repo": "a/b", "head_sha": "a" * 40})(),
    )
    captures = []
    monkeypatch.setattr(
        attachments, "capture_inputs", lambda value, base: captures.append(value) or value
    )
    calls = []
    monkeypatch.setattr(
        broker, "draft_submit", lambda *args, **kwargs: calls.append(kwargs) or "001-review.json"
    )
    result = CliRunner().invoke(
        container_app,
        [
            "pr",
            "review",
            "submit",
            "12",
            "--body",
            "summary",
            "--attach",
            "one=a.png",
            "--attach",
            "two=b.png",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captures == [{"attachments": {"one": {"file": "a.png"}, "two": {"file": "b.png"}}}]
    assert calls[0]["attachments"] == captures[0]["attachments"]
