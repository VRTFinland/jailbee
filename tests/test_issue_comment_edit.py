"""Editing one's own issue / PR conversation comment through the issue outbox.

Seam: the host publish path (`prepare_batch` → `plan_lines` → `revalidate_batch`
→ `apply_batch`) with GitHub reached only through a fake ``gh api`` process.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest

from tests.test_issue_outbox import (  # noqa: F401 - pytest fixtures
    _reconcile,
    _seed_execution,
    _sent_rows,
    execution,
    preflight,
)

COMMENT_API = "repos/acme/app/issues/comments/555"


def _action(**changes):
    return {
        "type": "comment_edit",
        "repo": ".",
        "issue": 7,
        "comment_id": 555,
        "body": "Fixed wording\nsecond line",
        "expected": {"body": "Old wording\nsecond line"},
        **changes,
    }


class FakeGh:
    """A GitHub holding one comment, answering only ``gh api`` on that comment."""

    def __init__(self, *, author="alice", body="Old wording\nsecond line", pull=False):
        kind = "pull" if pull else "issues"
        self.comment = {
            "id": 555,
            "user": {"login": author},
            "body": body,
            "html_url": f"https://github.com/acme/app/{kind}/7#issuecomment-555",
            "issue_url": "https://api.github.com/repos/acme/app/issues/7",
        }
        self.patches: list[dict[str, object]] = []

    def __call__(self, cmd, **kwargs):
        assert cmd[:3] == ["gh", "api", COMMENT_API], cmd
        if "--method" in cmd:
            assert cmd[cmd.index("--method") + 1] == "PATCH"
            payload = json.loads(kwargs["input"])
            self.patches.append(payload)
            self.comment = {**self.comment, "body": payload["body"]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(self.comment), "")


@pytest.fixture
def gh(mocker):
    fake = FakeGh()
    mocker.patch("jailbee.issue_github.subprocess.run", side_effect=fake)
    return fake


def _publish(preflight, batch):  # noqa: F811
    from jailbee import issue_outbox

    issue_outbox.revalidate_batch(batch)
    return issue_outbox.apply_batch(
        batch, incus=preflight["incus"], container_prefix="app", publisher="user"
    )


@pytest.mark.parametrize("pull", [False, True], ids=["issue-comment", "pr-conversation-comment"])
def test_stage_plan_publish_edits_own_comment(preflight, mocker, pull):  # noqa: F811
    from jailbee import issue_outbox

    fake = FakeGh(pull=pull)
    mocker.patch("jailbee.issue_github.subprocess.run", side_effect=fake)
    if pull:
        preflight["fetch"].return_value = replace(preflight["snapshot"], is_pull_request=True)

    batch = preflight["prepare"]({"a.json": [_action()]})
    plan = "\n".join(issue_outbox.plan_lines(batch))
    assert "comment_edit #7 [pending]" in plan
    assert "comment 555" in plan
    assert "-Old wording" in plan and "+Fixed wording" in plan
    assert " second line" in plan

    report = _publish(preflight, batch)

    assert report.failure is None
    assert fake.patches == [{"body": "Fixed wording\nsecond line"}]
    ((_, receipt),) = report.applied
    assert receipt.url == fake.comment["html_url"]
    (row,) = _sent_rows()
    assert row.kind == "comment_edit"
    assert row.target == 7
    assert row.content == "Fixed wording\nsecond line"


def test_comment_by_another_author_is_refused_before_any_edit(preflight, mocker):  # noqa: F811
    from jailbee import issue_outbox

    fake = FakeGh(author="mallory")
    mocker.patch("jailbee.issue_github.subprocess.run", side_effect=fake)

    with pytest.raises(issue_outbox.IssueGateError, match="authored by mallory, not alice"):
        preflight["prepare"]({"a.json": [_action()]})
    assert fake.patches == []


def test_changed_comment_is_refused_at_preflight(preflight, mocker):  # noqa: F811
    from jailbee import issue_outbox

    fake = FakeGh(body="Someone already fixed it")
    mocker.patch("jailbee.issue_github.subprocess.run", side_effect=fake)

    with pytest.raises(issue_outbox.IssueGateError, match="expected.body differs"):
        preflight["prepare"]({"a.json": [_action()]})
    assert fake.patches == []


def test_comment_changed_after_the_plan_is_stale(preflight, gh):  # noqa: F811
    from jailbee import issue_outbox

    batch = preflight["prepare"]({"a.json": [_action()]})
    gh.comment = {**gh.comment, "body": "Edited meanwhile"}

    with pytest.raises(issue_outbox.IssueStaleError, match="expected.body differs"):
        issue_outbox.revalidate_batch(batch)
    assert gh.patches == []


def test_comment_on_a_different_issue_is_refused(preflight, gh):  # noqa: F811
    from jailbee import issue_outbox

    with pytest.raises(issue_outbox.IssueGateError, match="belongs to #7, not #8"):
        preflight["prepare"]({"a.json": [_action(issue=8)]})


def test_two_edits_of_one_comment_across_manifests_conflict(preflight, gh):  # noqa: F811
    from jailbee import issue_outbox

    with pytest.raises(issue_outbox.IssueGateError, match="conflict on comment:555"):
        preflight["prepare"]({"a.json": [_action()], "b.json": [_action(body="Other")]})


def test_show_lines_render_the_staged_diff_without_github(gh):
    from jailbee import issue_outbox
    from jailbee.issue_manifest import parse_manifest

    manifest = parse_manifest(
        "a.json", json.dumps({"version": 1, "actions": [_action()]}), {}
    )
    lines = "\n".join(issue_outbox.show_lines(manifest, None, "box"))
    assert "comment 555" in lines and "+Fixed wording" in lines


def test_plan_json_names_the_comment(preflight, gh):  # noqa: F811
    from jailbee.outbox.publish import issue_plan_json

    batch = preflight["prepare"]({"a.json": [_action()]})
    (action,) = issue_plan_json(batch, proposal="issue/a.json", revision="r")["actions"]
    assert action["kind"] == "comment_edit"
    assert action["comment_id"] == 555
    assert action["target"] == "acme/app#7"


def test_uncertain_pr_comment_edit_resolves_with_its_pull_permalink(execution):  # noqa: F811
    from jailbee import issue_outbox

    batch = execution["batch"]({"a.json": [_action()]})
    _seed_execution(execution, batch, state="uncertain", issue=None)
    assert _reconcile(
        execution,
        batch,
        issue_outbox.AppliedResolution(
            "https://github.com/acme/app/pull/7#issuecomment-555", issue=None
        ),
    )
    with pytest.raises(Exception, match="does not match this action's issue"):
        batch2 = execution["batch"]({"b.json": [_action()]})
        _seed_execution(execution, batch2, state="uncertain", issue=None)
        _reconcile(
            execution,
            batch2,
            issue_outbox.AppliedResolution(
                "https://github.com/acme/app/pull/8#issuecomment-555", issue=None
            ),
        )
