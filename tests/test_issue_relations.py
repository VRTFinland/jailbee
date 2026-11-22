"""Native sub-issue and blocked-by relations through the issue outbox.

Seams: the manifest parser (`parse_manifest`), the container staging verbs
(`jailbee issue sub-issue|blocked-by|create --parent/--blocked-by`, through an
in-process broker that runs the host's draft validation), and the host publish
path (`prepare_batch` → `plan_lines` → `revalidate_batch` → `apply_batch`),
with GitHub reached only through a fake ``gh api`` process.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from jailbee import issue_github
from jailbee.issue_manifest import IssueManifestError, parse_manifest
from tests import container_broker_support
from tests.test_issue_outbox import (  # noqa: F401 - pytest fixtures
    _reconcile,
    _seed_execution,
    _sent_rows,
    execution,
    preflight,
)

_REAL = {name: getattr(issue_github, name) for name in ("get_issue", "create_issue", "add_comment")}


def _manifest(*actions):
    return parse_manifest("a.json", json.dumps({"version": 1, "actions": list(actions)}), {})


def _sub(**changes):
    return {"type": "sub_issue", "repo": ".", "issue": 10, "sub_issue": 12, "op": "add", **changes}


def _dep(**changes):
    return {
        "type": "dependency",
        "repo": ".",
        "issue": 12,
        "blocked_by": 10,
        "op": "add",
        **changes,
    }


def _create(ref="bug"):
    return {"type": "create", "repo": ".", "ref": ref, "title": "Bug", "body": "b", "labels": []}


# --------------------------------------------------------------------------
# Manifest parsing
# --------------------------------------------------------------------------


def test_parse_relations_between_existing_issues():
    from jailbee.issue_manifest import (
        DependencyAction,
        ExistingIssue,
        SubIssueAction,
        mutation_keys,
    )

    sub, dep = _manifest(_sub(), _dep(op="remove")).actions
    assert isinstance(sub, SubIssueAction)
    assert (sub.target, sub.child, sub.remove) == (ExistingIssue(10), ExistingIssue(12), False)
    assert isinstance(dep, DependencyAction)
    assert (dep.target, dep.blocker, dep.remove) == (ExistingIssue(12), ExistingIssue(10), True)
    assert mutation_keys(sub) == {"sub_issue:12"}
    assert mutation_keys(dep) == {"blocked_by:10"}


def test_parse_relations_to_issues_created_in_the_same_manifest():
    from jailbee.issue_manifest import CreatedIssue, ExistingIssue, mutation_keys

    _, sub, dep = _manifest(
        _create(),
        {"type": "sub_issue", "repo": ".", "issue": 10, "sub_issue_ref": "bug", "op": "add"},
        {"type": "dependency", "repo": ".", "issue_ref": "bug", "blocked_by": 10, "op": "add"},
    ).actions
    assert (sub.target, sub.child) == (ExistingIssue(10), CreatedIssue("bug"))
    assert (dep.target, dep.blocker) == (CreatedIssue("bug"), ExistingIssue(10))
    # A relation to a new issue cannot collide with another manifest's.
    assert mutation_keys(sub) == set()
    # A new issue's own target key is manifest-local already.
    assert mutation_keys(dep) == {"blocked_by:10"}


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (_sub(op="link"), "op must be 'add' or 'remove'"),
        ({k: v for k, v in _sub().items() if k != "op"}, "op must be 'add' or 'remove'"),
        ({k: v for k, v in _sub().items() if k != "sub_issue"}, "exactly one of sub_issue"),
        (_sub(sub_issue_ref="bug"), "exactly one of sub_issue"),
        (_sub(sub_issue=0), "sub_issue must be a positive integer"),
        (_sub(sub_issue=True), "sub_issue must be a positive integer"),
        (_sub(sub_issue=10), "cannot be related to itself"),
        (_dep(blocked_by=12), "cannot be related to itself"),
        ({**_dep(), "blocked_by_ref": "x"}, "exactly one of blocked_by"),
        (_sub(extra=1), "unknown field 'extra'"),
        ({**_dep(), "blocked_by": None}, "exactly one of blocked_by"),
    ],
)
def test_parse_refuses_malformed_relations(action, message):
    action = {k: v for k, v in action.items() if v is not None}
    with pytest.raises(IssueManifestError, match=message):
        _manifest(action)


def test_parse_refuses_unknown_and_cross_repo_related_refs():
    with pytest.raises(IssueManifestError, match="does not name an earlier create"):
        _manifest(
            {"type": "dependency", "repo": ".", "issue": 12, "blocked_by_ref": "x", "op": "add"}
        )
    with pytest.raises(IssueManifestError, match="same repo"):
        _manifest(
            {**_create(), "repo": "lib"},
            {"type": "sub_issue", "repo": ".", "issue": 10, "sub_issue_ref": "bug", "op": "add"},
        )


def test_parse_refuses_removing_a_relation_of_a_new_issue():
    with pytest.raises(IssueManifestError, match="remove needs existing issue numbers"):
        _manifest(
            _create(),
            {"type": "sub_issue", "repo": ".", "issue": 10, "sub_issue_ref": "bug", "op": "remove"},
        )


def test_parse_refuses_a_ref_relation_to_itself():
    with pytest.raises(IssueManifestError, match="cannot be related to itself"):
        _manifest(
            _create(),
            {
                "type": "sub_issue",
                "repo": ".",
                "issue_ref": "bug",
                "sub_issue_ref": "bug",
                "op": "add",
            },
        )


def test_two_changes_of_one_relation_in_a_manifest_conflict():
    with pytest.raises(IssueManifestError, match="already changes field 'sub_issue:12'"):
        _manifest(_sub(), _sub(op="remove"))


# --------------------------------------------------------------------------
# Host publish path with a fake GitHub
# --------------------------------------------------------------------------


def _issue_json(number, repo="acme/app", pull=False, parent=None):
    data = {
        "id": 1000 + number,
        "number": number,
        "title": f"Issue {number}",
        "body": "",
        "labels": [],
        "assignees": [],
        "state": "open",
        "html_url": f"https://github.com/{repo}/{'pull' if pull else 'issues'}/{number}",
    }
    if pull:
        data["pull_request"] = {}
    data["parent_issue_url"] = (
        f"https://api.github.com/repos/{repo}/issues/{parent}" if parent else None
    )
    return data


class FakeGitHub:
    """Issues, sub-issues and blocked-by links answering ``gh api`` like GitHub."""

    def __init__(self, *, pulls=()):
        self.pulls = set(pulls)
        self.sub_issues: dict[int, set[int]] = {}
        self.blocked_by: dict[int, set[int]] = {}
        self.next_number = 901
        self.created: list[int] = []
        self.mutations: list[tuple[str, str, object]] = []
        self.fail_mutation: str | None = None
        self.parents: dict[int, int] = {}
        self.calls: list[tuple[list[str], object]] = []

    def _number(self, issue_id):
        return issue_id - 1000

    def _page(self, numbers):
        return json.dumps([[_issue_json(n) for n in sorted(numbers)]])

    def __call__(self, cmd, **kwargs):
        assert cmd[:2] == ["gh", "api"], cmd
        endpoint = cmd[2]
        method = cmd[cmd.index("--method") + 1] if "--method" in cmd else "GET"
        self.calls.append((cmd, kwargs.get("input")))
        if "--input" in cmd:
            assert kwargs.get("input") is not None, "--input - would wait on stdin"
        elif method != "GET":
            assert kwargs.get("input") is None
        payload = json.loads(kwargs["input"]) if kwargs.get("input") else None
        prefix = "repos/acme/app/"
        assert endpoint.startswith(prefix), endpoint
        parts = endpoint.removeprefix(prefix).split("/")
        if method != "GET":
            self.mutations.append((method, endpoint, payload))
            if self.fail_mutation == method:
                return subprocess.CompletedProcess(cmd, 1, "", "gh: HTTP 422")
        out: object
        if parts == ["issues"] and method == "POST":
            number = self.next_number
            self.next_number += 1
            self.created.append(number)
            out = _issue_json(number)
        elif len(parts) == 2 and method == "GET":
            number = int(parts[1])
            out = _issue_json(number, pull=number in self.pulls, parent=self.parents.get(number))
        elif parts[2:] == ["sub_issues"] and method == "GET":
            return subprocess.CompletedProcess(
                cmd, 0, self._page(self.sub_issues.get(int(parts[1]), set())), ""
            )
        elif parts[2:] == ["sub_issues"] and method == "POST":
            assert set(payload) == {"sub_issue_id"}
            self.sub_issues.setdefault(int(parts[1]), set()).add(
                self._number(payload["sub_issue_id"])
            )
            out = _issue_json(int(parts[1]))
        elif parts[2:] == ["sub_issue"] and method == "DELETE":
            assert set(payload) == {"sub_issue_id"}
            self.sub_issues[int(parts[1])].discard(self._number(payload["sub_issue_id"]))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        elif parts[2:] == ["dependencies", "blocked_by"] and method == "GET":
            return subprocess.CompletedProcess(
                cmd, 0, self._page(self.blocked_by.get(int(parts[1]), set())), ""
            )
        elif parts[2:] == ["dependencies", "blocked_by"] and method == "POST":
            assert set(payload) == {"issue_id"}
            self.blocked_by.setdefault(int(parts[1]), set()).add(self._number(payload["issue_id"]))
            out = _issue_json(int(parts[1]))
        elif parts[2:4] == ["dependencies", "blocked_by"] and method == "DELETE":
            assert payload is None
            self.blocked_by[int(parts[1])].discard(self._number(int(parts[4])))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        else:
            raise AssertionError(f"unexpected gh api call: {method} {endpoint}")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")


@pytest.fixture
def github(preflight, mocker):  # noqa: F811
    fake = FakeGitHub()
    for name, real in _REAL.items():
        mocker.patch.object(issue_github, name, real)
    mocker.patch("jailbee.issue_github.subprocess.run", side_effect=fake)
    return fake


def _publish(preflight, batch):  # noqa: F811
    from jailbee import issue_outbox

    issue_outbox.revalidate_batch(batch)
    return issue_outbox.apply_batch(
        batch, incus=preflight["incus"], container_prefix="app", publisher="user"
    )


def test_add_sub_issue_and_dependency_between_existing_issues(preflight, github):  # noqa: F811
    from jailbee import issue_outbox

    batch = preflight["prepare"]({"a.json": [_sub(), _dep()]})
    plan = "\n".join(issue_outbox.plan_lines(batch))
    assert "sub_issue #10 [pending]" in plan
    assert "add sub-issue #12" in plan
    assert "dependency #12 [pending]" in plan
    assert "add blocked by #10" in plan

    report = _publish(preflight, batch)

    assert report.failure is None
    assert github.sub_issues == {10: {12}}
    assert github.blocked_by == {12: {10}}
    assert [m[:2] for m in github.mutations] == [
        ("POST", "repos/acme/app/issues/10/sub_issues"),
        ("POST", "repos/acme/app/issues/12/dependencies/blocked_by"),
    ]
    assert github.mutations[0][2] == {"sub_issue_id": 1012}
    assert github.mutations[1][2] == {"issue_id": 1010}
    assert [receipt.url for _, receipt in report.applied] == [
        "https://github.com/acme/app/issues/10",
        "https://github.com/acme/app/issues/12",
    ]
    rows = _sent_rows()
    assert [(row.kind, row.target, row.content) for row in rows] == [
        ("sub_issue", 10, "add sub-issue #12"),
        ("dependency", 12, "add blocked by #10"),
    ]


def test_remove_sub_issue_and_dependency(preflight, github):  # noqa: F811
    github.sub_issues = {10: {12}}
    github.blocked_by = {12: {10}}

    batch = preflight["prepare"]({"a.json": [_sub(op="remove"), _dep(op="remove")]})
    from jailbee import issue_outbox

    plan = "\n".join(issue_outbox.plan_lines(batch))
    assert "remove sub-issue #12" in plan
    assert "remove blocked by #10" in plan

    report = _publish(preflight, batch)

    assert report.failure is None
    assert github.sub_issues == {10: set()}
    assert github.blocked_by == {12: set()}
    assert github.mutations == [
        ("DELETE", "repos/acme/app/issues/10/sub_issue", {"sub_issue_id": 1012}),
        ("DELETE", "repos/acme/app/issues/12/dependencies/blocked_by/1010", None),
    ]


def test_removals_send_the_payload_only_where_the_api_takes_one(preflight, github):  # noqa: F811
    github.sub_issues = {10: {12}}
    github.blocked_by = {12: {10}}
    batch = preflight["prepare"]({"a.json": [_sub(op="remove"), _dep(op="remove")]})

    assert _publish(preflight, batch).failure is None

    deletes = {cmd[2]: (cmd, stdin) for cmd, stdin in github.calls if "DELETE" in cmd}
    sub_cmd, sub_stdin = deletes["repos/acme/app/issues/10/sub_issue"]
    assert sub_cmd[-2:] == ["--input", "-"]
    assert json.loads(sub_stdin) == {"sub_issue_id": 1012}
    dep_cmd, dep_stdin = deletes["repos/acme/app/issues/12/dependencies/blocked_by/1010"]
    assert "--input" not in dep_cmd
    assert dep_stdin is None


def test_relations_to_a_create_resolve_after_it(preflight, github):  # noqa: F811
    from jailbee import issue_outbox

    batch = preflight["prepare"](
        {
            "a.json": [
                _create(),
                {
                    "type": "sub_issue",
                    "repo": ".",
                    "issue": 10,
                    "sub_issue_ref": "bug",
                    "op": "add",
                },
                {
                    "type": "dependency",
                    "repo": ".",
                    "issue_ref": "bug",
                    "blocked_by": 11,
                    "op": "add",
                },
            ]
        }
    )
    plan = "\n".join(issue_outbox.plan_lines(batch))
    assert "add sub-issue ref bug" in plan
    assert "dependency ref bug [pending]" in plan

    report = _publish(preflight, batch)

    assert report.failure is None
    assert github.created == [901]
    assert github.sub_issues == {10: {901}}
    assert github.blocked_by == {901: {11}}


@pytest.mark.parametrize(
    ("state", "actions", "message"),
    [
        ({"sub_issues": {10: {12}}}, [_sub()], "#12 is already a sub-issue of #10"),
        ({}, [_sub(op="remove")], "#12 is not a sub-issue of #10"),
        ({"blocked_by": {12: {10}}}, [_dep()], "#12 is already blocked by #10"),
        ({}, [_dep(op="remove")], "#12 is not blocked by #10"),
        ({"pulls": {12}}, [_sub()], "#12 is a pull request, not an issue"),
        ({"pulls": {10}}, [_sub()], "target is a pull request"),
    ],
)
def test_preflight_refuses_a_relation_already_in_its_end_state(
    preflight,  # noqa: F811
    github,
    state,
    actions,
    message,
):
    from jailbee import issue_outbox

    github.sub_issues = state.get("sub_issues", {})
    github.blocked_by = state.get("blocked_by", {})
    github.pulls = state.get("pulls", set())
    with pytest.raises(issue_outbox.IssueGateError, match=message):
        preflight["prepare"]({"a.json": actions})
    assert github.mutations == []


def test_preflight_refuses_a_child_that_already_has_another_parent(preflight, github):  # noqa: F811
    from jailbee import issue_outbox

    github.parents = {12: 11}
    with pytest.raises(issue_outbox.IssueGateError, match="#12 is already a sub-issue of #11"):
        preflight["prepare"]({"a.json": [_sub()]})
    assert github.mutations == []


def test_child_with_another_parent_is_refused_on_revalidate(preflight, github):  # noqa: F811
    from jailbee import issue_outbox

    batch = preflight["prepare"]({"a.json": [_sub()]})
    github.parents = {12: 11}
    with pytest.raises(issue_outbox.IssueStaleError, match="#12 is already a sub-issue of #11"):
        issue_outbox.revalidate_batch(batch)


def test_one_child_under_two_parents_conflicts_across_manifests(preflight, github):  # noqa: F811
    from jailbee import issue_outbox

    with pytest.raises(issue_outbox.IssueGateError, match="conflict on parent"):
        preflight["prepare"]({"a.json": [_sub()], "b.json": [_sub(issue=11)]})


def test_one_child_under_two_parents_conflicts_in_a_manifest():
    with pytest.raises(IssueManifestError, match="already changes field 'parent'"):
        _manifest(_sub(), _sub(issue=11))


def test_direct_dependency_cycle_in_a_batch_is_refused(preflight, github):  # noqa: F811
    from jailbee import issue_outbox

    with pytest.raises(issue_outbox.IssueGateError, match="dependency cycle between #10 and #12"):
        preflight["prepare"](
            {"a.json": [_dep()], "b.json": [_dep(issue=10, blocked_by=12)]},
        )
    assert github.mutations == []


def test_removing_one_direction_is_not_a_cycle(preflight, github):  # noqa: F811
    github.blocked_by = {12: {10}}
    preflight["prepare"]({"a.json": [_dep(op="remove"), _dep(issue=10, blocked_by=12)]})


def test_uncertain_relation_points_at_resolve_applied_before_retry():
    from jailbee import issue_outbox
    from jailbee.outbox_io import IssueJournal, JournalAction

    manifest = _manifest(_sub(), _dep())
    journal = IssueJournal(
        issue_outbox.ContainerIdentity("c", ""),
        "a.json",
        "d",
        2,
        (JournalAction(0, "uncertain", "acme/app"), JournalAction(1, "uncertain", "acme/app")),
    )
    lines = issue_outbox.show_lines(manifest, journal, "box")
    hints = [line for line in lines if "already linked" in line]
    assert len(hints) == 2
    assert all("--applied" in line for line in hints)


def test_relation_changed_after_the_plan_is_stale(preflight, github):  # noqa: F811
    from jailbee import issue_outbox

    batch = preflight["prepare"]({"a.json": [_sub()]})
    github.sub_issues = {10: {12}}

    with pytest.raises(issue_outbox.IssueStaleError, match="already a sub-issue"):
        issue_outbox.revalidate_batch(batch)
    assert github.mutations == []


def test_one_relation_in_two_manifests_conflicts(preflight, github):  # noqa: F811
    from jailbee import issue_outbox

    with pytest.raises(issue_outbox.IssueGateError, match="conflict on blocked_by:10"):
        preflight["prepare"]({"a.json": [_dep()], "b.json": [_dep()]})


def test_rejected_relation_is_definite_and_not_journaled_uncertain(preflight, github):  # noqa: F811
    github.fail_mutation = "POST"
    batch = preflight["prepare"]({"a.json": [_sub()]})

    report = _publish(preflight, batch)

    assert report.failure is not None
    assert report.failure.uncertain is False
    assert report.failure.detail == "GitHub mutation was rejected"


def test_plan_json_names_the_related_issue(preflight, github):  # noqa: F811
    from jailbee.outbox.publish import issue_plan_json

    github.blocked_by = {12: {10}}
    batch = preflight["prepare"]({"a.json": [_sub(), _dep(op="remove")]})
    sub, dep = issue_plan_json(batch, proposal="issue/a.json", revision="r")["actions"]
    assert (sub["kind"], sub["target"], sub["related"], sub["operation"]) == (
        "sub_issue",
        "acme/app#10",
        "#12",
        "add",
    )
    assert (dep["kind"], dep["related"], dep["operation"]) == ("dependency", "#10", "remove")


def test_show_lines_and_inspect_render_without_github():
    from jailbee import issue_outbox
    from jailbee.outbox.inspect import _issue_action

    manifest = _manifest(_sub(), _dep(op="remove"))
    lines = "\n".join(issue_outbox.show_lines(manifest, None, "box"))
    assert "add sub-issue #12" in lines and "remove blocked by #10" in lines
    views = [_issue_action(i, a) for i, a in enumerate(manifest.actions)]
    assert [(v.kind, v.target, v.body) for v in views] == [
        ("sub_issue", "10", "add sub-issue #12"),
        ("dependency", "12", "remove blocked by #10"),
    ]


def test_uncertain_relation_resolves_with_the_target_issue_url(execution):  # noqa: F811
    from jailbee import issue_outbox

    batch = execution["batch"]({"a.json": [_sub()]})
    _seed_execution(execution, batch, state="uncertain", issue=None)
    assert _reconcile(
        execution,
        batch,
        issue_outbox.AppliedResolution("https://github.com/acme/app/issues/10", issue=None),
    )


# --------------------------------------------------------------------------
# Orchestrator grants
# --------------------------------------------------------------------------


def test_relation_kinds_are_grantable_and_not_implied():
    from jailbee.orchestration.grant import ISSUE_ACTION_KINDS, kind_granted

    assert {"sub_issue", "dependency"} <= set(ISSUE_ACTION_KINDS)
    assert not kind_granted("sub_issue", ("create", "edit", "dependency"))
    assert not kind_granted("dependency", ("create", "edit", "sub_issue"))


# --------------------------------------------------------------------------
# Container staging, end to end through the host publish path
# --------------------------------------------------------------------------


@pytest.fixture
def broker(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)
    return container_broker_support.install(monkeypatch, tmp_path / "broker")


def _stage(*args):
    from jailbee.container_cli import container_app

    result = CliRunner().invoke(container_app, ["issue", *args])
    return result


def _submit(name):
    from jailbee.container_cli import container_app

    result = CliRunner().invoke(container_app, ["outbox", "submit", name])
    assert result.exit_code == 0, result.output


def test_sub_issue_and_blocked_by_verbs_stage_parseable_actions(broker):
    result = _stage("sub-issue", "10", "12")
    assert result.exit_code == 0, result.output
    result = _stage("blocked-by", "12", "10", "--remove", "--repo", "lib")
    assert result.exit_code == 0, result.output
    first, second = broker.proposals("issue")
    assert json.loads(first.manifest)["actions"] == [_sub()]
    assert json.loads(second.manifest)["actions"] == [_dep(op="remove", repo="lib")]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("sub-issue", "10"), "missing sub-issue"),
        (("sub-issue", "10", "10"), "cannot be related to itself"),
        (("blocked-by", "12", "x"), "must be a positive number or @REF"),
        (("blocked-by", "12", "@bug", "--remove"), "--remove needs existing issue numbers"),
    ],
)
def test_relation_verbs_refuse_bad_input_without_staging(broker, args, message):
    result = _stage(*args)
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert broker.proposals("issue") == []


def test_create_with_parent_and_blocked_by_stages_follow_up_actions(broker):
    result = _stage(
        "create",
        "--title",
        "Bug",
        "--body",
        "b",
        "--parent",
        "10",
        "--blocked-by",
        "11",
        "--blocked-by",
        "13",
    )
    assert result.exit_code == 0, result.output
    (proposal,) = broker.proposals("issue")
    actions = json.loads(proposal.manifest)["actions"]
    assert actions[1:] == [
        {"type": "sub_issue", "repo": ".", "issue": 10, "sub_issue_ref": "issue", "op": "add"},
        {"type": "dependency", "repo": ".", "issue_ref": "issue", "blocked_by": 11, "op": "add"},
        {"type": "dependency", "repo": ".", "issue_ref": "issue", "blocked_by": 13, "op": "add"},
    ]


def test_create_parent_ref_in_a_batch_publishes_end_to_end(broker, preflight, github):  # noqa: F811
    result = _stage("create", "--title", "Epic", "--body", "e", "--ref", "epic", "--batch", "b")
    assert result.exit_code == 0, result.output
    result = _stage(
        "create",
        "--title",
        "Task",
        "--body",
        "t",
        "--ref",
        "task",
        "--parent",
        "@epic",
        "--blocked-by",
        "@epic",
        "--batch",
        "b",
    )
    assert result.exit_code == 0, result.output
    result = _stage("blocked-by", "10", "@task", "--batch", "b")
    assert result.exit_code == 0, result.output
    _submit("b")
    (proposal,) = broker.proposals("issue")
    actions = json.loads(proposal.manifest)["actions"]

    batch = preflight["prepare"]({"a.json": actions})
    report = _publish(preflight, batch)

    assert report.failure is None, report.failure
    assert github.created == [901, 902]
    assert github.sub_issues == {901: {902}}
    assert github.blocked_by == {902: {901}, 10: {902}}


def test_deleting_a_create_cascades_to_relations_naming_it(tmp_path):
    from jailbee.outbox.delete import DeleteSelection, plan_delete
    from jailbee.outbox.models import OutboxError, ProposalId
    from tests.test_outbox_delete import inspected

    actions = [
        _create(),
        {"type": "sub_issue", "repo": ".", "issue": 10, "sub_issue_ref": "bug", "op": "add"},
        {"type": "dependency", "repo": ".", "issue": 12, "blocked_by_ref": "bug", "op": "add"},
        _dep(),
    ]
    view = inspected(
        tmp_path, "issue", {"001.json": json.dumps({"version": 1, "actions": actions})}
    )
    with pytest.raises(OutboxError, match=r"\(1, 2\)"):
        plan_delete(view, ProposalId("issue", "001.json"), DeleteSelection(action=0))
    plan = plan_delete(
        view, ProposalId("issue", "001.json"), DeleteSelection(action=0, with_dependents=True)
    )
    assert plan.removed_actions == (0, 1, 2)
    assert json.loads(plan.new_text)["actions"] == [_dep()]


def test_one_relation_to_a_new_issue_twice_in_a_manifest_conflicts():
    with pytest.raises(IssueManifestError, match="already changes field 'sub_issue:@bug'"):
        _manifest(
            _create(),
            {"type": "sub_issue", "repo": ".", "issue": 10, "sub_issue_ref": "bug", "op": "add"},
            {"type": "sub_issue", "repo": ".", "issue": 10, "sub_issue_ref": "bug", "op": "add"},
        )


def test_staged_verbs_publish_add_and_remove_end_to_end(broker, preflight, github):  # noqa: F811
    github.sub_issues = {10: {12}}
    github.blocked_by = {12: {10}}
    for args in (
        ("sub-issue", "10", "12", "--remove"),
        ("blocked-by", "12", "10", "--remove"),
        ("sub-issue", "20", "12"),
        ("create", "--title", "Bug", "--body", "b", "--parent", "20", "--blocked-by", "12"),
    ):
        result = _stage(*args, "--batch", "b")
        assert result.exit_code == 0, result.output
    _submit("b")
    (proposal,) = broker.proposals("issue")

    batch = preflight["prepare"]({"a.json": json.loads(proposal.manifest)["actions"]})
    report = _publish(preflight, batch)

    assert report.failure is None, report.failure
    assert github.sub_issues == {10: set(), 20: {12, 901}}
    assert github.blocked_by == {12: set(), 901: {12}}
