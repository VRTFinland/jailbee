import json
import subprocess

import pytest

from jailbee import issue_projects
from jailbee.issue_github import IssueGithubMutationError, IssueGithubReadError


@pytest.mark.parametrize("operation,mutation", [("clear", "clearProjectV2ItemFieldValue"), ("remove", "deleteProjectV2Item")])
@pytest.mark.parametrize("present", [True, False])
def test_clear_remove_exact_mutations_and_absent_noop(tmp_path, mocker, operation, mutation, present):
    run = mocker.patch("jailbee.issue_projects.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"data": {mutation: {"projectV2Item": {"id": "ITEM"}} if operation == "clear" else {"deletedItemId": "ITEM"}}}), ""))
    board = issue_projects.Project("acme/3", "P3", "Roadmap")
    target = issue_projects.ProjectTarget("I7", "https://github.com/acme/app/pull/7", "ITEM" if present else None)
    if operation == "clear":
        receipt = issue_projects.clear_field(tmp_path, board, target, 7, issue_projects.ProjectField("F1", "Status", "SINGLE_SELECT"))
    else:
        receipt = issue_projects.remove_item(tmp_path, board, target, 7)
    assert receipt.url == target.url + ("#project-item-ITEM" if present else "")
    assert run.call_count == int(present)
    if present:
        assert run.call_args.args[0] == ["gh", "api", "graphql", "--input", "-"]
        payload = json.loads(run.call_args.kwargs["input"])
        assert mutation in payload["query"]
        assert payload["variables"] == {"project": "P3", "item": "ITEM", **({"field": "F1"} if operation == "clear" else {})}


def test_field_discovery_and_current_values_paginate(tmp_path, mocker):
    def response(connection, nodes, more=False, cursor="next"):
        return subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "data": {
                        "node": {
                            connection: {
                                "nodes": nodes,
                                "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                            }
                        }
                    }
                }
            ),
            "",
        )

    run = mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        side_effect=[
            response("fields", [{"id": "F1", "name": "Notes", "dataType": "TEXT"}], True),
            response("fields", [{"id": "F2", "name": "Sprint", "dataType": "ITERATION"}]),
            response("fieldValues", [{"field": {"id": "other"}, "text": "else"}], True),
            response("fieldValues", [{"field": {"id": "F1"}, "text": ""}]),
        ],
    )
    fields = issue_projects.list_fields(tmp_path, issue_projects.Project("acme/3", "P3", "Roadmap"))
    assert [f.data_type for f in fields] == ["TEXT", "ITERATION"]
    assert (
        issue_projects.get_field_value(
            tmp_path, issue_projects.ProjectTarget("I7", "url", "ITEM"), fields[0]
        )
        == ""
    )
    assert json.loads(run.call_args_list[1].kwargs["input"])["variables"] == {
        "id": "P3",
        "cursor": "next",
    }
    assert json.loads(run.call_args.kwargs["input"])["variables"] == {
        "id": "ITEM",
        "cursor": "next",
    }
    assert "ProjectV2IterationField" in json.loads(run.call_args_list[0].kwargs["input"])["query"]


def test_resolve_user_board_uses_explicit_owner_type(tmp_path, mocker):
    run = mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess(
                [], 0, json.dumps({"data": {"repositoryOwner": {"__typename": "User"}}}), ""
            ),
            subprocess.CompletedProcess(
                [],
                0,
                json.dumps({"data": {"user": {"projectV2": {"id": "P_3", "title": "Roadmap"}}}}),
                "",
            ),
        ],
    )
    board = issue_projects.resolve_project(tmp_path, "acme/3")
    assert (board.id, board.title) == ("P_3", "Roadmap")
    assert run.call_args.args[0] == ["gh", "api", "graphql", "--input", "-"]
    payload = json.loads(run.call_args.kwargs["input"])
    assert "user(login:" in payload["query"]
    assert payload["variables"] == {"owner": "acme", "number": 3}
    assert run.call_args.kwargs["cwd"] == tmp_path


