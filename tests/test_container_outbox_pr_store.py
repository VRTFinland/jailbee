"""`jailbee outbox ls|show|drop` cover PR source proposals held in the host store."""

import json

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from tests import container_broker_support


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def broker(monkeypatch, tmp_path):
    return container_broker_support.install(monkeypatch, tmp_path / "broker")


def run(*args):
    return CliRunner().invoke(container_app, list(args))


def stage_source():
    result = run(
        "pr",
        "source",
        "--repo",
        "acme/widgets",
        "--ref",
        "refs/heads/topic",
        "--sha",
        "a" * 40,
        "--title",
        "Topic",
        "--body",
        "Body",
    )
    assert result.exit_code == 0, result.output
    return "001-source.json"


def stage_comment(monkeypatch, broker):
    """A staged (not source) PR comment proposal; returns its name."""
    from jailbee.container_outbox import gh

    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(n or 7, "b" * 40, "acme/widgets"))
    result = run("pr", "comment", "7", "--body", "hi")
    assert result.exit_code == 0, result.output
    return broker.proposals("pr")[-1].name


def test_ls_lists_store_sources_and_comment_proposals(home, broker, monkeypatch):
    name = stage_source()
    comment = stage_comment(monkeypatch, broker)
    result = run("outbox", "ls", "--json")
    assert result.exit_code == 0, result.output
    names = [item["name"] for item in json.loads(result.stdout)["pr"]]
    assert sorted(names) == ["001-source.json", "002-comment.json"]
    assert (name, comment) == ("001-source.json", "002-comment.json")
    plain = run("outbox", "ls")
    assert f"pr/{name}  description" in plain.stdout
    assert f"pr/{comment}  comment" in plain.stdout


def test_show_and_drop_route_a_store_source_to_the_broker(home, broker):
    name = stage_source()
    shown = run("outbox", "show", f"pr/{name}")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["source"] == "refs/heads/topic"
    dropped = run("outbox", "drop", f"pr/{name}")
    assert dropped.exit_code == 0, dropped.output
    assert broker.proposals("pr") == []


def test_show_and_drop_reach_a_staged_comment(home, broker, monkeypatch):
    comment = stage_comment(monkeypatch, broker)
    shown = run("outbox", "show", f"pr/{comment}")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["actions"] == [{"type": "comment", "body": "hi"}]
    dropped = run("outbox", "drop", f"pr/{comment}")
    assert dropped.exit_code == 0, dropped.output
    assert broker.proposals("pr") == []


def test_a_child_cannot_drop_a_store_source(home, broker, monkeypatch):
    name = stage_source()
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    result = run("outbox", "drop", f"pr/{name}")
    assert result.exit_code == 1
    assert [row.name for row in broker.proposals("pr")] == [name]
