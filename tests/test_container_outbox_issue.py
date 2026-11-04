import json

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.container_outbox import gh
from jailbee.container_outbox.store import Store
from jailbee.issue_manifest import parse_manifest


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


def invoke(*args):
    return CliRunner().invoke(container_app, ["issue", *args])


def test_create_and_batch_roundtrip(tmp_path):
    result = invoke("create", "--title", "Bug", "--body", "x" * 5000, "--ref", "bug", "--batch", "b")
    assert result.exit_code == 0, result.output
    result = invoke("comment", "@bug", "--body", "comment", "--batch", "b")
    assert result.exit_code == 0, result.output
    store = Store(tmp_path)
    name = store.draft_submit("issue", "b", "bug")
    path = store.outbox("issue")
    data = json.loads((path / name).read_text())
    bodies = {p.name: p.read_text() for p in path.glob("*.md")}
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
    for args in [("create",), ("edit",), ("comment",), ("labels",), ("state",)]:
        result = invoke(*args)
        assert result.exit_code == 1, result.output
        assert "child container" in result.output
    assert not (tmp_path / ".jailbee").exists()


@pytest.mark.parametrize("action,args", [
    ("edit", (None, "changed")),
    ("comment", ("hello",)),
    ("labels", (["triage"], [])),
    ("state", ("closed", "completed")),
])
def test_ref_actions_roundtrip(action, args):
    from jailbee.container_outbox import issue_cmds as commands

    create = commands.create_action(".", "Bug", "body", [], "bug")
    built = getattr(commands, f"{action}_action")(".", "@bug", *args, *(() if action == "comment" else (None,)))
    assert built["issue_ref"] == "bug"
    parse_manifest("test.json", json.dumps({"version": 1, "actions": [create, built]}), {})