@pytest.mark.parametrize(
    "kind,value,payload",
    [
        ("SINGLE_SELECT", "High", {"singleSelectOptionId": "OPT"}),
        ("TEXT", "", {"text": ""}),
        ("NUMBER", "1.25", {"number": 1.25}),
        ("DATE", "2026-11-12", {"date": "2026-11-12"}),
    ],
)
def test_resolve_field_and_update_exact_typed_payload(tmp_path, mocker, kind, value, payload):
    fields = (issue_projects.ProjectField("F1", "Priority", kind, (("High", "OPT"),)),)
    resolved = issue_projects.resolve_field(fields, "priority", value)
    run = mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [], 0, '{"data":{"updateProjectV2ItemFieldValue":{"projectV2Item":{"id":"ITEM"}}}}', ""
        ),
    )
    receipt = issue_projects.set_field(
        tmp_path,
        issue_projects.Project("acme/3", "P3", "Roadmap"),
        issue_projects.ProjectTarget("I7", "https://github.com/acme/app/issues/7", "ITEM"),
        7,
        resolved,
    )
    assert receipt.item_id == "ITEM"
    assert run.call_args.args[0] == ["gh", "api", "graphql", "--input", "-"]
    request = json.loads(run.call_args.kwargs["input"])
    assert request["variables"] == {
        "project": "P3",
        "item": "ITEM",
        "field": "F1",
        "value": payload,
    }
    assert "updateProjectV2ItemFieldValue" in request["query"]


@pytest.mark.parametrize(
    "kind,value,match",
    [
        ("NUMBER", True, "number"),
        ("NUMBER", "NaN", "number"),
        ("NUMBER", float("inf"), "number"),
        ("NUMBER", "bad", "number"),
        ("TEXT", 3, "text"),
        ("DATE", "20261112", "YYYY-MM-DD"),
        ("DATE", "2026-02-30", "YYYY-MM-DD"),
        ("ITERATION", "Sprint", "not supported yet"),
        ("SINGLE_SELECT", "Urgent", "High"),
    ],
)
def test_resolve_field_refuses_invalid_values(kind, value, match):
    fields = (issue_projects.ProjectField("F1", "Priority", kind, (("High", "OPT"),)),)
    with pytest.raises(ValueError, match=match):
        issue_projects.resolve_field(fields, "Priority", value)
    with pytest.raises(ValueError, match="Priority"):
        issue_projects.resolve_field(fields, "Unknown", value)


def test_set_failure_after_implicit_add_is_uncertain(tmp_path, mocker):
    run = mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess(
                [], 0, '{"data":{"addProjectV2ItemById":{"item":{"id":"ITEM"}}}}', ""
            ),
            subprocess.CompletedProcess([], 0, '{"errors":[{"message":"denied"}]}', ""),
        ],
    )
    field = issue_projects.resolve_field(
        (issue_projects.ProjectField("F1", "Notes", "TEXT"),), "Notes", "new"
    )
    with pytest.raises(IssueGithubMutationError) as error:
        issue_projects.set_field(
            tmp_path,
            issue_projects.Project("acme/3", "P3", "Roadmap"),
            issue_projects.ProjectTarget("I7", "https://github.com/acme/app/issues/7", None),
            7,
            field,
        )
    assert error.value.uncertain is True
    assert run.call_count == 2


def test_scope_error_does_not_try_another_owner_type(tmp_path, mocker):
    run = mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [], 1, '{"errors":[{"message":"requires project scope"}]}', ""
        ),
    )
    with pytest.raises(IssueGithubReadError, match="gh auth refresh -s project"):
        issue_projects.resolve_project(tmp_path, "acme/3")
    assert run.call_count == 1


