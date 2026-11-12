"""Host-only Projects v2 GraphQL transport; no container token is consulted."""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
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


ProjectValue = str | int | float | None


@dataclass(frozen=True)
class ProjectExpectation:
    present: bool
    value: ProjectValue = None


@dataclass(frozen=True)
class ProjectField:
    id: str
    name: str
    data_type: str
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ResolvedField:
    field: ProjectField
    value: ProjectValue
    payload: dict[str, object]


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


def list_fields(root: Path, project: Project) -> tuple[ProjectField, ...]:
    """Read every board field, including unsupported types for actionable errors."""
    result = []
    for node in _pages(
        root,
        project.id,
        "ProjectV2",
        "fields",
        "... on ProjectV2Field{id name dataType} "
        "... on ProjectV2SingleSelectField{id name dataType options{id name}} "
        "... on ProjectV2IterationField{id name dataType}",
    ):
        options = node.get("options", [])
        if not isinstance(options, list):
            raise IssueGithubReadError("GitHub Projects returned invalid field options")
        result.append(
            ProjectField(
                _string(node.get("id")),
                _string(node.get("name")),
                _string(node.get("dataType")),
                tuple(
                    (_string(_object(o).get("name")), _string(_object(o).get("id")))
                    for o in options
                ),
            )
        )
    return tuple(result)


def _pages(
    root: Path, id: str, node_type: str, connection: str, selection: str
) -> list[dict[str, Any]]:
    cursor = None
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    while True:
        query = (
            "query($id:ID!,$cursor:String){node(id:$id){... on "
            + node_type
            + "{"
            + connection
            + "(first:100,after:$cursor){nodes{"
            + selection
            + "} pageInfo{hasNextPage endCursor}}}}}"
        )
        data = _graphql(root, query, {"id": id, "cursor": cursor})
        page_data = _object(_object(data.get("node")).get(connection))
        nodes = page_data.get("nodes")
        if not isinstance(nodes, list):
            raise IssueGithubReadError("GitHub Projects returned invalid connection")
        result.extend(_object(node) for node in nodes)
        page = _object(page_data.get("pageInfo"))
        if page.get("hasNextPage") is False:
            return result
        cursor = _string(page.get("endCursor"))
        if page.get("hasNextPage") is not True or cursor in seen:
            raise IssueGithubReadError("GitHub Projects returned invalid pagination")
        seen.add(cursor)


