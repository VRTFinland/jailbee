"""PR proposals roundtrip the host schema without publishing to GitHub."""

import json
import sys

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.container_outbox import gh
from jailbee.pr_manifest import parse_manifest as parse_pure
from jailbee.pr_outbox import parse_manifest


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(bindings, "SNAPSHOT", tmp_path / "absent-bindings.json")
    monkeypatch.setattr(bindings, "ERROR_MARKER", tmp_path / "absent-refresh-error")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    monkeypatch.setattr(
        gh, "pr_view", lambda number: gh.PrView(number or 1234, "old-head", "acme/widgets")
    )
    monkeypatch.setattr(gh, "run_json", lambda *a, **kw: pytest.fail("unexpected gh call"))


@pytest.fixture(autouse=True)
def broker(monkeypatch, tmp_path):
    """Source proposals are staged through the host broker (in process here)."""
    from tests import container_broker_support

    return container_broker_support.install(monkeypatch, tmp_path / "broker-state")


def invoke(*args):
    return CliRunner().invoke(container_app, ["pr", *args])


def staged_source(broker):
    """The single PR source proposal the broker stored: raw, parsed, bodies."""
    (row,) = broker.proposals("pr")
    bodies = dict(row.bodies)
    parsed = parse_manifest(row.name, row.manifest, bodies)
    assert parse_pure(row.name, row.manifest, bodies) == parsed
    return json.loads(row.manifest), parsed, bodies


def seed_draft(broker, name, action, *, pr=1234, head_sha="old-head", repo="acme/widgets"):
    """Put an action in a PR draft the way the container does, but for any envelope."""
    from jailbee.orchestration import wire

    broker.client().call(
        wire.OUTBOX_DRAFT_APPEND,
        {
            "kind": "pr",
            "name": name,
            "action": action,
            "bodies": {},
            "envelope": {"repo": repo, "pr": pr, "head_sha": head_sha},
        },
        timeout=None,
    )


def test_three_comments_one_review_preserves_anchor_and_body_files(monkeypatch, tmp_path, broker):
    body_file = tmp_path / "input.md"
    body_file.write_text("Keep this multiline\ncomment intact.\n")
    for args in [
        ("--line", "88", "--body", "First"),
        ("--line", "134", "--start-line", "120", "--side", "LEFT", "--body-file", str(body_file)),
        ("--line", "200", "--body", "Third"),
    ]:
        result = invoke("review", "add", "1234", "--file", "src/worktime.py", *args)
        assert result.exit_code == 0, result.output
    result = invoke("review", "submit", "1234")
    assert result.exit_code == 0, result.output
    raw, parsed, bodies = staged_source(broker)
    assert raw["head_sha"] == "old-head"
    assert len(raw["actions"]) == 1
    review = parsed.actions[0]
    assert review.event == "COMMENT"
    assert review.body == ""  # The host parser permits an empty optional summary.
    assert [c.body for c in review.comments] == ["First", body_file.read_text(), "Third"]
    assert review.comments[1].start_line == 120
    assert review.comments[1].side == "LEFT"
    assert list(bodies.values()) == [body_file.read_text()]
    assert broker.draft_state("review-1234", "pr") is None


@pytest.mark.parametrize(
    "args,expected",
    [
        (
            ("reply", "1234", "--to", "99887", "--body", "Reply"),
            {"type": "reply", "comment_id": 99887, "body": "Reply"},
        ),
        (
            ("comment", "1234", "--reply-to", "4455", "--body", "General"),
            {"type": "comment", "reply_to": 4455, "body": "General"},
        ),
        (
            ("description", "1234", "--title", "Improve onboarding", "--body", "Description"),
            {"type": "description", "title": "Improve onboarding", "body": "Description"},
        ),
        (
            (
                "description",
                "--new",
                "--repo",
                "acme/widgets",
                "--title",
                "New PR",
                "--body",
                "Description",
                "--branch",
                "fix/onboarding",
            ),
            {
                "type": "description",
                "title": "New PR",
                "body": "Description",
                "branch": "fix/onboarding",
            },
        ),
    ],
)
def test_schema_actions(monkeypatch, tmp_path, args, expected, broker):
    from jailbee.container_outbox import bindings

    if "--new" in args:
        monkeypatch.setattr(
            bindings, "SNAPSHOT", binding_snapshot(tmp_path, {"user.jailbee.repo_dir": "/repo"})
        )
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    raw, _, _ = staged_source(broker)
    assert raw == {
        "version": 1,
        "repo": "acme/widgets",
        "pr": None if args[1].startswith("--") else 1234,
        "head_sha": None if args[1].startswith("--") else "old-head",
        "actions": [expected],
    }


