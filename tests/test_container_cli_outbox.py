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


def invoke(*args):
    return CliRunner().invoke(container_app, ["outbox", *args])


def test_empty_list_and_child_guard(home, monkeypatch):
    result = invoke("ls", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"issue": [], "pr": [], "drafts": []}
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    for command in ("drop", "submit", "discard"):
        result = invoke(command)
        assert result.exit_code == 1
        assert "child container" in result.output
    assert not (home / ".jailbee").exists()


def test_ambiguous_draft(home):
    store = Store(home)
    for kind in ("issue", "pr"):
        store.draft_save(kind, "same", {"version": 1, "actions": []}, {})
    result = invoke("submit", "same")
    assert result.exit_code == 2
    assert "--kind" in result.output


def test_issue_batch_cli_roundtrip(home, broker):
    from jailbee.issue_manifest import parse_manifest

    runner = CliRunner()

    def run(*args):
        result = runner.invoke(container_app, list(args))
        assert result.exit_code == 0, result.output
        return result

    run(
        "issue",
        "create",
        "--title",
        "Bug",
        "--body",
        "x" * 5000,
        "--ref",
        "bug",
        "--batch",
        "batch",
    )
    run("issue", "comment", "@bug", "--body", "comment", "--batch", "batch")
    listing = json.loads(invoke("ls", "--json").stdout)
    assert listing["drafts"] == [{"kind": "issue", "name": "batch", "actions": 2}]
    run("outbox", "submit", "batch")
    name, data, bodies = broker.only()
    assert name == "001-batch.json"
    parse_manifest(name, json.dumps(data), bodies)
    assert len(data["actions"]) == 2
    assert list(bodies.values()) == ["x" * 5000]
    assert Store(home).draft_load("issue", "batch") is None
    assert not list(Store(home).outbox("issue").glob("*.json"))
    listing = json.loads(invoke("ls", "--json").stdout)
    assert listing["issue"] == [{"name": name, "summary": "create, comment"}]
    assert listing["drafts"] == []
    result = invoke("show", "issue/" + name)
    assert json.loads(result.stdout)["version"] == 1


def test_a_refused_issue_draft_submit_keeps_the_draft(home, broker):
    for _ in range(20):
        assert (
            CliRunner().invoke(container_app, ["issue", "comment", "7", "--body", "x"]).exit_code
            == 0
        )
    assert (
        CliRunner()
        .invoke(container_app, ["issue", "comment", "7", "--body", "y", "--batch", "b"])
        .exit_code
        == 0
    )
    result = invoke("submit", "b")
    assert result.exit_code == 2
    assert "cap of 20" in result.output
    assert Store(home).draft_load("issue", "b") is not None


def test_pr_batch_and_review_protection(home, monkeypatch):
    from jailbee.container_outbox import gh
    from jailbee.pr_outbox import parse_manifest

    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(n or 12, "old", "acme/repo"))
    runner = CliRunner()
    for args in [
        ("pr", "comment", "12", "--body", "hello", "--batch", "b"),
        ("pr", "review", "add", "12", "--file", "a.py", "--line", "1", "--body", "note"),
    ]:
        result = runner.invoke(container_app, list(args))
        assert result.exit_code == 0, result.output
    refused = invoke("submit", "review-12")
    assert refused.exit_code == 2
    assert "jailbee pr review submit" in refused.output
    assert Store(home).draft_load("pr", "review-12") is not None
    assert invoke("submit", "b", "--kind", "pr").exit_code == 0
    path = next(Store(home).outbox("pr").glob("*.json"))
    parse_manifest(path.name, path.read_text(), {})
    assert invoke("discard", "review-12", "--kind", "pr").exit_code == 0


def test_child_reads_and_refuses_mutation(home, monkeypatch, broker):
    runner = CliRunner()
    result = runner.invoke(container_app, ["issue", "create", "--title", "Bug", "--body", "body"])
    assert result.exit_code == 0, result.output
    name = broker.only()[0]
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    assert invoke("ls", "--json").exit_code == 0
    assert invoke("show", "issue/" + name).exit_code == 0
    for args in [("drop", "issue/" + name), ("submit", "x"), ("discard", "x")]:
        result = invoke(*args)
        assert result.exit_code == 1
        assert "jailbee inbox send message --text" in result.output
    assert broker.only()[0] == name


def test_real_container_entry(home, monkeypatch, capsys):
    import sys

    from jailbee import container_main

    monkeypatch.setattr(container_main.container_deps, "check", lambda: None)
    monkeypatch.setattr(container_main, "PACKAGE_DIR", home)
    monkeypatch.setattr(sys, "argv", ["jailbee", "outbox", "ls", "--json"])
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["drafts"] == []
