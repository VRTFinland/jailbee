"""`jailbee outbox ls|show|drop` cover PR source proposals held in the host store."""

import json

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.container_outbox.store import Store
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


def stage_file_comment(home):
    manifest = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": 7,
        "head_sha": "b" * 40,
        "actions": [{"type": "comment", "body": "hi"}],
    }
    return Store(home).write_manifest("pr", "comment", manifest, {})


def test_ls_lists_store_sources_and_file_proposals(home, broker):
    name = stage_source()
    comment = stage_file_comment(home)
    result = run("outbox", "ls", "--json")
    assert result.exit_code == 0, result.output
    names = [item["name"] for item in json.loads(result.stdout)["pr"]]
    assert sorted(names) == sorted([name, comment])
    plain = run("outbox", "ls")
    assert f"pr/{name}  description" in plain.stdout


def test_show_and_drop_route_a_store_source_to_the_broker(home, broker):
    name = stage_source()
    shown = run("outbox", "show", f"pr/{name}")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["source"] == "refs/heads/topic"
    dropped = run("outbox", "drop", f"pr/{name}")
    assert dropped.exit_code == 0, dropped.output
    assert broker.proposals("pr") == []


def test_show_and_drop_still_reach_file_proposals(home, broker):
    comment = stage_file_comment(home)
    shown = run("outbox", "show", f"pr/{comment}")
    assert shown.exit_code == 0, shown.output
    dropped = run("outbox", "drop", f"pr/{comment}")
    assert dropped.exit_code == 0, dropped.output
    assert list(Store(home).outbox("pr").glob("*.json")) == []


def test_a_child_cannot_drop_a_store_source(home, broker, monkeypatch):
    name = stage_source()
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    result = run("outbox", "drop", f"pr/{name}")
    assert result.exit_code == 1
    assert [row.name for row in broker.proposals("pr")] == [name]
