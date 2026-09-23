"""Pure parsing and validation for issue outbox manifest version 1."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

MAX_MANIFEST_BYTES = 256 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_ACTIONS = 50
MAX_MANIFESTS = 20
MAX_OFFER_ACTIONS = 100


class IssueManifestError(Exception):
    """A selected issue outbox manifest is malformed or exceeds a v1 limit."""


@dataclass(frozen=True)
class ExistingIssue:
    number: int


@dataclass(frozen=True)
class CreatedIssue:
    ref: str


IssueTarget = ExistingIssue | CreatedIssue


@dataclass(frozen=True)
class CreateAction:
    repo: str
    ref: str
    title: str
    body: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class EditAction:
    repo: str
    target: IssueTarget
    title: str | None
    body: str | None
    expected_title: str | None
    expected_body: str | None
    has_expected_title: bool
    has_expected_body: bool


@dataclass(frozen=True)
class CommentAction:
    repo: str
    target: IssueTarget
    body: str


@dataclass(frozen=True)
class LabelsAction:
    repo: str
    target: IssueTarget
    add: tuple[str, ...]
    remove: tuple[str, ...]
    expected_labels: tuple[str, ...]


@dataclass(frozen=True)
class StateAction:
    repo: str
    target: IssueTarget
    state: Literal["open", "closed"]
    reason: Literal["completed", "not_planned"] | None
    expected_state: Literal["open", "closed"]


IssueAction = CreateAction | EditAction | CommentAction | LabelsAction | StateAction


@dataclass(frozen=True)
class IssueManifest:
    name: str
    version: Literal[1]
    actions: tuple[IssueAction, ...]
    body_files: frozenset[str]


def _context(name: str, index: int | None = None) -> str:
    return name if index is None else f"{name} action {index}"


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise IssueManifestError(f"{context}: must be a JSON object")
    return cast(dict[str, object], value)


def _reject_unknown(item: Mapping[str, object], allowed: set[str], context: str) -> None:
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise IssueManifestError(f"{context}: unknown field {unknown[0]!r}")


def _required_string(
    item: Mapping[str, object], field: str, context: str, *, nonempty: bool = True
) -> str:
    if field not in item:
        raise IssueManifestError(f"{context}: missing required field {field!r}")
    value = item[field]
    if not isinstance(value, str) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise IssueManifestError(f"{context}: {field} must be a {qualifier}string")
    return value


def _repo(item: Mapping[str, object], context: str) -> str:
    repo = _required_string(item, "repo", context)
    if repo == ".":
        return repo
    parts = repo.split("/")
    if repo.startswith("/") or "\\" in repo or any(part in {"", ".", ".."} for part in parts):
        raise IssueManifestError(
            f"{context}: repo must be '.' or a normalized relative POSIX submodule path"
        )
    return repo


def _string_or_null(value: object, field: str, context: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise IssueManifestError(f"{context}: {field} must be a string or null")
    return value


def _utf8_size(value: str, field: str, context: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise IssueManifestError(f"{context}: {field} is not valid UTF-8") from exc


def _validate_body_size(body: str, field: str, context: str) -> None:
    if _utf8_size(body, field, context) > MAX_BODY_BYTES:
        raise IssueManifestError(f"{context}: {field} exceeds the 64 KiB limit")


def _labels(value: object, field: str, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise IssueManifestError(f"{context}: {field} must be a list of non-empty strings")
    labels: list[str] = []
    normalized: set[str] = set()
    for label in value:
        if not isinstance(label, str) or not label:
            raise IssueManifestError(f"{context}: {field} must contain non-empty strings")
        folded = label.casefold()
        if folded in normalized:
            raise IssueManifestError(f"{context}: duplicate label {label!r} in {field}")
        normalized.add(folded)
        labels.append(label)
    return tuple(labels)


def _resolve_body(
    item: Mapping[str, object],
    files: Mapping[str, str],
    body_files: set[str],
    context: str,
    *,
    required: bool,
) -> str | None:
    has_body = "body" in item
    has_body_file = "body_file" in item
    if has_body and has_body_file:
        raise IssueManifestError(f"{context}: exactly one of body or body_file is allowed")
    if not has_body and not has_body_file:
        if required:
            raise IssueManifestError(f"{context}: exactly one of body or body_file is required")
        return None

    if has_body:
        value = item["body"]
        if not isinstance(value, str):
            raise IssueManifestError(f"{context}: body must be a string")
        body = value
    else:
        filename = item["body_file"]
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
        ):
            raise IssueManifestError(
                f"{context}: body_file must be a plain filename at the outbox root"
            )
        if filename not in files:
            raise IssueManifestError(
                f"{context}: body_file {filename!r} is missing from the outbox"
            )
        body = files[filename]
        body_files.add(filename)

    _validate_body_size(body, "body", context)
    return body


def _target(
    item: Mapping[str, object],
    repo: str,
    refs: Mapping[str, str],
    context: str,
) -> IssueTarget:
    has_issue = "issue" in item
    has_ref = "issue_ref" in item
    if has_issue == has_ref:
        raise IssueManifestError(f"{context}: exactly one of issue or issue_ref is required")
    if has_issue:
        value = item["issue"]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise IssueManifestError(f"{context}: issue must be a positive integer")
        return ExistingIssue(value)

    ref = item["issue_ref"]
    if not isinstance(ref, str) or not ref:
        raise IssueManifestError(f"{context}: issue_ref must name an earlier create action")
    ref_repo = refs.get(ref)
    if ref_repo is None:
        raise IssueManifestError(
            f"{context}: issue_ref {ref!r} does not name an earlier create action"
        )
    if ref_repo != repo:
        raise IssueManifestError(
            f"{context}: issue_ref {ref!r} must target a create in the same repo"
        )
    return CreatedIssue(ref)


def _expected(item: Mapping[str, object], context: str) -> dict[str, object]:
    if "expected" not in item:
        raise IssueManifestError(f"{context}: missing required field 'expected'")
    return _object(item["expected"], f"{context} expected")


def _target_key(repo: str, target: IssueTarget) -> tuple[str, str, int | str]:
    if isinstance(target, ExistingIssue):
        return repo, "issue", target.number
    return repo, "ref", target.ref


def _record_mutations(
    changed: dict[tuple[str, str, int | str], set[str]],
    repo: str,
    target: IssueTarget,
    fields: set[str],
    context: str,
) -> None:
    seen = changed.setdefault(_target_key(repo, target), set())
    duplicate = sorted(seen & fields)
    if duplicate:
        raise IssueManifestError(f"{context}: target already changes field {duplicate[0]!r}")
    seen.update(fields)


def _parse_create(
    item: dict[str, object],
    files: Mapping[str, str],
    body_files: set[str],
    refs: dict[str, str],
    context: str,
) -> CreateAction:
    _reject_unknown(item, {"type", "repo", "ref", "title", "body", "body_file", "labels"}, context)
    repo = _repo(item, context)
    ref = _required_string(item, "ref", context)
    if ref in refs:
        raise IssueManifestError(f"{context}: duplicate create ref {ref!r}")
    title = _required_string(item, "title", context)
    body = _resolve_body(item, files, body_files, context, required=True)
    assert body is not None
    labels = _labels(item.get("labels", []), "labels", context)
    refs[ref] = repo
    return CreateAction(repo=repo, ref=ref, title=title, body=body, labels=labels)


def _parse_edit(
    item: dict[str, object],
    files: Mapping[str, str],
    body_files: set[str],
    refs: Mapping[str, str],
    changed: dict[tuple[str, str, int | str], set[str]],
    context: str,
) -> EditAction:
    _reject_unknown(
        item,
        {"type", "repo", "issue", "issue_ref", "title", "body", "body_file", "expected"},
        context,
    )
    repo = _repo(item, context)
    target = _target(item, repo, refs, context)
    has_title = "title" in item
    if has_title:
        title = _required_string(item, "title", context)
    else:
        title = None
    body = _resolve_body(item, files, body_files, context, required=False)
    has_body = "body" in item or "body_file" in item
    if not has_title and not has_body:
        raise IssueManifestError(f"{context}: edit must change title or body")

    expected = _expected(item, context)
    _reject_unknown(expected, {"title", "body"}, f"{context} expected")
    has_expected_title = "title" in expected
    has_expected_body = "body" in expected
    if has_title != has_expected_title:
        detail = (
            "expected.title is required" if has_title else "expected.title is not being changed"
        )
        raise IssueManifestError(f"{context}: {detail}")
    if has_body != has_expected_body:
        detail = "expected.body is required" if has_body else "expected.body is not being changed"
        raise IssueManifestError(f"{context}: {detail}")
    expected_title = (
        _string_or_null(expected["title"], "expected.title", context)
        if has_expected_title
        else None
    )
    expected_body = (
        _string_or_null(expected["body"], "expected.body", context) if has_expected_body else None
    )
    if expected_body is not None:
        _validate_body_size(expected_body, "expected.body", context)
    fields = ({"title"} if has_title else set()) | ({"body"} if has_body else set())
    _record_mutations(changed, repo, target, fields, context)
    return EditAction(
        repo=repo,
        target=target,
        title=title,
        body=body,
        expected_title=expected_title,
        expected_body=expected_body,
        has_expected_title=has_expected_title,
        has_expected_body=has_expected_body,
    )


def _parse_comment(
    item: dict[str, object],
    files: Mapping[str, str],
    body_files: set[str],
    refs: Mapping[str, str],
    context: str,
) -> CommentAction:
    _reject_unknown(item, {"type", "repo", "issue", "issue_ref", "body", "body_file"}, context)
    repo = _repo(item, context)
    target = _target(item, repo, refs, context)
    body = _resolve_body(item, files, body_files, context, required=True)
    assert body is not None
    if not body:
        raise IssueManifestError(f"{context}: comment body must be non-empty")
    return CommentAction(repo=repo, target=target, body=body)


def _parse_label_action(
    item: dict[str, object],
    refs: Mapping[str, str],
    changed: dict[tuple[str, str, int | str], set[str]],
    context: str,
) -> LabelsAction:
    _reject_unknown(
        item, {"type", "repo", "issue", "issue_ref", "add", "remove", "expected"}, context
    )
    repo = _repo(item, context)
    target = _target(item, repo, refs, context)
    add = _labels(item.get("add", []), "add", context)
    remove = _labels(item.get("remove", []), "remove", context)
    if not add and not remove:
        raise IssueManifestError(f"{context}: at least one label in add or remove is required")
    overlap = {label.casefold() for label in add} & {label.casefold() for label in remove}
    if overlap:
        raise IssueManifestError(f"{context}: a label cannot appear in both add and remove")
    if "expected" not in item:
        raise IssueManifestError(f"{context}: expected.labels is required")
    expected = _expected(item, context)
    _reject_unknown(expected, {"labels"}, f"{context} expected")
    if "labels" not in expected:
        raise IssueManifestError(f"{context}: expected.labels is required")
    expected_labels = _labels(expected["labels"], "expected.labels", context)
    _record_mutations(changed, repo, target, {"labels"}, context)
    return LabelsAction(
        repo=repo, target=target, add=add, remove=remove, expected_labels=expected_labels
    )


def _state(value: object, field: str, context: str) -> Literal["open", "closed"]:
    if not isinstance(value, str) or value not in ("open", "closed"):
        raise IssueManifestError(f"{context}: {field} must be 'open' or 'closed'")
    return cast(Literal["open", "closed"], value)


def _parse_state_action(
    item: dict[str, object],
    refs: Mapping[str, str],
    changed: dict[tuple[str, str, int | str], set[str]],
    context: str,
) -> StateAction:
    _reject_unknown(
        item, {"type", "repo", "issue", "issue_ref", "state", "reason", "expected"}, context
    )
    repo = _repo(item, context)
    target = _target(item, repo, refs, context)
    if "state" not in item:
        raise IssueManifestError(f"{context}: missing required field 'state'")
    state = _state(item["state"], "state", context)
    reason: Literal["completed", "not_planned"] | None
    if state == "closed":
        raw_reason = item.get("reason")
        if not isinstance(raw_reason, str) or raw_reason not in ("completed", "not_planned"):
            raise IssueManifestError(
                f"{context}: closing requires reason 'completed' or 'not_planned'"
            )
        reason = cast(Literal["completed", "not_planned"], raw_reason)
    else:
        if "reason" in item:
            raise IssueManifestError(f"{context}: reopening forbids reason")
        reason = None
    expected = _expected(item, context)
    _reject_unknown(expected, {"state"}, f"{context} expected")
    if "state" not in expected:
        raise IssueManifestError(f"{context}: expected.state is required")
    expected_state = _state(expected["state"], "expected.state", context)
    if state == expected_state:
        raise IssueManifestError(f"{context}: state must differ from expected.state")
    _record_mutations(changed, repo, target, {"state"}, context)
    return StateAction(
        repo=repo, target=target, state=state, reason=reason, expected_state=expected_state
    )


def parse_manifest(name: str, text: str, files: Mapping[str, str]) -> IssueManifest:
    """Parse one issue manifest using only its text and outbox file snapshot."""
    if _utf8_size(text, "manifest", name) > MAX_MANIFEST_BYTES:
        raise IssueManifestError(f"{name}: manifest exceeds the 256 KiB limit")
    try:
        decoded: object = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IssueManifestError(f"{name}: invalid JSON: {exc}") from exc
    root = _object(decoded, name)
    _reject_unknown(root, {"version", "actions"}, name)
    if type(root.get("version")) is not int or root["version"] != 1:
        raise IssueManifestError(f"{name}: version must be exactly 1")
    raw_actions = root.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise IssueManifestError(f"{name}: actions must be a non-empty list")
    if len(raw_actions) > MAX_ACTIONS:
        raise IssueManifestError(f"{name}: manifest contains more than 50 actions")

    actions: list[IssueAction] = []
    body_files: set[str] = set()
    refs: dict[str, str] = {}
    changed: dict[tuple[str, str, int | str], set[str]] = {}
    for index, raw_action in enumerate(raw_actions):
        context = _context(name, index)
        item = _object(raw_action, context)
        action_type = item.get("type")
        action: IssueAction
        if action_type == "create":
            action = _parse_create(item, files, body_files, refs, context)
        elif action_type == "edit":
            action = _parse_edit(item, files, body_files, refs, changed, context)
        elif action_type == "comment":
            action = _parse_comment(item, files, body_files, refs, context)
        elif action_type == "labels":
            action = _parse_label_action(item, refs, changed, context)
        elif action_type == "state":
            action = _parse_state_action(item, refs, changed, context)
        else:
            raise IssueManifestError(
                f"{context}: type must be one of create, edit, comment, labels, or state"
            )
        actions.append(action)

    return IssueManifest(
        name=name, version=1, actions=tuple(actions), body_files=frozenset(body_files)
    )
