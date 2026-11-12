import json

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.container_outbox import gh
from jailbee.issue_manifest import parse_manifest
from tests import container_broker_support


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


@pytest.fixture(autouse=True)
def broker(monkeypatch, tmp_path):
    return container_broker_support.install(monkeypatch, tmp_path / "broker")


def invoke(*args):
    return CliRunner().invoke(container_app, ["issue", *args])


def submit(name):
    result = CliRunner().invoke(container_app, ["outbox", "submit", name])
    assert result.exit_code == 0, result.output
    return result


def test_project_batch_sends_one_atomic_request_and_reports_reader_failure(monkeypatch):
    from jailbee.container_outbox import broker
    from jailbee.container_outbox.common import OutboxCommandError

    calls = []

    def append_many(*args):
        calls.append(args)
        raise OutboxCommandError("second host reader failed", 1)

    monkeypatch.setattr(broker, "draft_append_many", append_many)
    monkeypatch.setattr(broker, "draft_append", lambda *a: pytest.fail("non-atomic append"))
    result = invoke(
        "project",
        "7",
        "--project",
        "acme/3",
        "--set",
        "Status=Doing",
        "--set",
        "Priority=High",
        "--batch",
        "b",
    )
    assert result.exit_code == 1
    assert "second host reader failed" in result.output
    assert len(calls) == 1 and len(calls[0][2]) == 2


def test_project_add_stages_without_container_github_reads(monkeypatch):
    from jailbee.container_outbox import broker

    staged = []
    monkeypatch.setattr(gh, "issue_view", lambda *a: pytest.fail("container read"))
    monkeypatch.setattr(broker, "put", lambda *args: staged.append(args) or "001-project-add.json")
    result = invoke("project", "7", "--project", "acme/3", "--add")
    assert result.exit_code == 0, result.output
    assert staged[0][2] == {
        "version": 1,
        "actions": [{"type": "project_add", "repo": ".", "issue": 7, "project": "acme/3"}],
    }


def test_project_clear_and_remove_flags_stage_explicit_actions(monkeypatch):
    from jailbee.container_outbox import broker

    staged = []
    monkeypatch.setattr(broker, "put", lambda *args: staged.append(args) or "001-project.json")
    result = invoke(
        "project", "7", "--project", "acme/3", "--set", "Status=Doing", "--clear", "Priority"
    )
    assert result.exit_code == 0, result.output
    assert [a["type"] for a in staged[-1][2]["actions"]] == ["project_set", "project_clear"]
    assert staged[-1][2]["actions"][1] == {
        "type": "project_clear",
        "repo": ".",
        "issue": 7,
        "project": "acme/3",
        "field": "Priority",
    }
    result = invoke("project", "7", "--remove")
    assert result.exit_code == 0, result.output
    assert staged[-1][2]["actions"] == [{"type": "project_remove", "repo": ".", "issue": 7}]
    assert invoke("project", "7", "--remove", "--clear", "Priority").exit_code == 2
    assert invoke("project", "7", "--set", "Priority=High", "--clear", "priority").exit_code == 2


def test_project_set_expands_fields_into_one_proposal_without_reads(monkeypatch):
    from jailbee.container_outbox import broker

    staged = []
    monkeypatch.setattr(gh, "issue_view", lambda *a: pytest.fail("container read"))
    monkeypatch.setattr(broker, "put", lambda *args: staged.append(args) or "001-fields.json")
    result = invoke("project", "7", "--set", "Status=Doing", "--set", "Priority=High")
    assert result.exit_code == 0, result.output
    assert staged[0][2]["actions"] == [
        {"type": "project_set", "repo": ".", "issue": 7, "field": "Status", "value": "Doing"},
        {"type": "project_set", "repo": ".", "issue": 7, "field": "Priority", "value": "High"},
    ]
    result = invoke("project", "7", "--set", "Status=Doing", "--set", "status=Todo")
    assert result.exit_code == 2 and "duplicate" in result.output
    assert len(staged) == 1


def test_create_json_outputs_only_manifest_name(tmp_path, broker):
    result = invoke("create", "--title", "Bug", "--body", "body", "--json")
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output == {"name": "001-bug.json"}
    assert broker.only()[0] == output["name"]
    assert not (tmp_path / ".jailbee/issue-outbox").exists()


