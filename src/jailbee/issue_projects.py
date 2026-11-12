"""Host-only Projects v2 GraphQL transport; no container token is consulted."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jailbee.issue_github import IssueGithubMutationError, IssueGithubReadError, MutationReceipt
from jailbee.issue_manifest import valid_project


@dataclass(frozen=True)
class Project:
    board: str
    id: str
    title: str


@dataclass(frozen=True)
class ProjectTarget:
    content_id: str
    url: str
    item_id: str | None

    @property
    def present(self) -> bool:
        return self.item_id is not None


@dataclass(frozen=True)
class ProjectReceipt(MutationReceipt):
    item_id: str


def _graphql(
    root: Path, query: str, variables: dict[str, object], *, mutation: bool = False
) -> dict[str, Any]:
    def fail(message: str, uncertain: bool = False) -> None:
        if mutation:
            raise IssueGithubMutationError(message, uncertain=uncertain)
        raise IssueGithubReadError(message)

    try:
        proc = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            cwd=root,
            input=json.dumps({"query": query, "variables": variables}),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError:
        fail("GitHub Projects requires 'gh'")
        raise AssertionError from None
    except (OSError, UnicodeError, subprocess.SubprocessError, KeyboardInterrupt):
        fail("GitHub Projects transport failed", uncertain=True)
        raise AssertionError from None
    try:
        response = json.loads(proc.stdout)
    except (ValueError, UnicodeError):
        response = None
    errors = response.get("errors") if isinstance(response, dict) else None
    if errors:
        text = json.dumps(errors).lower()
        scope = any(word in text for word in ("scope", "permission", "resource not accessible"))
        fail(
            "GitHub Projects requires host authorization: gh auth refresh -s project"
            if scope
            else "GitHub Projects GraphQL request was rejected"
        )
    if proc.returncode:
        scope = "scope" in proc.stderr.lower() or (
            "project" in proc.stderr.lower() and "permission" in proc.stderr.lower()
        )
        fail(
            "GitHub Projects requires host authorization: gh auth refresh -s project"
            if scope
            else "GitHub Projects transport failed",
            uncertain=not scope,
        )
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        fail("GitHub Projects returned an unreadable response", uncertain=True)
    assert isinstance(data, dict)
    return data


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IssueGithubReadError("GitHub Projects returned an unexpected response")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise IssueGithubReadError("GitHub Projects returned an unexpected response")
    return value


def resolve_project(root: Path, board: str) -> Project:
    """Resolve owner type once; never retry a failed read as a different owner."""
    if not valid_project(board):
        raise IssueGithubReadError("project must be owner/positive-number")
    owner, number = board.split("/")
    data = _graphql(
        root, "query($owner:String!){repositoryOwner(login:$owner){__typename}}", {"owner": owner}
    )
    kind = _object(data.get("repositoryOwner")).get("__typename")
    if kind not in ("Organization", "User"):
        raise IssueGithubReadError("GitHub project owner was not found")
    field = "organization" if kind == "Organization" else "user"
    data = _graphql(
        root,
        f"query($owner:String!,$number:Int!){{{field}(login:$owner)"
        "{projectV2(number:$number){id title}}}",
        {"owner": owner, "number": int(number)},
    )
    project = _object(_object(data.get(field)).get("projectV2"))
    return Project(board, _string(project.get("id")), _string(project.get("title")))


def get_target(root: Path, project: Project, repo: str, number: int) -> ProjectTarget:
    """Find issue/PR identity and membership, exhausting all project-item pages."""
    owner, name = repo.split("/")
    cursor: str | None = None
    seen_cursors: set[str] = set()
    content_id = url = ""
    item_id = None
    while True:
        data = _graphql(
            root,
            "query($owner:String!,$name:String!,$number:Int!,$cursor:String){"
            "repository(owner:$owner,name:$name){issueOrPullRequest(number:$number){"
            "... on Issue{id url projectItems(first:100,after:$cursor){"
            "nodes{id project{id}} pageInfo{hasNextPage endCursor}}} "
            "... on PullRequest{id url projectItems(first:100,after:$cursor){"
            "nodes{id project{id}} pageInfo{hasNextPage endCursor}}}}}}",
            {"owner": owner, "name": name, "number": number, "cursor": cursor},
        )
        target = _object(_object(data.get("repository")).get("issueOrPullRequest"))
        content_id, url = _string(target.get("id")), _string(target.get("url"))
        connection = _object(target.get("projectItems"))
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise IssueGithubReadError("GitHub Projects returned an unexpected response")
        for raw in nodes:
            item = _object(raw)
            if _object(item.get("project")).get("id") == project.id:
                item_id = _string(item.get("id"))
        page = _object(connection.get("pageInfo"))
        if page.get("hasNextPage") is False:
            return ProjectTarget(content_id, url, item_id)
        next_cursor = _string(page.get("endCursor"))
        if page.get("hasNextPage") is not True or next_cursor in seen_cursors:
            raise IssueGithubReadError("GitHub Projects returned invalid pagination")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def add_item(root: Path, project: Project, target: ProjectTarget, number: int) -> ProjectReceipt:
    """An already-present item is a no-op. The durable receipt URL carries its
    item id in ``#project-item-ID``, retained by both the journal and sent log.
    """
    item_id = target.item_id
    if item_id is None:
        data = _graphql(
            root,
            "mutation($project:ID!,$content:ID!){addProjectV2ItemById(input:{projectId:$project,contentId:$content}){item{id}}}",
            {"project": project.id, "content": target.content_id},
            mutation=True,
        )
        try:
            item_id = _string(
                _object(_object(data.get("addProjectV2ItemById")).get("item")).get("id")
            )
        except IssueGithubReadError:
            raise IssueGithubMutationError(
                "GitHub Projects returned an unusable receipt", uncertain=True
            ) from None
    return ProjectReceipt(number, f"{target.url}#project-item-{item_id}", item_id)