@pytest.mark.parametrize("operation", ["add", "submit"])
def test_changed_review_head_refuses_without_changing_draft(
    monkeypatch, tmp_path, operation, broker
):
    assert (
        invoke(
            "review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "First"
        ).exit_code
        == 0
    )
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in (tmp_path / ".jailbee/outbox-drafts").rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(1234, "new-head", "acme/widgets"))
    args = ("--file", "b.py", "--line", "2", "--body", "Second") if operation == "add" else ()
    result = invoke("review", operation, "1234", *args)
    assert result.exit_code == 2, result.output
    assert "head moved" in result.output
    assert {
        p.relative_to(tmp_path): p.read_bytes()
        for p in (tmp_path / ".jailbee/outbox-drafts").rglob("*")
        if p.is_file()
    } == before
    assert broker.proposals("pr") == []


def test_submitted_review_keeps_original_anchor_for_real_host_gate(
    monkeypatch,
    tmp_path,
    mocker,
    make_cfg,
    broker,
):
    from jailbee.pr_outbox import StaleError, resolve_target
    from tests.test_pr_outbox import _pr_info, _target_setup

    assert (
        invoke(
            "review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "First"
        ).exit_code
        == 0
    )
    assert invoke("review", "submit", "1234").exit_code == 0
    manifest = staged_source(broker)[1]
    incus = _target_setup(mocker, tmp_path, pr=_pr_info(head_sha="later-head"))
    with pytest.raises(StaleError, match="head moved"):
        resolve_target(make_cfg(tmp_path), incus, "c", manifest, force=False)


@pytest.mark.parametrize("alias", ["--ref", "--head"])
@pytest.mark.parametrize("numbered", [False, True])
def test_source_schema_examples_without_existing_pr(monkeypatch, tmp_path, broker, alias, numbered):
    monkeypatch.setattr(
        gh, "pr_view", lambda *a: pytest.fail("source with repo must not need a PR")
    )
    ref = (
        "refs/jailbee/from/rework-12/fix/issue-12"
        if numbered
        else "refs/jailbee/from/fix-12/fix/issue-12"
    )
    sha = (
        "abcdef0123456789abcdef0123456789abcdef01"
        if numbered
        else "0123456789abcdef0123456789abcdef01234567"
    )
    args = ["source", alias, ref, "--sha", sha, "--repo", "acme/widgets"]
    if numbered:
        args += ["--pr", "1234"]
    else:
        args += [
            "--base",
            "main",
            "--title",
            "Fix issue 12",
            "--body",
            "Fix the reported issue.",
            "--branch",
            "fix/issue-12",
        ]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    raw, _, _ = staged_source(broker)
    assert not (tmp_path / ".jailbee/pr-outbox").exists()
    expected = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": 1234 if numbered else None,
        "head_sha": None,
        "source": ref,
        "source_sha": sha,
        "actions": [],
    }
    if not numbered:
        expected.update(
            base="main",
            actions=[
                {
                    "type": "description",
                    "title": "Fix issue 12",
                    "body": "Fix the reported issue.",
                    "branch": "fix/issue-12",
                }
            ],
        )
    assert raw == expected


@pytest.mark.parametrize("start", [10, 11])
def test_invalid_span_before_draft_write(tmp_path, start):
    result = invoke(
        "review",
        "add",
        "1234",
        "--file",
        "a.py",
        "--line",
        "10",
        "--start-line",
        str(start),
        "--body",
        "bad",
    )
    assert result.exit_code == 2, result.output
    assert "start_line" in result.output
    assert not (tmp_path / ".jailbee/outbox-drafts").exists()