def test_create_json_rejects_unpublished_batch(tmp_path):
    result = invoke("create", "--title", "Bug", "--body", "body", "--batch", "b", "--json")
    assert result.exit_code == 2
    assert "--json cannot be combined with --batch" in result.output
    assert not (tmp_path / ".jailbee/issue-outbox").exists()


def test_create_and_batch_roundtrip(tmp_path, broker):
    result = invoke(
        "create", "--title", "Bug", "--body", "x" * 5000, "--ref", "bug", "--batch", "b"
    )
    assert result.exit_code == 0, result.output
    result = invoke("comment", "@bug", "--body", "comment", "--batch", "b")
    assert result.exit_code == 0, result.output
    assert broker.draft_state("b") is not None
    submit("b")
    name, data, bodies = broker.only()
    assert broker.draft_state("b") is None
    parse_manifest(name, json.dumps(data), bodies)
    assert len(data["actions"]) == 2
    assert list(bodies.values()) == ["x" * 5000]


def test_reject_existing_label(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "issue_view", lambda *a: gh.IssueView("t", "b", ("bug",), "open"))
    result = invoke("labels", "42", "--add", "bug")
    assert result.exit_code == 2
    assert not (tmp_path / ".jailbee/issue-outbox").exists()


def test_child_refused_before_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    monkeypatch.setattr(gh, "issue_view", lambda *a: pytest.fail("lookup"))
    for args in [
        ("create",),
        ("edit",),
        ("comment",),
        ("labels",),
        ("state",),
        ("assign", "42", "--me"),
    ]:
        result = invoke(*args)
        assert result.exit_code == 1, result.output
        assert "child container" in result.output
    assert not (tmp_path / ".jailbee").exists()


@pytest.mark.parametrize(
    "action,args",
    [
        ("edit", (None, "changed")),
        ("comment", ("hello",)),
        ("labels", (["triage"], [])),
        ("state", ("closed", "completed")),
    ],
)
def test_ref_actions_roundtrip(action, args):
    from jailbee.container_outbox import issue_cmds as commands

    create = commands.create_action(".", "Bug", "body", [], "bug")
    built = getattr(commands, f"{action}_action")(
        ".", "@bug", *args, *(() if action == "comment" else (None,))
    )
    assert built["issue_ref"] == "bug"
    parse_manifest("test.json", json.dumps({"version": 1, "actions": [create, built]}), {})


def test_unknown_batch_ref_does_not_change_draft(tmp_path, broker):
    assert (
        invoke(
            "create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b"
        ).exit_code
        == 0
    )
    before = broker.draft_state("b")
    result = invoke("comment", "@nope", "--body", "hello", "--batch", "b")
    assert result.exit_code == 2
    assert "nope" in result.output
    assert broker.draft_state("b") == before


def test_same_state_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "issue_view", lambda *a: gh.IssueView("t", "b", (), "open"))
    result = invoke("state", "42", "open")
    assert result.exit_code == 2
    assert not (tmp_path / ".jailbee/issue-outbox").exists()


def test_entry_dispatches_issue(monkeypatch, tmp_path, capsys):
    import sys

    from jailbee import container_main

    monkeypatch.setattr(container_main.container_deps, "check", lambda: None)
    monkeypatch.setattr(container_main, "PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["jailbee", "issue", "create", "--title", "Bug", "--body", "body"]
    )
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 0
    assert "staged issue/" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ("edit", "42", "--title", "new"),
        ("edit", "42", "--body", "new body"),
        ("comment", "42", "--body", "hello"),
        ("labels", "42", "--add", "triage", "--remove", "bug"),
        ("state", "42", "closed", "--reason", "completed"),
    ],
)
def test_existing_issue_roundtrip(monkeypatch, tmp_path, broker, args):
    monkeypatch.setattr(
        gh, "issue_view", lambda *a: gh.IssueView("old", "old body", ("bug", "other"), "open")
    )
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    name, data, bodies = broker.only()
    parse_manifest(name, json.dumps(data), bodies)
    action = data["actions"][0]
    if args[0] == "edit":
        assert action["expected"] == (
            {"title": "old"} if "--title" in args else {"body": "old body"}
        )
    if args[0] == "labels":
        assert action["expected"] == {"labels": ["bug", "other"]}