@pytest.mark.parametrize(
    "response,uncertain",
    [
        (subprocess.CompletedProcess([], 0, '{"errors":[{"message":"denied"}]}', ""), False),
        (subprocess.CompletedProcess([], 1, "", "connection lost"), True),
        (subprocess.TimeoutExpired("gh", 60), True),
    ],
)
def test_add_classifies_graphql_and_transport_failures(tmp_path, mocker, response, uncertain):
    run = mocker.patch("jailbee.issue_projects.subprocess.run")
    if isinstance(response, Exception):
        run.side_effect = response
    else:
        run.return_value = response
    with pytest.raises(IssueGithubMutationError) as error:
        issue_projects.add_item(
            tmp_path,
            issue_projects.Project("acme/3", "P3", "Roadmap"),
            issue_projects.ProjectTarget("I7", "https://github.com/acme/app/pull/7", None),
            7,
        )
    assert error.value.uncertain is uncertain


def test_membership_reads_all_pages_and_organization_owner(tmp_path, mocker):
    def response(data):
        return subprocess.CompletedProcess([], 0, json.dumps({"data": data}), "")

    run = mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        side_effect=[
            response({"repositoryOwner": {"__typename": "Organization"}}),
            response({"organization": {"projectV2": {"id": "P3", "title": "Roadmap"}}}),
            response(
                {
                    "repository": {
                        "issueOrPullRequest": {
                            "id": "I7",
                            "url": "https://github.com/acme/app/issues/7",
                            "projectItems": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                            },
                        }
                    }
                }
            ),
            response(
                {
                    "repository": {
                        "issueOrPullRequest": {
                            "id": "I7",
                            "url": "https://github.com/acme/app/issues/7",
                            "projectItems": {
                                "nodes": [{"id": "ITEM7", "project": {"id": "P3"}}],
                                "pageInfo": {"hasNextPage": False, "endCursor": "last"},
                            },
                        }
                    }
                }
            ),
        ],
    )
    board = issue_projects.resolve_project(tmp_path, "acme/3")
    target = issue_projects.get_target(tmp_path, board, "acme/app", 7)
    assert target.item_id == "ITEM7"
    assert "organization(login:" in json.loads(run.call_args_list[1].kwargs["input"])["query"]
    assert json.loads(run.call_args.kwargs["input"])["variables"] == {
        "owner": "acme",
        "name": "app",
        "number": 7,
        "cursor": "next",
    }


@pytest.mark.parametrize(
    "connection", [{"nodes": []}, {"nodes": [], "pageInfo": {"hasNextPage": True}}]
)
def test_membership_malformed_page_fails_closed(tmp_path, mocker, connection):
    mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "issueOrPullRequest": {
                                "id": "I7",
                                "url": "https://github.com/acme/app/issues/7",
                                "projectItems": connection,
                            }
                        }
                    }
                }
            ),
            "",
        ),
    )
    with pytest.raises(IssueGithubReadError):
        issue_projects.get_target(
            tmp_path, issue_projects.Project("acme/3", "P3", "Roadmap"), "acme/app", 7
        )


def test_add_returns_target_and_item_receipt(tmp_path, mocker):
    run = mocker.patch(
        "jailbee.issue_projects.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [], 0, '{"data":{"addProjectV2ItemById":{"item":{"id":"PVTI_9"}}}}', ""
        ),
    )
    receipt = issue_projects.add_item(
        tmp_path,
        issue_projects.Project("acme/3", "P3", "Roadmap"),
        issue_projects.ProjectTarget("I7", "https://github.com/acme/app/pull/7", None),
        7,
    )
    assert receipt.url == "https://github.com/acme/app/pull/7#project-item-PVTI_9"
    assert receipt.item_id == "PVTI_9"
    payload = json.loads(run.call_args.kwargs["input"])
    assert payload["variables"] == {"project": "P3", "content": "I7"}
    assert "addProjectV2ItemById" in payload["query"]