COMMANDS = [
    ("comment",),
    ("reply",),
    ("description",),
    ("description", "--title", "Title only"),
    ("description", "1234", "--title", "Title only"),
    ("review", "add"),
    ("review", "submit"),
    ("review", "discard"),
    ("source",),
    ("comment", "1234", "--body-file", "missing"),
    ("reply", "1234", "--to", "9", "--body-file", "missing"),
    ("description", "1234", "--title", "Title", "--body-file", "missing"),
    ("review", "add", "1234", "--file", "a.py", "--line", "1", "--body-file", "missing"),
    ("review", "submit", "1234", "--body-file", "missing"),
    ("review", "discard", "1234"),
    (
        "source",
        "--repo",
        "acme/widgets",
        "--ref",
        "refs/heads/a",
        "--sha",
        "a" * 40,
        "--body-file",
        "missing",
    ),
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


@pytest.mark.parametrize(
    "command",
    [
        ("comment", "--body", "hello"),
        ("reply", "--to", "9", "--body", "hello"),
        ("review", "discard"),
    ],
)
def test_omitted_number_resolves_bound_pr(monkeypatch, tmp_path, command, broker):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(
        bindings,
        "SNAPSHOT",
        binding_snapshot(tmp_path, {"user.jailbee.repo_dir": "/repo", "user.jailbee.pr": "1234"}),
    )

    def view(number, *, cwd=None):
        assert number == 1234 and cwd == "/repo"
        return gh.PrView(1234, "head", "acme/widgets")

    monkeypatch.setattr(gh, "pr_view", view)
    if command[-1] == "discard":
        # Discard needs a review draft for the bound PR to exist.
        assert (
            invoke("review", "add", "--file", "a.py", "--line", "1", "--body", "x").exit_code == 0
        )
        assert broker.draft_state("review-1234", "pr") is not None
    result = invoke(*command)
    assert result.exit_code == 0, result.output
    if command[-1] == "discard":
        assert broker.draft_state("review-1234", "pr") is None
    else:
        assert staged_source(broker)[0]["pr"] == 1234


@pytest.mark.parametrize("explicit,code", [(False, 2), (True, 1)])
def test_lookup_failure_distinguishes_missing_target(monkeypatch, tmp_path, explicit, code):
    def fail(number):
        raise gh.GhError("lookup failed; retry `gh pr view`")

    monkeypatch.setattr(gh, "pr_view", fail)
    result = invoke("comment", *(("1234",) if explicit else ()), "--body", "hello")
    assert result.exit_code == code, result.output
    if explicit:
        assert "lookup failed" in result.output
    else:
        assert "name the PR number" in result.output
    assert not (tmp_path / ".jailbee").exists()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_title_only_description_refuses_without_replaying_body(
    monkeypatch,
    tmp_path,
    explicit,
    batch,
    broker,
):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(
        bindings,
        "SNAPSHOT",
        binding_snapshot(tmp_path, {"user.jailbee.repo_dir": "/repo", "user.jailbee.pr": "1234"}),
    )
    if batch:
        monkeypatch.setattr(gh, "pr_view", lambda n, **kw: gh.PrView(n, "head", "acme/widgets"))
        assert invoke("comment", "1234", "--body", "Keep", "--batch", "b").exit_code == 0
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in (tmp_path / ".jailbee").rglob("*")
        if p.is_file()
    }
    reads = []

    def current_body(*args, **kwargs):
        reads.append(True)
        return "Stage-time body that must not be replayed"

    monkeypatch.setattr(gh, "pr_body", current_body)
    monkeypatch.setattr(gh, "pr_view", lambda n, **kw: gh.PrView(n, "head", "acme/widgets"))
    result = invoke(
        "description",
        *(("1234",) if explicit else ()),
        "--title",
        "New title",
        *(("--batch", "b") if batch else ()),
    )
    assert result.exit_code == 2, result.output
    assert "--body" in result.output and "--body-file" in result.output
    assert "title-only" in result.output
    assert reads == []
    assert broker.proposals("pr") == []
    assert {
        p.relative_to(tmp_path): p.read_bytes()
        for p in (tmp_path / ".jailbee").rglob("*")
        if p.is_file()
    } == before


@pytest.mark.parametrize("file_body", [False, True])
@pytest.mark.parametrize("text", ["", "Explicit replacement"])
def test_description_explicit_body_including_empty_keeps_schema(
    monkeypatch,
    tmp_path,
    file_body,
    text,
    broker,
):
    monkeypatch.setattr(gh, "pr_body", lambda *a, **kw: pytest.fail("automatic body read"))
    if file_body:
        body = tmp_path / "input.md"
        body.write_text(text)
        args = ("--body-file", str(body))
    else:
        args = ("--body", text)
    result = invoke("description", "1234", "--title", "New title", *args)
    assert result.exit_code == 0, result.output
    raw, parsed, _ = staged_source(broker)
    assert parsed.actions[0].body == text
    assert parsed.actions[0].title == "New title"
    assert set(raw["actions"][0]) == {"type", "title", "body_file" if file_body else "body"}