def test_unknown_batch_ref_does_not_change_draft(tmp_path):
    assert invoke("create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("issue", "b")
    result = invoke("comment", "@nope", "--body", "hello", "--batch", "b")
    assert result.exit_code == 2
    assert "nope" in result.output
    assert store.draft_load_state("issue", "b") == before


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
    monkeypatch.setattr(sys, "argv", ["jailbee", "issue", "create", "--title", "Bug", "--body", "body"])
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 0
    assert "staged issue/" in capsys.readouterr().out


def test_concurrent_draft_appends_preserve_both(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = Store(tmp_path)
    entered, release, second_started = Event(), Event(), Event()
    original = store.draft_load_state
    calls = 0

    def paused_load(kind, name):
        nonlocal calls
        state = original(kind, name)
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
        return state

    monkeypatch.setattr(store, "draft_load_state", paused_load)
    def append(index):
        if index == 2:
            second_started.set()
        store.draft_append("issue", "b", {"type": "comment", "repo": ".", "issue": index, "body": str(index)}, {})
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(append, 1)
        assert entered.wait(5)
        second = executor.submit(append, 2)
        assert second_started.wait(5)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    manifest, bodies = store.draft_load_state("issue", "b")
    assert {a["issue"] for a in manifest["actions"]} == {1, 2}
    parse_manifest("b.json", json.dumps(manifest), bodies)


@pytest.mark.parametrize("args", [
    ("edit", "42", "--title", "new"),
    ("edit", "42", "--body", "new body"),
    ("comment", "42", "--body", "hello"),
    ("labels", "42", "--add", "triage", "--remove", "bug"),
    ("state", "42", "closed", "--reason", "completed"),
])
def test_existing_issue_roundtrip(monkeypatch, tmp_path, args):
    monkeypatch.setattr(gh, "issue_view", lambda *a: gh.IssueView("old", "old body", ("bug", "other"), "open"))
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    path = tmp_path / ".jailbee/issue-outbox"
    manifest = next(path.glob("*.json"))
    data = json.loads(manifest.read_text())
    parse_manifest(manifest.name, json.dumps(data), {})
    action = data["actions"][0]
    if args[0] == "edit":
        assert action["expected"] == ({"title": "old"} if "--title" in args else {"body": "old body"})
    if args[0] == "labels":
        assert action["expected"] == {"labels": ["bug", "other"]}


def test_body_file_is_staged(monkeypatch, tmp_path):
    body = tmp_path / "input.md"
    body.write_text("small body")
    result = invoke("create", "--title", "Bug", "--body-file", str(body))
    assert result.exit_code == 0, result.output
    path = tmp_path / ".jailbee/issue-outbox"
    manifest = next(path.glob("*.json"))
    data = json.loads(manifest.read_text())
    body_name = data["actions"][0]["body_file"]
    assert (path / body_name).read_text() == "small body"
    parse_manifest(manifest.name, json.dumps(data), {body_name: "small body"})


def test_gh_failure_is_refused(monkeypatch, tmp_path):
    def fail(*args):
        raise gh.GhError("lookup failed")
    monkeypatch.setattr(gh, "issue_view", fail)
    result = invoke("edit", "42", "--title", "new")
    assert result.exit_code == 1
    assert "lookup failed" in result.output
    assert not (tmp_path / ".jailbee").exists()


def test_cross_repo_ref_and_body_cap_rejected_without_draft_change(tmp_path):
    assert invoke("create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("issue", "b")
    for args in [("comment", "@bug", "--body", "comment", "--repo", "github:other/repo"), ("comment", "@bug", "--body", "x" * 65537)]:
        result = invoke(*args, "--batch", "b")
        assert result.exit_code == 2, result.output
        assert store.draft_load_state("issue", "b") == before


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
    from jailbee.container_outbox.issue_cmds import create_action, edit_action, comment_action, labels_action, state_action
    view = gh.IssueView("old", "old body", ("bug",), "open")
    assert create_action(".", "title", "body", ["bug"], "new") == {"type": "create", "repo": ".", "ref": "new", "title": "title", "body": "body", "labels": ["bug"]}
    assert edit_action(".", 42, "title", None, view)["expected"] == {"title": "old"}
    assert edit_action(".", 42, None, "body", view)["expected"] == {"body": "old body"}
    assert comment_action(".", "@new", "body") == {"type": "comment", "repo": ".", "issue_ref": "new", "body": "body"}
    assert labels_action(".", 42, ["triage"], [], view)["expected"] == {"labels": ["bug"]}
    assert state_action(".", 42, "closed", "completed", view)["expected"] == {"state": "open"}


def test_reopened_reason_normalized_for_host_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "issue_view", lambda *a: gh.IssueView("t", "b", (), "closed"))
    result = invoke("state", "42", "open", "--reason", "reopened")
    assert result.exit_code == 0, result.output
    manifest = next((tmp_path / ".jailbee/issue-outbox").glob("*.json"))
    data = json.loads(manifest.read_text())
    assert "reason" not in data["actions"][0]
    parse_manifest(manifest.name, json.dumps(data), {})


@pytest.mark.parametrize("args", [
    ("edit", "@bug", "--title", "new"),
    ("comment", "@bug", "--body", "hello"),
    ("labels", "@bug", "--add", "triage"),
    ("state", "@bug", "closed", "--reason", "completed"),
])
def test_ref_cli_roundtrip(tmp_path, args):
    assert invoke("create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b").exit_code == 0
    result = invoke(*args, "--batch", "b")
    assert result.exit_code == 0, result.output
    store = Store(tmp_path)
    name = store.draft_submit("issue", "b", "bug")
    manifest = store.outbox("issue") / name
    actions = parse_manifest(name, manifest.read_text(), {}).actions
    assert len(actions) == 2


@pytest.mark.parametrize("args", [
    ("edit", "@missing", "--title", "new"),
    ("labels", "@missing", "--add", "new"),
    ("state", "@missing", "closed", "--reason", "completed"),
    ("edit", "@bug", "--title", "new", "--repo", "github:other/repo"),
    ("labels", "@bug", "--add", "new", "--repo", "github:other/repo"),
    ("state", "@bug", "closed", "--reason", "completed", "--repo", "github:other/repo"),
])
def test_ref_lookup_rejected_without_changes_or_gh(monkeypatch, tmp_path, args):
    monkeypatch.setattr(gh, "issue_view", lambda *a: pytest.fail("unexpected gh lookup"))
    assert invoke("create", "--title", "Bug", "--body", "body", "--ref", "bug", "--batch", "b").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("issue", "b")
    result = invoke(*args, "--batch", "b")
    assert result.exit_code == 2, result.output
    assert store.draft_load_state("issue", "b") == before


def test_ref_label_check_uses_created_labels(tmp_path):
    assert invoke("create", "--title", "Bug", "--body", "body", "--ref", "bug", "--label", "bug", "--batch", "b").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("issue", "b")
    result = invoke("labels", "@bug", "--add", "BUG", "--batch", "b")
    assert result.exit_code == 2
    assert store.draft_load_state("issue", "b") == before
