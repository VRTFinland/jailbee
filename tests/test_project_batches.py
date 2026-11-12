"""Mixed project batches through host preparation and publication."""

# ruff: noqa: F811 (imported pytest fixture)

import json

import pytest

from jailbee import issue_github, issue_outbox, issue_projects
from jailbee.outbox_io import ContainerIdentity
from tests.issue_store_support import StoreJournals, instance, seed
from tests.test_outbox_draft_ops import append, create, env, on_ref, stored_actions  # noqa: F401


@pytest.fixture
def pipeline(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.list_containers.return_value = [instance()]
    mocker.patch.object(
        issue_outbox,
        "resolve_repo_targets",
        return_value={".": issue_outbox.RepoTarget(".", tmp_path, "acme/app")},
    )
    mocker.patch.object(issue_github, "current_login", return_value="alice")
    mocker.patch.object(
        issue_github,
        "get_issue",
        return_value=issue_github.IssueSnapshot(
            901,
            "New",
            "Body",
            ("Bug", "Feature"),
            "closed",
            "https://github.com/acme/app/issues/901",
            False,
        ),
    )
    mocker.patch.object(
        issue_github, "list_labels", return_value={"bug": "Bug", "feature": "Feature"}
    )
    board = issue_projects.Project("acme/3", "P3", "Roadmap")
    mocker.patch.object(issue_projects, "resolve_project", return_value=board)
    target = mocker.patch.object(
        issue_projects,
        "get_target",
        return_value=issue_projects.ProjectTarget(
            "I901", "https://github.com/acme/app/issues/901", None
        ),
    )
    calls = []
    receipt = issue_github.MutationReceipt(901, "https://github.com/acme/app/issues/901")
    for method, name in (
        ("create_issue", "create"),
        ("replace_labels", "labels"),
        ("add_comment", "comment"),
        ("set_state", "state"),
    ):

        def mutation(*args, _name=name, **kwargs):
            calls.append(_name)
            if _name != "create":
                assert args[2] == 901
            return receipt

        mocker.patch.object(issue_github, method, side_effect=mutation)

    def add(*args):
        calls.append("project")
        assert args[3] == 901
        return issue_projects.ProjectReceipt(901, receipt.url + "#project-item-ITEM", "ITEM")

    project = mocker.patch.object(issue_projects, "add_item", side_effect=add)
    store = StoreJournals(ContainerIdentity("test-box", "2026-09-18"))
    actions = [
        {
            "type": "create",
            "repo": ".",
            "ref": "new",
            "title": "New",
            "body": "Body",
            "labels": ["bug"],
        },
        {
            "type": "labels",
            "repo": ".",
            "issue_ref": "new",
            "add": ["feature"],
            "remove": [],
            "expected": {"labels": ["bug"]},
        },
        {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Next"},
        {
            "type": "state",
            "repo": ".",
            "issue_ref": "new",
            "state": "closed",
            "reason": "completed",
            "expected": {"state": "open"},
        },
        {
            "type": "project_add",
            "repo": ".",
            "issue_ref": "new",
            "project": "acme/3",
            "expected": {"present": False},
        },
        {"type": "comment", "repo": ".", "issue_ref": "new", "body": "After project"},
    ]
    seed({"batch.json": json.dumps({"version": 1, "actions": actions})})

    def prepare():
        return issue_outbox.prepare_batch(cfg, incus, "test-box", ["batch.json"])

    def apply(batch):
        return issue_outbox.apply_batch(
            batch, incus=incus, container_prefix="app", publisher="user", publications=store.pubs
        )

    return prepare, apply, calls, project, target


def test_draft_overwrites_ref_membership_without_project_reader(env, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("a new issue must not require a project read")

    monkeypatch.setattr(issue_projects, "resolve_project", unexpected)
    monkeypatch.setattr(issue_projects, "get_target", unexpected)
    assert append(env, create()).ok
    assert append(env, on_ref("project_add", project="acme/3", expected={"present": True})).ok
    assert stored_actions(env)[1]["expected"] == {"present": False}


def test_draft_overwrites_ref_field_expectation_without_project_reader(env, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("a new issue must not require a project read")

    monkeypatch.setattr(issue_projects, "resolve_project", unexpected)
    monkeypatch.setattr(issue_projects, "get_target", unexpected)
    assert append(env, create()).ok
    assert append(
        env,
        on_ref(
            "project_set",
            project="acme/3",
            field="Status",
            value="Todo",
            expected={"present": True, "value": "Done"},
        ),
    ).ok
    assert stored_actions(env)[1]["expected"] == {"present": False, "value": None}


def test_mixed_ref_batch_prepares_and_publishes_in_order(pipeline):
    prepare, apply, calls, _, target = pipeline
    batch = prepare()
    target.assert_not_called()
    assert [a.action.kind for a in batch.manifests[0].actions] == [
        "create",
        "labels",
        "comment",
        "state",
        "project_add",
        "comment",
    ]
    report = apply(batch)
    assert report.failure is None
    assert calls == ["create", "labels", "comment", "state", "project", "comment"]
    assert [receipt.index for _, receipt in report.applied] == list(range(6))


def test_project_failure_stops_batch_and_retry_preserves_prior_actions(pipeline):
    prepare, apply, calls, project, _ = pipeline
    project.side_effect = issue_github.IssueGithubMutationError("rejected", uncertain=False)
    first = apply(prepare())
    assert first.failure is not None and not first.failure.uncertain
    assert calls == ["create", "labels", "comment", "state"]
    assert [receipt.index for _, receipt in first.applied] == [0, 1, 2, 3]
    project.side_effect = None
    project.return_value = issue_projects.ProjectReceipt(
        901, "https://github.com/acme/app/issues/901#project-item-ITEM", "ITEM"
    )
    second = apply(prepare())
    assert second.failure is None
    assert calls == ["create", "labels", "comment", "state", "comment"]
    assert project.call_count == 2


def test_multiple_fields_on_new_issue_resume_after_failure(pipeline, mocker):
    prepare, apply, calls, _, target = pipeline
    fields = (
        issue_projects.ProjectField("F1", "Status", "SINGLE_SELECT", (("Todo", "O1"),)),
        issue_projects.ProjectField("F2", "Notes", "TEXT"),
    )
    mocker.patch.object(issue_projects, "list_fields", return_value=fields)
    set_calls = []
    field_values = {}
    present = False
    url = "https://github.com/acme/app/issues/901"

    def get_target(*args):
        return issue_projects.ProjectTarget("I901", url, "ITEM" if present else None)

    def set_field(root, project, item, number, resolved):
        nonlocal present
        set_calls.append((resolved.field.name, resolved.value, item.present))
        if len(set_calls) == 2:
            raise issue_github.IssueGithubMutationError("rejected", uncertain=False)
        present = True
        field_values[resolved.field.name] = resolved.value
        return issue_projects.ProjectReceipt(number, url + "#project-item-ITEM", "ITEM")

    mocker.patch.object(
        issue_projects,
        "get_field_value",
        side_effect=lambda root, item, field: field_values.get(field.name),
    )
    mocker.patch.object(issue_projects, "set_field", side_effect=set_field)
    actions = [
        {"type": "create", "repo": ".", "ref": "new", "title": "New", "body": "Body", "labels": []},
        {
            "type": "project_set",
            "repo": ".",
            "issue_ref": "new",
            "project": "acme/3",
            "field": "Status",
            "value": "Todo",
            "expected": {"present": False, "value": None},
        },
        {
            "type": "project_set",
            "repo": ".",
            "issue_ref": "new",
            "project": "acme/3",
            "field": "Notes",
            "value": "Hello",
            "expected": {"present": False, "value": None},
        },
    ]
    seed({"batch.json": json.dumps({"version": 1, "actions": actions})})
    batch = prepare()
    target.assert_not_called()
    target.side_effect = get_target
    first = apply(batch)
    assert first.failure is not None and not first.failure.uncertain
    assert [receipt.index for _, receipt in first.applied] == [0, 1]
    assert set_calls == [("Status", "Todo", False), ("Notes", "Hello", True)]
    resumed = apply(prepare())
    assert resumed.failure is None
    assert calls == ["create"]
    assert set_calls == [
        ("Status", "Todo", False),
        ("Notes", "Hello", True),
        ("Notes", "Hello", True),
    ]


@pytest.mark.parametrize("resolution", ["retry", "applied"])
def test_uncertain_project_blocks_retry_until_resolved(pipeline, resolution):
    prepare, apply, calls, project, _ = pipeline
    project.side_effect = issue_github.IssueGithubMutationError("timeout", uncertain=True)
    batch = prepare()
    first = apply(batch)
    assert first.failure is not None and first.failure.uncertain
    blocked = apply(prepare())
    assert blocked.failure is not None and blocked.failure.uncertain
    assert project.call_count == 1
    manifest = batch.manifests[0]
    decision = (
        issue_outbox.RetryResolution()
        if resolution == "retry"
        else issue_outbox.AppliedResolution(
            "https://github.com/acme/app/issues/901#project-item-ITEM", None
        )
    )
    issue_outbox.reconcile_action(
        uuid=batch.uuid,
        index=4,
        repo=manifest.actions[4].repo,
        manifest=manifest.manifest,
        digest=manifest.digest,
        resolution=decision,
    )
    project.side_effect = None
    project.return_value = issue_projects.ProjectReceipt(
        901, "https://github.com/acme/app/issues/901#project-item-ITEM", "ITEM"
    )
    assert apply(prepare()).failure is None
    assert project.call_count == (2 if resolution == "retry" else 1)
    assert calls == ["create", "labels", "comment", "state", "comment"]