@pytest.mark.parametrize("command", ["comment", "reply", "description"])
def test_batches_keep_body_files_and_target(monkeypatch, tmp_path, command, broker):
    body = tmp_path / "input.md"
    body.write_text("File body\n")
    args = [command, "1234", "--body-file", str(body), "--batch", "b"]
    if command == "reply":
        args += ["--to", "9"]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert invoke("comment", "1234", "--body", "second", "--batch", "b").exit_code == 0
    before = broker.draft_state("b", "pr")
    result = invoke("comment", "999", "--body", "wrong target", "--batch", "b")
    assert result.exit_code == 2, result.output
    assert broker.draft_state("b", "pr") == before
    assert (
        CliRunner().invoke(container_app, ["outbox", "submit", "b", "--kind", "pr"]).exit_code == 0
    )
    assert broker.draft_state("b", "pr") is None
    _, parsed, bodies = staged_source(broker)
    assert [a.body for a in parsed.actions] == ["File body\n", "second"]
    assert list(bodies.values()) == ["File body\n"]
    assert broker.proposals("pr")[0].name == "001-b.json"


def test_discard_removes_only_review_draft(tmp_path, broker):
    assert (
        invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "x").exit_code
        == 0
    )
    assert invoke("comment", "1234", "--body", "keep", "--batch", "b").exit_code == 0
    assert invoke("review", "discard", "1234").exit_code == 0
    assert broker.draft_state("review-1234", "pr") is None
    assert broker.draft_state("b", "pr") is not None


@pytest.mark.parametrize(
    "ref",
    ["main", "refs/../bad", "refs/heads/a.lock", "refs/heads/a b", "refs//bad", "refs/heads/a@{1}"],
)
def test_source_invalid_refs_leave_no_files(tmp_path, broker, ref):
    result = invoke(
        "source", "--repo", "acme/widgets", "--ref", ref, "--sha", "a" * 40, "--pr", "1234"
    )
    assert result.exit_code == 2, result.output
    assert not (tmp_path / ".jailbee/pr-outbox").exists()
    assert broker.proposals("pr") == []


def test_caps_preserve_review_draft(tmp_path, broker):
    args = ("review", "add", "1234", "--file", "a.py", "--line", "1", "--body")
    assert invoke(*args, "keep").exit_code == 0
    before = broker.draft_state("review-1234", "pr")
    assert invoke(*args, "x" * 65537).exit_code == 2
    assert broker.draft_state("review-1234", "pr") == before
    result = invoke("review", "submit", "1234", "--body", "x" * 65537)
    assert result.exit_code == 2
    assert broker.draft_state("review-1234", "pr") == before


@pytest.mark.parametrize(
    "args,missing",
    [
        (("comment",), "body"),
        (("reply",), "--to"),
        (("description",), "title or body"),
        (("review", "add"), "--file"),
        (("review", "add", "--file", "a.py"), "--line"),
        (("source",), "--ref"),
        (("source", "--ref", "refs/heads/a"), "--sha"),
    ],
)
def test_missing_values_name_value(tmp_path, args, missing):
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert missing in result.output
    assert not (tmp_path / ".jailbee").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("comment", "1234", "--body", "inline", "--body-file", "missing"),
        ("comment", "1234", "--body-file", "missing"),
        ("description", "--new", "--repo", "acme/widgets", "--title", "Only title"),
        (
            "source",
            "--repo",
            "acme/widgets",
            "--ref",
            "refs/heads/a",
            "--sha",
            "BAD",
            "--pr",
            "1234",
        ),
        (
            "source",
            "--repo",
            "acme/widgets",
            "--ref",
            "refs/heads/a",
            "--sha",
            "a" * 40,
            "--base",
            "bad..base",
            "--pr",
            "1234",
        ),
    ],
)
def test_invalid_inputs_no_publication(tmp_path, broker, args):
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert not (tmp_path / ".jailbee/pr-outbox").exists()
    assert broker.proposals("pr") == []