def find_field(fields: tuple[ProjectField, ...], name: str) -> ProjectField:
    """Resolve a supported field without requiring a replacement value."""
    matches = [field for field in fields if field.name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError(
            f"unknown or ambiguous project field {name!r}; valid fields: "
            + ", ".join(field.name for field in fields)
        )
    field = matches[0]
    if field.data_type not in ("SINGLE_SELECT", "TEXT", "NUMBER", "DATE"):
        raise ValueError(f"{field.name}: {field.data_type.lower()} fields are not supported yet")
    return field


def resolve_field(fields: tuple[ProjectField, ...], name: str, value: object) -> ResolvedField:
    """Resolve a name and scalar before publishing; invalid names list choices."""
    field = find_field(fields, name)
    kind = field.data_type
    if kind == "SINGLE_SELECT":
        options = [
            (label, id)
            for label, id in field.options
            if isinstance(value, str) and label.casefold() == value.casefold()
        ]
        if len(options) != 1:
            raise ValueError(
                f"unknown option {value!r} for {field.name}; valid options: "
                + ", ".join(label for label, _ in field.options)
            )
        label, id = options[0]
        return ResolvedField(field, label, {"singleSelectOptionId": id})
    if kind == "TEXT":
        if not isinstance(value, str):
            raise ValueError(f"{field.name} requires a text value")
        return ResolvedField(field, value, {"text": value})
    if kind == "NUMBER":
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError
            number = float(Decimal(str(value)))
            if not math.isfinite(number):
                raise ValueError
        except (InvalidOperation, ValueError, OverflowError):
            raise ValueError(f"{field.name} requires a finite decimal number") from None
        return ResolvedField(field, number, {"number": number})
    if kind == "DATE":
        try:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{field.name} requires a valid YYYY-MM-DD date") from None
        return ResolvedField(field, value, {"date": value})
    raise ValueError(f"{field.name}: {kind.lower()} fields are not supported yet")


def get_field_value(root: Path, target: ProjectTarget, field: ProjectField) -> ProjectValue:
    """Read every item value, preserving empty text distinctly from an unset field."""
    if not target.present:
        return None
    assert target.item_id is not None
    nodes = _pages(
        root,
        target.item_id,
        "ProjectV2Item",
        "fieldValues",
        "... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{id}}} "
        "... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{id}}} "
        "... on ProjectV2ItemFieldDateValue{date field{... on ProjectV2FieldCommon{id}}} "
        "... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{id}}}",
    )
    for item in nodes:
        if not item or _object(item.get("field")).get("id") != field.id:
            continue
        key = {"TEXT": "text", "NUMBER": "number", "DATE": "date", "SINGLE_SELECT": "name"}.get(
            field.data_type
        )
        if key is None or key not in item:
            raise IssueGithubReadError("GitHub Projects returned an invalid field value")
        value = item[key]
        if value is None:
            return None
        try:
            return resolve_field((field,), field.name, value).value
        except ValueError as exc:
            raise IssueGithubReadError("GitHub Projects returned an invalid field value") from exc
    return None


def set_field(
    root: Path, project: Project, target: ProjectTarget, number: int, resolved: ResolvedField
) -> ProjectReceipt:
    """Implicit add plus field update is uncertain if only the add succeeded."""
    added = not target.present
    receipt = add_item(root, project, target, number)
    try:
        data = _graphql(
            root,
            "mutation($project:ID!,$item:ID!,$field:ID!,$value:ProjectV2FieldValue!){"
            "updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,"
            "fieldId:$field,value:$value}){projectV2Item{id}}}",
            {
                "project": project.id,
                "item": receipt.item_id,
                "field": resolved.field.id,
                "value": resolved.payload,
            },
            mutation=True,
        )
        returned_id = _object(
            _object(data.get("updateProjectV2ItemFieldValue")).get("projectV2Item")
        ).get("id")
        if returned_id != receipt.item_id:
            raise IssueGithubMutationError(
                "GitHub Projects returned an unusable receipt", uncertain=True
            )
    except IssueGithubReadError:
        raise IssueGithubMutationError(
            "GitHub Projects returned an unusable receipt", uncertain=True
        ) from None
    except IssueGithubMutationError as exc:
        if added and not exc.uncertain:
            raise IssueGithubMutationError(str(exc), uncertain=True) from exc
        raise
    return receipt


def clear_field(
    root: Path, project: Project, target: ProjectTarget, number: int, field: ProjectField
) -> MutationReceipt:
    """Clearing an absent item does not add it to the board."""
    if not target.present:
        return MutationReceipt(number, target.url)
    data = _graphql(
        root,
        "mutation($project:ID!,$item:ID!,$field:ID!){clearProjectV2ItemFieldValue("
        "input:{projectId:$project,itemId:$item,fieldId:$field}){projectV2Item{id}}}",
        {"project": project.id, "item": target.item_id, "field": field.id},
        mutation=True,
    )
    try:
        id = _object(_object(data.get("clearProjectV2ItemFieldValue")).get("projectV2Item")).get(
            "id"
        )
        if id != target.item_id:
            raise IssueGithubReadError("invalid item")
    except IssueGithubReadError:
        raise IssueGithubMutationError(
            "GitHub Projects returned an unusable receipt", uncertain=True
        ) from None
    return MutationReceipt(number, f"{target.url}#project-item-{target.item_id}")


def remove_item(
    root: Path, project: Project, target: ProjectTarget, number: int
) -> MutationReceipt:
    """A matching absent expectation settles without dispatching a mutation."""
    if not target.present:
        return MutationReceipt(number, target.url)
    data = _graphql(
        root,
        "mutation($project:ID!,$item:ID!){deleteProjectV2Item("
        "input:{projectId:$project,itemId:$item}){deletedItemId}}",
        {"project": project.id, "item": target.item_id},
        mutation=True,
    )
    try:
        if _object(data.get("deleteProjectV2Item")).get("deletedItemId") != target.item_id:
            raise IssueGithubReadError("invalid item")
    except IssueGithubReadError:
        raise IssueGithubMutationError(
            "GitHub Projects returned an unusable receipt", uncertain=True
        ) from None
    return MutationReceipt(number, f"{target.url}#project-item-{target.item_id}")