def test_body_file_is_staged(monkeypatch, tmp_path, broker):
    body = tmp_path / "input.md"
    body.write_text("small body")
    result = invoke("create", "--title", "Bug", "--body-file", str(body))
    assert result.exit_code == 0, result.output
    name, data, bodies = broker.only()
    body_name = data["actions"][0]["body_file"]
    assert body_name == "001-bug-1.md"
    assert bodies == {body_name: "small body"}
    parse_manifest(name, json.dumps(data), bodies)


def test_gh_failure_is_refused(monkeypatch, tmp_path):
    def fail(*args):
        raise gh.GhError("lookup failed")

    monkeypatch.setattr(gh, "issue_view", fail)
    result = invoke("edit", "42", "--title", "new")
    assert result.exit_code == 1
    assert "lookup failed" in result.output
    assert not (tmp_path / ".jailbee").exists()


def test_cross_repo_ref_and_body_cap_rejected_without_draft_change(tmp_path, broker):
    assert (
        invoke(
            "create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b"
        ).exit_code
        == 0
    )
    before = broker.draft_state("b")
    for args in [
        ("comment", "@bug", "--body", "comment", "--repo", "github:other/repo"),
        ("comment", "@bug", "--body", "x" * 65537),
    ]:
        result = invoke(*args, "--batch", "b")
        assert result.exit_code == 2, result.output
        assert broker.draft_state("b") == before


def test_child_marker_refuses_before_body_file_access(tmp_path):
    marker = tmp_path / ".jailbee/orchestration/role"
    marker.parent.mkdir(parents=True)
    marker.write_text("child\n")
    result = invoke("create", "--title", "Bug", "--body-file", str(tmp_path / "missing"))
    assert result.exit_code == 1
    assert "child container" in result.output
    assert "jailbee inbox send message --text" in result.output
    assert not (tmp_path / ".jailbee/issue-outbox").exists()


def test_builder_expected_fields():
    from jailbee.container_outbox.issue_cmds import (
        comment_action,
        create_action,
        edit_action,
        labels_action,
        state_action,
    )

    view = gh.IssueView("old", "old body", ("bug",), "open")
    assert create_action(".", "title", "body", ["bug"], "new") == {
        "type": "create",
        "repo": ".",
        "ref": "new",
        "title": "title",
        "body": "body",
        "labels": ["bug"],
    }
    assert edit_action(".", 42, "title", None, view)["expected"] == {"title": "old"}
    assert edit_action(".", 42, None, "body", view)["expected"] == {"body": "old body"}
    assert comment_action(".", "@new", "body") == {
        "type": "comment",
        "repo": ".",
        "issue_ref": "new",
        "body": "body",
    }
    assert labels_action(".", 42, ["triage"], [], view)["expected"] == {"labels": ["bug"]}
    assert state_action(".", 42, "closed", "completed", view)["expected"] == {"state": "open"}


def test_reopened_reason_normalized_for_host_schema(monkeypatch, tmp_path, broker):
    monkeypatch.setattr(gh, "issue_view", lambda *a: gh.IssueView("t", "b", (), "closed"))
    result = invoke("state", "42", "open", "--reason", "reopened")
    assert result.exit_code == 0, result.output
    name, data, _ = broker.only()
    assert "reason" not in data["actions"][0]
    parse_manifest(name, json.dumps(data), {})


@pytest.mark.parametrize(
    "args",
    [
        ("edit", "@bug", "--title", "new"),
        ("comment", "@bug", "--body", "hello"),
        ("labels", "@bug", "--add", "triage"),
        ("state", "@bug", "closed", "--reason", "completed"),
    ],
)
def test_ref_cli_roundtrip(tmp_path, broker, args):
    assert (
        invoke(
            "create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b"
        ).exit_code
        == 0
    )
    result = invoke(*args, "--batch", "b")
    assert result.exit_code == 0, result.output
    submit("b")
    name, data, bodies = broker.only()
    assert len(parse_manifest(name, json.dumps(data), bodies).actions) == 2


@pytest.mark.parametrize(
    "args",
    [
        ("edit", "@missing", "--title", "new"),
        ("labels", "@missing", "--add", "new"),
        ("state", "@missing", "closed", "--reason", "completed"),
        ("edit", "@bug", "--title", "new", "--repo", "github:other/repo"),
        ("labels", "@bug", "--add", "new", "--repo", "github:other/repo"),
        ("state", "@bug", "closed", "--reason", "completed", "--repo", "github:other/repo"),
    ],
)
def test_ref_lookup_rejected_without_changes_or_gh(monkeypatch, tmp_path, broker, args):
    monkeypatch.setattr(gh, "issue_view", lambda *a: pytest.fail("unexpected gh lookup"))
    assert (
        invoke(
            "create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b"
        ).exit_code
        == 0
    )
    before = broker.draft_state("b")
    result = invoke(*args, "--batch", "b")
    assert result.exit_code == 2, result.output
    assert broker.draft_state("b") == before