def test_source_body_file(tmp_path, broker):
    body = tmp_path / "source.md"
    body.write_text("## Source change\n")
    result = invoke(
        "source",
        "--repo",
        "acme/widgets",
        "--ref",
        "refs/heads/a",
        "--sha",
        "a" * 40,
        "--body-file",
        str(body),
    )
    assert result.exit_code == 0, result.output
    _, parsed, bodies = staged_source(broker)
    assert parsed.actions[0].body == body.read_text()
    assert list(bodies.values()) == [body.read_text()]


def test_review_submit_summary_body_file(tmp_path, broker):
    body = tmp_path / "summary.md"
    body.write_text("Summary\n")
    assert (
        invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "x").exit_code
        == 0
    )
    result = invoke("review", "submit", "1234", "--body-file", str(body))
    assert result.exit_code == 0, result.output
    _, parsed, bodies = staged_source(broker)
    assert parsed.actions[0].body == "Summary\n"
    assert list(bodies.values()) == ["Summary\n"]


def test_review_comment_cap_keeps_existing_draft(tmp_path, broker):
    comment = {"path": "a.py", "line": 1, "body": "keep"}
    seed_draft(
        broker,
        "review-1234",
        {"type": "review", "event": "COMMENT", "body": "", "comments": [comment] * 100},
    )
    before = broker.draft_state("review-1234", "pr")
    result = invoke(
        "review", "add", "1234", "--file", "a.py", "--line", "2", "--body", "one too many"
    )
    assert result.exit_code == 2, result.output
    assert "100" in result.output
    assert broker.draft_state("review-1234", "pr") == before


def test_failed_submit_keeps_body_files(monkeypatch, tmp_path, broker):
    body = tmp_path / "comment.md"
    body.write_text("Keep file body\n")
    assert (
        invoke(
            "review", "add", "1234", "--file", "a.py", "--line", "1", "--body-file", str(body)
        ).exit_code
        == 0
    )
    before = broker.draft_state("review-1234", "pr")

    def fail(number):
        raise gh.GhError("lookup failed")

    monkeypatch.setattr(gh, "pr_view", fail)
    result = invoke("review", "submit", "1234")
    assert result.exit_code == 1
    assert broker.draft_state("review-1234", "pr") == before


def test_two_review_adds_both_land_in_the_draft_with_bodies(tmp_path, broker):
    body = tmp_path / "comment.md"
    body.write_text("First file body")
    first = invoke(
        "review", "add", "1234", "--file", "a.py", "--line", "1", "--body-file", str(body)
    )
    second = invoke("review", "add", "1234", "--file", "a.py", "--line", "2", "--body", "Second")
    assert first.exit_code == 0 and second.exit_code == 0
    manifest, bodies = broker.draft_state("review-1234", "pr")
    (review,) = manifest["actions"]
    assert [c["line"] for c in review["comments"]] == [1, 2]
    assert list(bodies.values()) == ["First file body"]
    assert invoke("review", "submit", "1234", "--body", "Summary").exit_code == 0
    _, parsed, _ = staged_source(broker)
    assert [c.body for c in parsed.actions[0].comments] == ["First file body", "Second"]
    assert parsed.actions[0].body == "Summary"


def test_twenty_pending_manifests_refuse_submit_without_losing_draft(tmp_path, broker):
    for index in range(20):
        assert invoke("comment", "1234", "--body", str(index)).exit_code == 0
    assert (
        invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "keep").exit_code
        == 0
    )
    before = broker.draft_state("review-1234", "pr")
    result = invoke("review", "submit", "1234")
    assert result.exit_code == 2, result.output
    assert "cap of 20" in result.output
    assert broker.draft_state("review-1234", "pr") == before
    assert len(broker.proposals("pr")) == 20


def test_source_repo_inference_uses_explicit_number(monkeypatch, tmp_path, broker):
    def view(number):
        assert number == 1234
        return gh.PrView(1234, "head", "acme/widgets")

    monkeypatch.setattr(gh, "pr_view", view)
    result = invoke("source", "--ref", "refs/heads/a", "--sha", "a" * 40, "--pr", "1234")
    assert result.exit_code == 0, result.output
    raw, _, _ = staged_source(broker)
    assert raw["repo"] == "acme/widgets"
    assert raw["head_sha"] is None


