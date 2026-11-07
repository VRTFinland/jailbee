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


def test_ambiguous_draft(home, broker, monkeypatch):
    from jailbee.container_outbox import gh

    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(n or 7, "old", "acme/repo"))
    runner = CliRunner()
    assert (
        runner.invoke(
            container_app, ["pr", "comment", "7", "--body", "x", "--batch", "same"]
        ).exit_code
        == 0
    )
    assert (
        CliRunner()
        .invoke(container_app, ["issue", "comment", "7", "--body", "x", "--batch", "same"])
        .exit_code
        == 0
    )
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
    assert broker.draft_state("batch") is None
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
    assert broker.draft_state("b") is not None


def test_pr_batch_and_review_protection(home, monkeypatch, broker):
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
    assert broker.draft("review-12", "pr") is not None
    assert invoke("submit", "b", "--kind", "pr").exit_code == 0
    (row,) = broker.proposals("pr")
    assert row.name == "001-b.json"
    parse_manifest(row.name, row.manifest, dict(row.bodies))
    assert invoke("discard", "review-12", "--kind", "pr").exit_code == 0
    assert broker.draft("review-12", "pr") is None


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


def test_issue_draft_discard_goes_over_the_socket(home, broker):
    assert (
        CliRunner()
        .invoke(container_app, ["issue", "comment", "7", "--body", "x", "--batch", "b"])
        .exit_code
        == 0
    )
    assert broker.draft_state("b") is not None
    result = invoke("discard", "b", "--kind", "issue")
    assert result.exit_code == 0, result.output
    assert broker.draft_state("b") is None
    assert not (home / ".jailbee").exists()
    assert invoke("discard", "b", "--kind", "issue").exit_code == 2  # nothing left to discard
    assert broker.proposals() == []


def test_issue_drafts_never_touch_the_container_file_store(home, broker):
    runner = CliRunner()
    runner.invoke(container_app, ["issue", "comment", "7", "--body", "x", "--batch", "b"])
    assert invoke("submit", "b", "--kind", "issue").exit_code == 0
    assert not (home / ".jailbee/outbox-drafts").exists()
    assert not (home / ".jailbee/issue-outbox").exists()
