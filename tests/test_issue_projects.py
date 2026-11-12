import json
import subprocess

import pytest

from jailbee import issue_projects
from jailbee.issue_github import IssueGithubMutationError, IssueGithubReadError


def test_resolve_user_board_uses_explicit_owner_type(tmp_path, mocker):
    run = mocker.patch("jailbee.issue_projects.subprocess.run", side_effect=[
        subprocess.CompletedProcess([], 0, json.dumps({"data": {"repositoryOwner": {"__typename": "User"}}}), ""),
        subprocess.CompletedProcess([], 0, json.dumps({"data": {"user": {"projectV2": {"id": "P_3", "title": "Roadmap"}}}}), ""),
    ])
    board = issue_projects.resolve_project(tmp_path, "acme/3")
    assert (board.id, board.title) == ("P_3", "Roadmap")
    assert run.call_args.args[0] == ["gh", "api", "graphql", "--input", "-"]
    payload = json.loads(run.call_args.kwargs["input"])
    assert "user(login:" in payload["query"]
    assert payload["variables"] == {"owner": "acme", "number": 3}
    assert run.call_args.kwargs["cwd"] == tmp_path


def test_scope_error_does_not_try_another_owner_type(tmp_path, mocker):
    run = mocker.patch("jailbee.issue_projects.subprocess.run", return_value=subprocess.CompletedProcess(
        [], 1, '{"errors":[{"message":"requires project scope"}]}', ""))
    with pytest.raises(IssueGithubReadError, match="gh auth refresh -s project"):
        issue_projects.resolve_project(tmp_path, "acme/3")
    assert run.call_count == 1


@pytest.mark.parametrize("response,uncertain", [
    (subprocess.CompletedProcess([], 0, '{"errors":[{"message":"denied"}]}', ""), False),
    (subprocess.CompletedProcess([], 1, "", "connection lost"), True),
    (subprocess.TimeoutExpired("gh", 60), True),
])
def test_add_classifies_graphql_and_transport_failures(tmp_path, mocker, response, uncertain):
    run = mocker.patch("jailbee.issue_projects.subprocess.run")
    if isinstance(response, Exception):
        run.side_effect = response
    else:
        run.return_value = response
    with pytest.raises(IssueGithubMutationError) as error:
        issue_projects.add_item(tmp_path, issue_projects.Project("acme/3", "P3", "Roadmap"),
                                issue_projects.ProjectTarget("I7", "https://github.com/acme/app/pull/7", None), 7)
    assert error.value.uncertain is uncertain


def test_membership_reads_all_pages_and_organization_owner(tmp_path, mocker):
    def response(data):
        return subprocess.CompletedProcess([], 0, json.dumps({"data": data}), "")
    run = mocker.patch("jailbee.issue_projects.subprocess.run", side_effect=[
        response({"repositoryOwner": {"__typename": "Organization"}}),
        response({"organization": {"projectV2": {"id": "P3", "title": "Roadmap"}}}),
        response({"repository": {"issueOrPullRequest": {"id": "I7", "url": "https://github.com/acme/app/issues/7", "projectItems": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}}),
        response({"repository": {"issueOrPullRequest": {"id": "I7", "url": "https://github.com/acme/app/issues/7", "projectItems": {"nodes": [{"id": "ITEM7", "project": {"id": "P3"}}], "pageInfo": {"hasNextPage": False, "endCursor": "last"}}}}}),
    ])
    board = issue_projects.resolve_project(tmp_path, "acme/3")
    target = issue_projects.get_target(tmp_path, board, "acme/app", 7)
    assert target.item_id == "ITEM7"
    assert "organization(login:" in json.loads(run.call_args_list[1].kwargs["input"])["query"]
    assert json.loads(run.call_args.kwargs["input"])["variables"] == {"owner": "acme", "name": "app", "number": 7, "cursor": "next"}


@pytest.mark.parametrize("connection", [{"nodes": []}, {"nodes": [], "pageInfo": {"hasNextPage": True}}])
def test_membership_malformed_page_fails_closed(tmp_path, mocker, connection):
    mocker.patch("jailbee.issue_projects.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"data": {"repository": {"issueOrPullRequest": {"id": "I7", "url": "https://github.com/acme/app/issues/7", "projectItems": connection}}}}), ""))
    with pytest.raises(IssueGithubReadError):
        issue_projects.get_target(tmp_path, issue_projects.Project("acme/3", "P3", "Roadmap"), "acme/app", 7)


def test_add_returns_target_and_item_receipt(tmp_path, mocker):
    run = mocker.patch("jailbee.issue_projects.subprocess.run", return_value=subprocess.CompletedProcess(
        [], 0, '{"data":{"addProjectV2ItemById":{"item":{"id":"PVTI_9"}}}}', ""))
    receipt = issue_projects.add_item(tmp_path, issue_projects.Project("acme/3", "P3", "Roadmap"),
                                     issue_projects.ProjectTarget("I7", "https://github.com/acme/app/pull/7", None), 7)
    assert receipt.url == "https://github.com/acme/app/pull/7#project-item-PVTI_9"
    assert receipt.item_id == "PVTI_9"
    payload = json.loads(run.call_args.kwargs["input"])
    assert payload["variables"] == {"project": "P3", "content": "I7"}
    assert "addProjectV2ItemById" in payload["query"]