@pytest.mark.parametrize("command", ["add", "submit", "discard"])
def test_review_commands_do_not_overwrite_a_named_batch(tmp_path, command, broker):
    assert invoke("comment", "1234", "--body", "Keep", "--batch", "review-1234").exit_code == 0
    before = broker.draft_state("review-1234", "pr")
    args = ["review", command, "1234"]
    if command == "add":
        args += ["--file", "a.py", "--line", "1", "--body", "comment"]
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert "review draft" in result.output
    assert broker.draft_state("review-1234", "pr") == before
    assert broker.proposals("pr") == []


@pytest.mark.parametrize("command", ["comment", "reply", "description"])
def test_batch_cannot_append_to_review_draft(tmp_path, command, broker):
    body = tmp_path / "comment.md"
    body.write_text("Original review comment\n")
    assert (
        invoke(
            "review", "add", "1234", "--file", "a.py", "--line", "1", "--body-file", str(body)
        ).exit_code
        == 0
    )
    before = broker.draft_state("review-1234", "pr")
    args = [command, "1234", "--body", "Not a review", "--batch", "review-1234"]
    if command == "reply":
        args += ["--to", "9"]
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert "review draft" in result.output
    assert broker.draft_state("review-1234", "pr") == before
    assert invoke("review", "submit", "1234").exit_code == 0
    assert staged_source(broker)[1].actions[0].comments[0].body == body.read_text()


@pytest.mark.parametrize(
    "command,message",
    [
        ("add", "different repo or PR"),
        # The broker's submit sees only the head sha, so the moved head is what refuses it.
        ("submit", "different repo or PR"),
        ("discard", "different repo or PR"),
    ],
)
def test_review_repo_collision_keeps_original_draft(
    monkeypatch, tmp_path, broker, command, message
):
    assert (
        invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "Keep").exit_code
        == 0
    )
    before = broker.draft_state("review-1234", "pr")
    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(1234, "head", "other/widgets"))
    args = ["review", command, "1234"]
    if command == "add":
        args += ["--file", "a.py", "--line", "2", "--body", "Wrong repo"]
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert broker.draft_state("review-1234", "pr") == before
    assert broker.proposals("pr") == []


def test_review_discard_checks_pr_inside_draft(tmp_path, broker):
    seed_draft(
        broker,
        "review-1234",
        {"type": "review", "event": "COMMENT", "body": "", "comments": []},
        pr=999,
        head_sha="head",
    )
    before = broker.draft_state("review-1234", "pr")
    result = invoke("review", "discard", "1234")
    assert result.exit_code == 2, result.output
    assert "different repo or PR" in result.output
    assert broker.draft_state("review-1234", "pr") == before


def test_batch_repo_collision_preserves_review_draft(monkeypatch, tmp_path, broker):
    assert (
        invoke("review", "add", "1234", "--file", "a.py", "--line", "1", "--body", "Keep").exit_code
        == 0
    )
    before = broker.draft_state("review-1234", "pr")
    monkeypatch.setattr(gh, "pr_view", lambda n: gh.PrView(1234, "head", "other/widgets"))
    result = invoke("comment", "1234", "--body", "Wrong repo", "--batch", "review-1234")
    assert result.exit_code == 2, result.output
    assert "different repo or PR" in result.output
    assert broker.draft_state("review-1234", "pr") == before


def test_source_prints_source_push_hint(tmp_path, broker):
    result = invoke(
        "source",
        "--repo",
        "acme/widgets",
        "--ref",
        "refs/heads/a",
        "--sha",
        "a" * 40,
        "--pr",
        "1234",
    )
    assert result.exit_code == 0, result.output
    (row,) = broker.proposals("pr")
    assert row.name == "001-source.json"
    import socket

    assert f"jb pr push {socket.gethostname()} --source-manifest {row.name}" in result.output
    assert "jb review apply" not in result.output


