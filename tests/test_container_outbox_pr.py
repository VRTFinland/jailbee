"""PR proposals roundtrip the host schema without publishing to GitHub."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.container_outbox import gh
from jailbee.container_outbox.store import Store
from jailbee.pr_outbox import parse_manifest
from jailbee.pr_manifest import parse_manifest as parse_pure


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    monkeypatch.setattr(gh, "pr_view", lambda number: gh.PrView(number or 1234, "old-head", "acme/widgets"))
    monkeypatch.setattr(gh, "run_json", lambda *a, **kw: pytest.fail("unexpected gh call"))


def invoke(*args):
    return CliRunner().invoke(container_app, ["pr", *args])


def published(tmp_path):
    path = tmp_path / ".jailbee/pr-outbox"
    manifests = sorted(path.glob("*.json"))
    assert len(manifests) == 1
    text = manifests[0].read_text()
    bodies = {p.name: p.read_text() for p in path.glob("*.md")}
    parsed = parse_manifest(manifests[0].name, text, bodies)
    assert parse_pure(manifests[0].name, text, bodies) == parsed
    return json.loads(text), parsed, bodies


def test_three_comments_one_review_fresh_head_and_body_files(monkeypatch, tmp_path):
    body_file = tmp_path / "input.md"
    body_file.write_text("Keep this multiline\ncomment intact.\n")
    for args in [
        ("--line", "88", "--body", "First"),
        ("--line", "134", "--start-line", "120", "--side", "LEFT", "--body-file", str(body_file)),
        ("--line", "200", "--body", "Third"),
    ]:
        result = invoke("review", "add", "1234", "--file", "src/worktime.py", *args)
        assert result.exit_code == 0, result.output
    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(1234, "fresh-head", "acme/widgets"))
    result = invoke("review", "submit", "1234")
    assert result.exit_code == 0, result.output
    raw, parsed, bodies = published(tmp_path)
    assert raw["head_sha"] == "fresh-head"
    assert len(raw["actions"]) == 1
    review = parsed.actions[0]
    assert review.event == "COMMENT"
    assert review.body == ""  # The host parser permits an empty optional summary.
    assert [c.body for c in review.comments] == ["First", body_file.read_text(), "Third"]
    assert review.comments[1].start_line == 120
    assert review.comments[1].side == "LEFT"
    assert list(bodies.values()) == [body_file.read_text()]
    assert Store(tmp_path).draft_load_state("pr", "review-1234") is None


@pytest.mark.parametrize("args,expected", [
    (("reply", "1234", "--to", "99887", "--body", "Reply"), {"type": "reply", "comment_id": 99887, "body": "Reply"}),
    (("comment", "1234", "--reply-to", "4455", "--body", "General"), {"type": "comment", "reply_to": 4455, "body": "General"}),
    (("description", "1234", "--title", "Improve onboarding", "--body", "Description"), {"type": "description", "title": "Improve onboarding", "body": "Description"}),
    (("description", "--repo", "acme/widgets", "--title", "New PR", "--body", "Description", "--branch", "fix/onboarding"), {"type": "description", "title": "New PR", "body": "Description", "branch": "fix/onboarding"}),
])
def test_schema_actions(tmp_path, args, expected):
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    raw, _, _ = published(tmp_path)
    assert raw == {"version": 1, "repo": "acme/widgets", "pr": None if args[1].startswith("--") else 1234, "head_sha": None if args[1].startswith("--") else "old-head", "actions": [expected]}


@pytest.mark.parametrize("alias", ["--ref", "--head"])
@pytest.mark.parametrize("numbered", [False, True])
def test_source_schema_examples_without_existing_pr(monkeypatch, tmp_path, alias, numbered):
    monkeypatch.setattr(gh, "pr_view", lambda *a: pytest.fail("source with repo must not need a PR"))
    ref = "refs/jailbee/from/rework-12/fix/issue-12" if numbered else "refs/jailbee/from/fix-12/fix/issue-12"
    sha = "abcdef0123456789abcdef0123456789abcdef01" if numbered else "0123456789abcdef0123456789abcdef01234567"
    args = ["source", alias, ref, "--sha", sha, "--repo", "acme/widgets"]
    if numbered:
        args += ["--pr", "1234"]
    else:
        args += ["--base", "main", "--title", "Fix issue 12", "--body", "Fix the reported issue.", "--branch", "fix/issue-12"]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    raw, _, _ = published(tmp_path)
    expected = {"version": 1, "repo": "acme/widgets", "pr": 1234 if numbered else None, "head_sha": None, "source": ref, "source_sha": sha, "actions": []}
    if not numbered:
        expected.update(base="main", actions=[{"type": "description", "title": "Fix issue 12", "body": "Fix the reported issue.", "branch": "fix/issue-12"}])
    assert raw == expected


@pytest.mark.parametrize("start", [10, 11])
def test_invalid_span_before_draft_write(tmp_path, start):
    result = invoke("review", "add", "1234", "--file", "a.py", "--line", "10", "--start-line", str(start), "--body", "bad")
    assert result.exit_code == 2, result.output
    assert "start_line" in result.output
    assert not (tmp_path / ".jailbee/outbox-drafts").exists()


COMMANDS = [
    ("comment",), ("reply",), ("description",), ("review", "add"),
    ("review", "submit"), ("review", "discard"), ("source",),
    ("comment", "1234", "--body-file", "missing"),
    ("reply", "1234", "--to", "9", "--body-file", "missing"),
    ("description", "1234", "--title", "Title", "--body-file", "missing"),
    ("review", "add", "1234", "--file", "a.py", "--line", "1", "--body-file", "missing"),
    ("review", "submit", "1234", "--body-file", "missing"),
    ("review", "discard", "1234"),
    ("source", "--repo", "acme/widgets", "--ref", "refs/heads/a", "--sha", "a" * 40, "--body-file", "missing"),
]


@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("command", COMMANDS)
def test_child_all_commands_guard_before_missing_options(monkeypatch, tmp_path, command, marker):
    if marker:
        role = tmp_path / ".jailbee/orchestration/role"
        role.parent.mkdir(parents=True)
        role.write_text("child\n")
    else:
        monkeypatch.setenv("JAILBEE_ROLE", "child")
    monkeypatch.setattr(gh, "pr_view", lambda *a: pytest.fail("lookup before guard"))
    result = invoke(*command)
    assert result.exit_code == 1, result.output
    assert "child container" in result.output
    assert "jailbee inbox send message --text" in result.output
    assert not (tmp_path / ".jailbee/pr-outbox").exists()
    assert not (tmp_path / ".jailbee/outbox-drafts").exists()


@pytest.mark.parametrize("command", [("comment", "--body", "hello"), ("reply", "--to", "9", "--body", "hello"), ("review", "discard")])
def test_omitted_number_resolves_current_branch(monkeypatch, tmp_path, command):
    def view(number):
        assert number is None
        return gh.PrView(1234, "head", "acme/widgets")
    monkeypatch.setattr(gh, "pr_view", view)
    result = invoke(*command)
    assert result.exit_code == 0, result.output
    if command[-1] != "discard":
        assert published(tmp_path)[0]["pr"] == 1234


@pytest.mark.parametrize("explicit,code", [(False, 2), (True, 1)])
def test_lookup_failure_distinguishes_missing_target(monkeypatch, tmp_path, explicit, code):
    def fail(number):
        raise gh.GhError("lookup failed; retry `gh pr view`")
    monkeypatch.setattr(gh, "pr_view", fail)
    result = invoke("comment", *(("1234",) if explicit else ()), "--body", "hello")
    assert result.exit_code == code, result.output
    assert "lookup failed" in result.output
    if not explicit:
        assert "name the PR number" in result.output
    assert not (tmp_path / ".jailbee").exists()


def test_title_only_description_preserves_current_body(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "run_json", lambda argv, **kw: {"body": "Original\nbody\n"} if argv == ["gh", "pr", "view", "1234", "--json", "body"] else pytest.fail(str(argv)))
    result = invoke("description", "1234", "--title", "New title")
    assert result.exit_code == 0, result.output
    assert published(tmp_path)[1].actions[0].body == "Original\nbody\n"


@pytest.mark.parametrize("command", ["comment", "reply", "description"])
def test_batches_keep_body_files_and_target(monkeypatch, tmp_path, command):
    body = tmp_path / "input.md"
    body.write_text("File body\n")
    args = [command, "1234", "--body-file", str(body), "--batch", "b"]
    if command == "reply":
        args += ["--to", "9"]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert invoke("comment", "1234", "--body", "second", "--batch", "b").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("pr", "b")
    result = invoke("comment", "999", "--body", "wrong target", "--batch", "b")
    assert result.exit_code == 2, result.output
    assert store.draft_load_state("pr", "b") == before
    store.draft_submit("pr", "b", "batch")
    _, parsed, bodies = published(tmp_path)
    assert [a.body for a in parsed.actions] == ["File body\n", "second"]
    assert list(bodies.values()) == ["File body\n"]


def test_discard_removes_only_review_draft(tmp_path):
    assert invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "x").exit_code == 0
    assert invoke("comment", "1234", "--body", "keep", "--batch", "b").exit_code == 0
    assert invoke("review", "discard", "1234").exit_code == 0
    store = Store(tmp_path)
    assert store.draft_load_state("pr", "review-1234") is None
    assert store.draft_load_state("pr", "b") is not None


@pytest.mark.parametrize("ref", ["main", "refs/../bad", "refs/heads/a.lock", "refs/heads/a b", "refs//bad", "refs/heads/a@{1}"])
def test_source_invalid_refs_leave_no_files(tmp_path, ref):
    result = invoke("source", "--repo", "acme/widgets", "--ref", ref, "--sha", "a" * 40, "--pr", "1234")
    assert result.exit_code == 2, result.output
    assert not (tmp_path / ".jailbee/pr-outbox").exists()


def test_caps_preserve_review_draft(tmp_path):
    args = ("review", "add", "1234", "--file", "a.py", "--line", "1", "--body")
    assert invoke(*args, "keep").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("pr", "review-1234")
    assert invoke(*args, "x" * 65537).exit_code == 2
    assert store.draft_load_state("pr", "review-1234") == before
    result = invoke("review", "submit", "1234", "--body", "x" * 65537)
    assert result.exit_code == 2
    assert store.draft_load_state("pr", "review-1234") == before


@pytest.mark.parametrize("args,missing", [
    (("comment",), "body"),
    (("reply",), "--to"),
    (("description",), "title or body"),
    (("review", "add"), "--file"),
    (("review", "add", "--file", "a.py"), "--line"),
    (("source",), "--ref"),
    (("source", "--ref", "refs/heads/a"), "--sha"),
])
def test_missing_values_name_value(tmp_path, args, missing):
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert missing in result.output
    assert not (tmp_path / ".jailbee").exists()


@pytest.mark.parametrize("args", [
    ("comment", "1234", "--body", "inline", "--body-file", "missing"),
    ("comment", "1234", "--body-file", "missing"),
    ("description", "--repo", "acme/widgets", "--title", "Only title"),
    ("source", "--repo", "acme/widgets", "--ref", "refs/heads/a", "--sha", "BAD", "--pr", "1234"),
    ("source", "--repo", "acme/widgets", "--ref", "refs/heads/a", "--sha", "a" * 40, "--base", "bad..base", "--pr", "1234"),
])
def test_invalid_inputs_no_publication(tmp_path, args):
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert not (tmp_path / ".jailbee/pr-outbox").exists()


def test_source_body_file(tmp_path):
    body = tmp_path / "source.md"
    body.write_text("## Source change\n")
    result = invoke("source", "--repo", "acme/widgets", "--ref", "refs/heads/a", "--sha", "a" * 40, "--body-file", str(body))
    assert result.exit_code == 0, result.output
    _, parsed, bodies = published(tmp_path)
    assert parsed.actions[0].body == body.read_text()
    assert list(bodies.values()) == [body.read_text()]


def test_review_submit_summary_body_file(tmp_path):
    body = tmp_path / "summary.md"
    body.write_text("Summary\n")
    assert invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "x").exit_code == 0
    result = invoke("review", "submit", "1234", "--body-file", str(body))
    assert result.exit_code == 0, result.output
    _, parsed, bodies = published(tmp_path)
    assert parsed.actions[0].body == "Summary\n"
    assert list(bodies.values()) == ["Summary\n"]


def test_review_comment_cap_keeps_existing_draft(tmp_path):
    store = Store(tmp_path)
    comment = {"path": "a.py", "line": 1, "body": "keep"}
    store.draft_save("pr", "review-1234", {"version": 1, "repo": "acme/widgets", "pr": 1234, "head_sha": "old-head", "actions": [{"type": "review", "body": "", "comments": [comment] * 100}]}, {})
    before = store.draft_load_state("pr", "review-1234")
    result = invoke("review", "add", "1234", "--file", "a.py", "--line", "2", "--body", "one too many")
    assert result.exit_code == 2, result.output
    assert "100" in result.output
    assert store.draft_load_state("pr", "review-1234") == before


def test_failed_submit_keeps_body_files(monkeypatch, tmp_path):
    body = tmp_path / "comment.md"
    body.write_text("Keep file body\n")
    assert invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body-file", str(body)).exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("pr", "review-1234")
    def fail(number):
        raise gh.GhError("lookup failed")
    monkeypatch.setattr(gh, "pr_view", fail)
    result = invoke("review", "submit", "1234")
    assert result.exit_code == 1
    assert store.draft_load_state("pr", "review-1234") == before


def test_concurrent_review_adds_preserve_comments_and_bodies(monkeypatch, tmp_path):
    from jailbee.container_outbox import pr_cmds
    store = Store(tmp_path)
    monkeypatch.setattr(pr_cmds, "_guard", lambda: store)
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
    body = tmp_path / "comment.md"
    body.write_text("First file body")
    def add(index):
        if index == 2:
            second_started.set()
        pr_cmds.review_add("1234", file="a.py", line=index, body=None if index == 1 else "Second", body_file=body if index == 1 else None)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(add, 1)
        assert entered.wait(5)
        second = executor.submit(add, 2)
        assert second_started.wait(5)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert invoke("review", "submit", "1234", "--body", "Summary").exit_code == 0
    _, parsed, _ = published(tmp_path)
    assert [c.body for c in parsed.actions[0].comments] == ["First file body", "Second"]


def test_submit_serializes_with_concurrent_review_add(monkeypatch, tmp_path):
    from jailbee.container_outbox import pr_cmds
    store = Store(tmp_path)
    monkeypatch.setattr(pr_cmds, "_guard", lambda: store)
    pr_cmds.review_add("1234", file="a.py", line=1, body="First")
    entered, release, add_started = Event(), Event(), Event()
    original = store.write_manifest
    def paused_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "write_manifest", paused_write)
    def add():
        add_started.set()
        pr_cmds.review_add("1234", file="a.py", line=2, body="Second")
    with ThreadPoolExecutor(max_workers=2) as executor:
        submit = executor.submit(pr_cmds.review_submit, "1234")
        assert entered.wait(5)
        append = executor.submit(add)
        assert add_started.wait(5)
        release.set()
        submit.result(timeout=5)
        append.result(timeout=5)
    assert [c.body for c in published(tmp_path)[1].actions[0].comments] == ["First"]
    manifest, bodies = store.draft_load_state("pr", "review-1234")
    assert [c.body for c in parse_pure("draft.json", json.dumps(manifest), bodies).actions[0].comments] == ["Second"]


def test_twenty_pending_manifests_refuse_submit_without_losing_draft(tmp_path):
    store = Store(tmp_path)
    for index in range(20):
        store.write_manifest("pr", "comment", {"version": 1, "repo": "acme/widgets", "pr": 1234, "head_sha": "head", "actions": [{"type": "comment", "body": str(index)}]}, {})
    assert invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "keep").exit_code == 0
    before = store.draft_load_state("pr", "review-1234")
    result = invoke("review", "submit", "1234")
    assert result.exit_code == 2, result.output
    assert "20" in result.output
    assert store.draft_load_state("pr", "review-1234") == before
    assert len(list(store.outbox("pr").glob("*.json"))) == 20


def test_source_repo_inference_uses_explicit_number(monkeypatch, tmp_path):
    def view(number):
        assert number == 1234
        return gh.PrView(1234, "head", "acme/widgets")
    monkeypatch.setattr(gh, "pr_view", view)
    result = invoke("source", "--ref", "refs/heads/a", "--sha", "a" * 40, "--pr", "1234")
    assert result.exit_code == 0, result.output
    raw, _, _ = published(tmp_path)
    assert raw["repo"] == "acme/widgets"
    assert raw["head_sha"] is None


@pytest.mark.parametrize("command", ["add", "submit", "discard"])
def test_review_commands_do_not_overwrite_a_named_batch(tmp_path, command):
    assert invoke("comment", "1234", "--body", "Keep", "--batch", "review-1234").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("pr", "review-1234")
    args = ["review", command, "1234"]
    if command == "add":
        args += ["--file", "a.py", "--line", "1", "--body", "comment"]
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert "review draft" in result.output
    assert store.draft_load_state("pr", "review-1234") == before
    assert not list(store.outbox("pr").glob("*.json"))


@pytest.mark.parametrize("command", ["comment", "reply", "description"])
def test_batch_cannot_append_to_review_draft(tmp_path, command):
    body = tmp_path / "comment.md"
    body.write_text("Original review comment\n")
    assert invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body-file", str(body)).exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("pr", "review-1234")
    args = [command, "1234", "--body", "Not a review", "--batch", "review-1234"]
    if command == "reply":
        args += ["--to", "9"]
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert "review draft" in result.output
    assert store.draft_load_state("pr", "review-1234") == before
    assert invoke("review", "submit", "1234").exit_code == 0
    assert published(tmp_path)[1].actions[0].comments[0].body == body.read_text()


@pytest.mark.parametrize("command", ["add", "submit", "discard"])
def test_review_repo_collision_keeps_original_draft(monkeypatch, tmp_path, command):
    assert invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "Keep").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("pr", "review-1234")
    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(1234, "head", "other/widgets"))
    args = ["review", command, "1234"]
    if command == "add":
        args += ["--file", "a.py", "--line", "2", "--body", "Wrong repo"]
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert "different repo or PR" in result.output
    assert store.draft_load_state("pr", "review-1234") == before
    assert not list(store.outbox("pr").glob("*.json"))


def test_review_discard_checks_pr_inside_draft(tmp_path):
    store = Store(tmp_path)
    store.draft_save("pr", "review-1234", {"version": 1, "repo": "acme/widgets", "pr": 999, "head_sha": "head", "actions": [{"type": "review", "body": "", "comments": []}]}, {})
    before = store.draft_load_state("pr", "review-1234")
    result = invoke("review", "discard", "1234")
    assert result.exit_code == 2, result.output
    assert "different repo or PR" in result.output
    assert store.draft_load_state("pr", "review-1234") == before


def test_discard_validates_while_holding_draft_lock(monkeypatch, tmp_path):
    import fcntl
    from jailbee.container_outbox import pr_cmds
    assert invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "Keep").exit_code == 0
    original = pr_cmds._review_actions
    checked = False
    def check_locked(manifest):
        nonlocal checked
        lock = tmp_path / ".jailbee/outbox-draft-locks/pr/review-1234.lock"
        with lock.open("r") as stream:
            with pytest.raises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checked = True
        return original(manifest)
    monkeypatch.setattr(pr_cmds, "_review_actions", check_locked)
    result = invoke("review", "discard", "1234")
    assert result.exit_code == 0, result.output
    assert checked
    assert Store(tmp_path).draft_load_state("pr", "review-1234") is None


def test_batch_repo_collision_preserves_review_draft(monkeypatch, tmp_path):
    assert invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "Keep").exit_code == 0
    store = Store(tmp_path)
    before = store.draft_load_state("pr", "review-1234")
    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(1234, "head", "other/widgets"))
    result = invoke("comment", "1234", "--body", "Wrong repo", "--batch", "review-1234")
    assert result.exit_code == 2, result.output
    assert "different repo or PR" in result.output
    assert store.draft_load_state("pr", "review-1234") == before


def test_source_prints_source_push_hint(tmp_path):
    result = invoke("source", "--repo", "acme/widgets", "--ref", "refs/heads/a", "--sha", "a" * 40, "--pr", "1234")
    assert result.exit_code == 0, result.output
    manifest = next((tmp_path / ".jailbee/pr-outbox").glob("*.json"))
    import socket
    assert f"jb pr push {socket.gethostname()} --source-manifest {manifest.name}" in result.output
    assert "jb review apply" not in result.output


def test_publication_lock_cannot_collide_with_batch_name(monkeypatch, tmp_path):
    import fcntl
    from jailbee.container_outbox import store as storage
    original = storage.fcntl.flock
    def nonblocking(fd, operation):
        return original(fd, operation | fcntl.LOCK_NB if operation == fcntl.LOCK_EX else operation)
    monkeypatch.setattr(storage.fcntl, "flock", nonblocking)
    result = invoke("comment", "1234", "--body", "Keep", "--batch", "outbox-publication")
    assert result.exit_code == 0, result.output
    store = Store(tmp_path)
    store.draft_submit("pr", "outbox-publication", "batch")
    assert published(tmp_path)[1].actions[0].body == "Keep"


def test_concurrent_publications_enforce_twenty_manifest_cap(tmp_path):
    store = Store(tmp_path)
    manifest = {"version": 1, "repo": "acme/widgets", "pr": 1234, "head_sha": "head", "actions": [{"type": "comment", "body": "x"}]}
    for _ in range(19):
        store.write_manifest("pr", "comment", manifest, {})
    from jailbee.container_outbox.store import OutboxCommandError
    def publish():
        try:
            store.write_manifest("pr", "comment", manifest, {})
            return "published"
        except OutboxCommandError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: publish(), range(2)))
    assert results.count("published") == 1, (results, sorted(p.name for p in store.outbox("pr").iterdir()))
    assert any("20" in result for result in results)
    assert len(list(store.outbox("pr").glob("*.json"))) == 20


def test_real_entry_dispatches_pr(monkeypatch, tmp_path, capsys):
    from jailbee import container_main
    monkeypatch.setattr(container_main.container_deps, "check", lambda: None)
    monkeypatch.setattr(container_main, "PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["jailbee", "pr", "comment", "1234", "--body", "hello"])
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 0
    assert "staged pr/" in capsys.readouterr().out
    assert published(tmp_path)[0]["pr"] == 1234