def test_assign_staged_json_exact(monkeypatch, tmp_path, broker):
    monkeypatch.setattr(
        gh, "issue_view", lambda *a: gh.IssueView("t", "b", (), "open", ("alice", "carol"))
    )
    result = invoke("assign", "42", "--me", "--add", "bob", "--add", "dave", "--remove", "carol")
    assert result.exit_code == 0, result.output
    name, data, _ = broker.only()
    assert data == {
        "version": 1,
        "actions": [
            {
                "type": "assignees",
                "repo": ".",
                "issue": 42,
                "add": ["@me", "bob", "dave"],
                "remove": ["carol"],
                "expected": {"assignees": ["alice", "carol"]},
            }
        ],
    }
    parse_manifest(name, json.dumps(data), {})


def test_assign_without_flags_is_usage_error(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "issue_view", lambda *a: pytest.fail("unexpected gh lookup"))
    result = invoke("assign", "42")
    assert result.exit_code == 2, result.output
    assert "--unassign-me" in result.output
    assert not (tmp_path / ".jailbee/issue-outbox").exists()


@pytest.mark.parametrize(
    "args,message",
    [
        (("assign", "42", "--me", "--unassign-me"), "both add and remove"),
        (("assign", "42", "--add", "alice"), "already assigned"),
        (("assign", "42", "--remove", "bob"), "is not assigned"),
        (("assign", "42", "--add", "bob", "--add", "BOB"), "more than once"),
        (("assign", "42", "--add", "bob", "--remove", "Bob"), "both add and remove"),
        (("assign", "42", "--add", "@ME"), "invalid GitHub login"),
        (("assign", "42", "--add", "bad login"), "invalid GitHub login"),
        (("assign", "42", "--remove", "-bob"), "invalid GitHub login"),
    ],
)
def test_assign_conflicts_rejected_without_draft(monkeypatch, tmp_path, args, message):
    monkeypatch.setattr(gh, "issue_view", lambda *a: gh.IssueView("t", "b", (), "open", ("alice",)))
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert not (tmp_path / ".jailbee/issue-outbox").exists()


def test_assign_ref_roundtrip_and_created_check(tmp_path, broker):
    assert (
        invoke(
            "create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b"
        ).exit_code
        == 0
    )
    result = invoke("assign", "@bug", "--me", "--add", "bob", "--batch", "b")
    assert result.exit_code == 0, result.output
    submit("b")
    name, data, _ = broker.only()
    assert data["actions"][1] == {
        "type": "assignees",
        "repo": ".",
        "issue_ref": "bug",
        "add": ["@me", "bob"],
        "remove": [],
        "expected": {"assignees": []},
    }
    parse_manifest(name, json.dumps(data), {})


def test_assign_ref_remove_of_unassigned_login_rejected(tmp_path, broker):
    assert (
        invoke(
            "create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b"
        ).exit_code
        == 0
    )
    before = broker.draft_state("b")
    result = invoke("assign", "@bug", "--remove", "bob", "--batch", "b")
    assert result.exit_code == 2, result.output
    assert "is not assigned" in result.output
    assert broker.draft_state("b") == before


def test_ref_label_check_uses_created_labels(tmp_path, broker):
    assert (
        invoke(
            "create",
            "--title",
            "Bug",
            "--body",
            "body",
            "--ref",
            "bug",
            "--label",
            "bug",
            "--batch",
            "b",
        ).exit_code
        == 0
    )
    before = broker.draft_state("b")
    result = invoke("labels", "@bug", "--add", "BUG", "--batch", "b")
    assert result.exit_code == 2
    assert broker.draft_state("b") == before


def test_assign_accepts_underscore_logins(monkeypatch, tmp_path, broker):
    monkeypatch.setattr(gh, "issue_view", lambda *a: gh.IssueView("t", "b", (), "open", ()))
    result = invoke("assign", "42", "--add", "jdoe_acme")
    assert result.exit_code == 0, result.output
    assert broker.only()[1]["actions"][0]["add"] == ["jdoe_acme"]