@pytest.mark.parametrize("option", ["--to", "--reply-to", "--line", "--start-line"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_positive_ids_and_lines_before_lookup(monkeypatch, tmp_path, option, value):
    monkeypatch.setattr(gh, "pr_view", lambda *a, **kw: pytest.fail("lookup before validation"))
    if option == "--to":
        args = ["reply", "1234", "--body", "x"]
    elif option == "--reply-to":
        args = ["comment", "1234", "--body", "x"]
    else:
        args = ["review", "add", "1234", "--file", "a.py", "--body", "x"]
        if option == "--start-line":
            args += ["--line", "10"]
    result = invoke(*args, option, value)
    assert result.exit_code == 2, result.output
    assert "positive" in result.output
    assert not (tmp_path / ".jailbee").exists()


def binding_snapshot(tmp_path, config):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(config))
    return path


@pytest.mark.parametrize("branch", ["", "unrelated-checkout"])
def test_omitted_pr_uses_binding_not_checkout(monkeypatch, tmp_path, branch, broker):
    from jailbee.container_outbox import bindings

    path = binding_snapshot(
        tmp_path,
        {"user.jailbee.pr": "42", "user.jailbee.repo_dir": "/repo", "user.jailbee.branch": branch},
    )
    monkeypatch.setattr(bindings, "SNAPSHOT", path)

    def view(number, *, cwd=None):
        assert number == 42 and cwd == "/repo"
        return gh.PrView(42, "fresh", "acme/widgets")

    monkeypatch.setattr(gh, "pr_view", view)
    result = invoke("comment", "--body", "bound")
    assert result.exit_code == 0, result.output
    assert staged_source(broker)[0]["pr"] == 42


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"user.jailbee.pr": "42", "user.jailbee.stacked_pr": "43"},
        {"user.jailbee.pr": "42", "user.jailbee.sub_pr": '{"lib":{"pr":42}}'},
        {"user.jailbee.pr": "42", "user.jailbee.branch_prs": '{"head":{"pr":43}}'},
    ],
)
def test_omitted_pr_requires_unique_actual_binding(monkeypatch, tmp_path, config):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(
        bindings,
        "SNAPSHOT",
        binding_snapshot(tmp_path, {"user.jailbee.repo_dir": "/repo", **config}),
    )
    monkeypatch.setattr(gh, "pr_view", lambda *a, **kw: pytest.fail("ambiguous binding lookup"))
    result = invoke("comment", "--body", "x")
    assert result.exit_code == 2, result.output
    assert "name the PR number" in result.output


@pytest.mark.parametrize(
    "key,value,cwd",
    [
        ("user.jailbee.stacked_pr", "43", "/repo"),
        ("user.jailbee.sub_pr", '{"lib":{"pr":43}}', "/repo/lib"),
    ],
)
def test_omitted_pr_resolves_stacked_or_submodule(monkeypatch, tmp_path, key, value, cwd, broker):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(
        bindings,
        "SNAPSHOT",
        binding_snapshot(tmp_path, {"user.jailbee.repo_dir": "/repo", key: value}),
    )

    def view(number, **kwargs):
        assert number == 43 and kwargs == {"cwd": cwd}
        return gh.PrView(43, "fresh", "acme/lib")

    monkeypatch.setattr(gh, "pr_view", view)
    result = invoke("description", "--body", "Existing")
    assert result.exit_code == 0, result.output
    assert staged_source(broker)[0]["pr"] == 43


@pytest.mark.parametrize("explicit_new", [True, False])
def test_new_description_repo_lookup_does_not_need_pr(monkeypatch, tmp_path, explicit_new, broker):
    from jailbee.container_outbox import bindings

    config = {"user.jailbee.repo_dir": "/repo"}
    monkeypatch.setattr(bindings, "SNAPSHOT", binding_snapshot(tmp_path, config))
    monkeypatch.setattr(gh, "pr_view", lambda *a, **kw: pytest.fail("new description needs no PR"))
    monkeypatch.setattr(
        gh,
        "run_json",
        lambda argv, **kw: (
            {"nameWithOwner": "acme/widgets"}
            if argv == ["gh", "repo", "view", "--json", "nameWithOwner"]
            else pytest.fail(str(argv))
        ),
    )
    result = invoke(
        "description", *(("--new",) if explicit_new else ()), "--title", "New", "--body", "Body"
    )
    assert result.exit_code == 0, result.output
    assert staged_source(broker)[0]["pr"] is None


@pytest.mark.parametrize(
    "config",
    [
        {"user.jailbee.pr": "bad"},
        {"user.jailbee.sub_pr": '{"../outside":{"pr":1}}'},
        {"user.jailbee.sub_pr": "[]"},
        {"error": "unavailable"},
    ],
)
def test_invalid_binding_snapshot_fails_closed(monkeypatch, tmp_path, config):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(
        bindings,
        "SNAPSHOT",
        binding_snapshot(tmp_path, {"user.jailbee.repo_dir": "/repo", **config}),
    )
    monkeypatch.setattr(gh, "pr_view", lambda *a, **kw: pytest.fail("invalid snapshot lookup"))
    result = invoke("comment", "--body", "x")
    assert result.exit_code == 2, result.output


def test_same_actual_pr_binding_is_unique(monkeypatch, tmp_path, broker):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(
        bindings,
        "SNAPSHOT",
        binding_snapshot(
            tmp_path,
            {
                "user.jailbee.repo_dir": "/repo",
                "user.jailbee.pr": "42",
                "user.jailbee.stacked_pr": "42",
            },
        ),
    )
    monkeypatch.setattr(gh, "pr_view", lambda n, **kw: gh.PrView(n, "fresh", "acme/widgets"))
    assert invoke("comment", "--body", "x").exit_code == 0
    assert staged_source(broker)[0]["pr"] == 42


@pytest.mark.parametrize(
    "config",
    [
        {"user.jailbee.pr": "1234"},
        {"user.jailbee.pr": "1234", "user.jailbee.stacked_pr": "1300"},
        {"user.jailbee.sub_pr": '{"lib":{"pr":42}}'},
        {"user.jailbee.branch_prs": '{"topic":{"pr":99}}'},
    ],
)
def test_explicit_new_refuses_any_binding_before_staging(monkeypatch, tmp_path, config, broker):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(
        bindings,
        "SNAPSHOT",
        binding_snapshot(tmp_path, {"user.jailbee.repo_dir": "/repo", **config}),
    )
    result = invoke("description", "--new", "--repo", "other/repo", "--body", "New")
    assert result.exit_code == 2, result.output
    assert "--new" in result.output and "binding" in result.output
    assert broker.proposals("pr") == []


def test_explicit_new_cannot_reach_existing_pr_through_host_gate(
    monkeypatch,
    tmp_path,
    mocker,
    make_cfg,
    broker,
):
    from jailbee.container_outbox import bindings
    from jailbee.pr_outbox import resolve_target
    from tests.test_pr_outbox import _null_pr_manifest, _target_setup

    incus = _target_setup(mocker, tmp_path)
    target = resolve_target(make_cfg(tmp_path), incus, "c", _null_pr_manifest(), force=False)
    assert target.pr.number == 1234  # Null does not force creation on the host.
    monkeypatch.setattr(
        bindings,
        "SNAPSHOT",
        binding_snapshot(tmp_path, {"user.jailbee.repo_dir": "/repo", "user.jailbee.pr": "1234"}),
    )
    result = invoke("description", "--new", "--repo", "acme/widgets", "--body", "New")
    assert result.exit_code == 2, result.output
    assert broker.proposals("pr") == []


@pytest.mark.parametrize("snapshot", [None, "not json", "[]", '{"error":"unavailable"}'])
def test_explicit_new_requires_readable_empty_bindings(monkeypatch, tmp_path, snapshot, broker):
    from jailbee.container_outbox import bindings

    path = tmp_path / "bindings.json"
    if snapshot is not None:
        path.write_text(snapshot)
    monkeypatch.setattr(bindings, "SNAPSHOT", path)
    result = invoke("description", "--new", "--repo", "acme/widgets", "--body", "New")
    assert result.exit_code == 2, result.output
    assert broker.proposals("pr") == []


def test_missing_snapshot_never_assumes_new_description(monkeypatch, tmp_path):
    from jailbee.container_outbox import bindings

    monkeypatch.setattr(bindings, "SNAPSHOT", tmp_path / "absent")
    result = invoke("description", "--body", "x")
    assert result.exit_code == 2 and "bindings" in result.output
    assert not (tmp_path / ".jailbee/pr-outbox").exists()


def test_description_rejects_new_with_number(tmp_path):
    result = invoke("description", "1234", "--new", "--body", "x")
    assert result.exit_code == 2 and "--new" in result.output


def test_real_entry_dispatches_pr(monkeypatch, tmp_path, capsys, broker):
    from jailbee import container_main

    monkeypatch.setattr(container_main.container_deps, "check", lambda: None)
    monkeypatch.setattr(container_main, "PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["jailbee", "pr", "comment", "1234", "--body", "hello"])
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 0
    assert "staged pr/" in capsys.readouterr().out
    assert staged_source(broker)[0]["pr"] == 1234
