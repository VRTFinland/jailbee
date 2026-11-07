"""Stage issue proposals from inside a container."""

import json
import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer

from jailbee.container_outbox import broker, gh
from jailbee.container_outbox.store import OutboxCommandError, Store

issue_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Stage GitHub issue changes in the outbox; the host operator publishes them "
        "(the container's gh is read-only)."
    ),
)

IssueArg = Annotated[
    str | None,
    typer.Argument(help="Issue number, or @REF for a create staged earlier in the same batch."),
]
RepoOpt = Annotated[
    str,
    typer.Option(help="Repository: a checkout path, github:OWNER/NAME, or . for this one."),
]
BatchOpt = Annotated[
    str | None,
    typer.Option(help="Append to the draft batch NAME instead of staging now; see outbox submit."),
]
BodyOpt = Annotated[str | None, typer.Option(help="Text, inline.")]
BodyFileOpt = Annotated[Path | None, typer.Option(help="Read the text from this file.")]


def _target(target: int | str) -> dict[str, object]:
    return {"issue": target} if isinstance(target, int) else {"issue_ref": target.removeprefix("@")}


def create_action(
    repo: str, title: str, body: str, labels: list[str], ref: str
) -> dict[str, object]:
    return {
        "type": "create",
        "repo": repo,
        "title": title,
        "body": body,
        "labels": labels,
        "ref": ref,
    }


def edit_action(
    repo: str, target: int | str, title: str | None, body: str | None, view: gh.IssueView | None
) -> dict[str, object]:
    action: dict[str, object] = {"type": "edit", "repo": repo, **_target(target)}
    expected: dict[str, object] = {}
    if title is not None:
        action["title"] = title
        expected["title"] = view.title if view else None
    if body is not None:
        action["body"] = body
        expected["body"] = view.body if view else None
    action["expected"] = expected
    return action


def comment_action(repo: str, target: int | str, body: str) -> dict[str, object]:
    return {"type": "comment", "repo": repo, **_target(target), "body": body}


def labels_action(
    repo: str, target: int | str, add: list[str], remove: list[str], view: gh.IssueView | None
) -> dict[str, object]:
    return {
        "type": "labels",
        "repo": repo,
        **_target(target),
        "add": add,
        "remove": remove,
        "expected": {"labels": list(view.labels) if view else []},
    }


def assignees_action(
    repo: str, target: int | str, add: list[str], remove: list[str], view: gh.IssueView | None
) -> dict[str, object]:
    return {
        "type": "assignees",
        "repo": repo,
        **_target(target),
        "add": add,
        "remove": remove,
        "expected": {"assignees": list(view.assignees) if view else []},
    }


def state_action(
    repo: str, target: int | str, state: str, reason: str | None, view: gh.IssueView | None
) -> dict[str, object]:
    action: dict[str, object] = {
        "type": "state",
        "repo": repo,
        **_target(target),
        "state": state,
        "expected": {"state": view.state if view else "open"},
    }
    if reason is not None and not (state == "open" and reason == "reopened"):
        action["reason"] = reason
    return action


def _guard() -> Store:
    store = Store(Path.home())
    store.require_role()
    return store


def _body(body: str | None, body_file: Path | None, required: bool) -> str | None:
    if body is not None and body_file is not None:
        raise OutboxCommandError("choose --body or --body-file", 2)
    if body_file is not None:
        try:
            return body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise OutboxCommandError(f"body file: {exc}", 2) from exc
    if body is None and required:
        raise OutboxCommandError("missing body (--body or --body-file)", 2)
    return body


def _parse_target(value: str | None) -> int | str:
    if not value:
        raise OutboxCommandError("missing issue", 2)
    if value.startswith("@") and len(value) > 1:
        return value
    if value.isdecimal() and int(value) > 0:
        return int(value)
    raise OutboxCommandError("issue must be a positive number or @REF", 2)


def _view(repo: str, target: int | str) -> gh.IssueView | None:
    return gh.issue_view(repo, target) if isinstance(target, int) else None


def _prepare_ref(
    manifest: dict[str, object], bodies: dict[str, str], action: dict[str, object]
) -> dict[str, object]:
    ref = action.get("issue_ref")
    if not isinstance(ref, str):
        return action
    actions = manifest.get("actions")
    assert isinstance(actions, list)
    for create in actions:
        if (
            isinstance(create, dict)
            and create.get("type") == "create"
            and create.get("ref") == ref
            and create.get("repo") == action.get("repo")
        ):
            body = create.get("body")
            if "body_file" in create:
                body = bodies.get(create["body_file"])
            title = create.get("title")
            labels = create.get("labels", [])
            if (
                not isinstance(title, str)
                or not isinstance(body, str)
                or not isinstance(labels, list)
                or not all(isinstance(label, str) for label in labels)
            ):
                raise OutboxCommandError(f"issue_ref {ref!r} has an invalid create action", 2)
            view = gh.IssueView(title, body, tuple(labels), "open")
            result = dict(action)
            if action["type"] == "edit":
                expected = {}
                if "title" in action:
                    expected["title"] = view.title
                if "body" in action or "body_file" in action:
                    expected["body"] = view.body
                result["expected"] = expected
            elif action["type"] == "labels":
                _check_labels(action.get("add", []), action.get("remove", []), view)
                result["expected"] = {"labels": list(view.labels)}
            elif action["type"] == "state":
                result["expected"] = {"state": view.state}
            elif action["type"] == "assignees":
                supplied = action.get("expected")
                if supplied is not None:
                    names = supplied.get("assignees", []) if isinstance(supplied, dict) else None
                    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                        raise OutboxCommandError(
                            "assignees expected.assignees must be a list of login strings", 2
                        )
                _check_assignees(action.get("add", []), action.get("remove", []), view)
                result["expected"] = {"assignees": list(view.assignees)}
            return result
    raise OutboxCommandError(
        f"issue_ref {ref!r} does not name an earlier create in the same repo", 2
    )


def _check_labels(add: object, remove: object, view: gh.IssueView | None) -> None:
    assert isinstance(add, list) and isinstance(remove, list)
    current = {x.casefold() for x in view.labels} if view else set()
    for item in add:
        if item.casefold() in current:
            raise OutboxCommandError(f"label {item!r} already present", 2)
    for item in remove:
        if item.casefold() not in current:
            raise OutboxCommandError(f"label {item!r} is not present", 2)


ASSIGNEE_ME = "@me"
# Syntax only, mirroring issue_manifest._LOGIN_RE (container code imports no host module).
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_]|-(?=[A-Za-z0-9_])){0,38}")


def _check_assignees(add: object, remove: object, view: gh.IssueView | None) -> None:
    for field, items in (("add", add), ("remove", remove)):
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise OutboxCommandError(f"assignees {field} must be a list of login strings", 2)
    assert isinstance(add, list) and isinstance(remove, list)
    for items in (add, remove):
        seen: set[str] = set()
        for item in items:
            if item != ASSIGNEE_ME and not _LOGIN_RE.fullmatch(item):
                raise OutboxCommandError(f"invalid GitHub login {item!r}", 2)
            if item.casefold() in seen:
                raise OutboxCommandError(f"assignee {item!r} given more than once", 2)
            seen.add(item.casefold())
    removed = {x.casefold() for x in remove}
    for item in add:
        if item.casefold() in removed:
            raise OutboxCommandError(f"login {item!r} cannot be both add and remove", 2)
    current = {x.casefold() for x in view.assignees} if view else set()
    for item in add:
        if item != ASSIGNEE_ME and item.casefold() in current:
            raise OutboxCommandError(f"assignee {item!r} already assigned", 2)
    for item in remove:
        if item != ASSIGNEE_ME and item.casefold() not in current:
            raise OutboxCommandError(f"assignee {item!r} is not assigned", 2)


def _stage(
    store: Store,
    action: dict[str, object],
    batch: str | None,
    file_body: bool = False,
    as_json: bool = False,
) -> None:
    bodies: dict[str, str] = {}
    body = action.get("body")
    if isinstance(body, str) and (file_body or len(body.encode("utf-8")) > 4096):
        name = f"{uuid4().hex}.md"
        bodies[name] = body
        del action["body"]
        action["body_file"] = name
    if batch is not None:
        store.draft_append("issue", batch, action, bodies, prepare=_prepare_ref)
        typer.echo(f"staged issue draft {batch}")
    else:
        slug = (
            re.sub(r"[^a-z0-9]+", "-", str(action.get("title", action["type"])).lower()).strip("-")[
                :80
            ]
            or "issue"
        )
        name = broker.put("issue", slug, {"version": 1, "actions": [action]}, bodies)
        if as_json:
            typer.echo(json.dumps({"name": name}))
        else:
            typer.echo(
                f"staged issue/{name} — publish on the host: "
                f"jb issue apply {socket.gethostname()} {name}"
            )


def _run(fn: Callable[[], None]) -> None:
    try:
        fn()
    except (OutboxCommandError, gh.GhError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(exc.exit_code if isinstance(exc, OutboxCommandError) else 1) from exc


@issue_app.command("create")
def create(
    title: Annotated[str | None, typer.Option(help="Issue title (required).")] = None,
    body: BodyOpt = None,
    body_file: BodyFileOpt = None,
    label: Annotated[list[str] | None, typer.Option(help="Label to add; repeatable.")] = None,
    repo: RepoOpt = ".",
    ref: Annotated[
        str | None,
        typer.Option(help="Name for later batch actions to target as @REF (default: issue)."),
    ] = None,
    batch: BatchOpt = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the staged manifest name as JSON.")
    ] = False,
) -> None:
    """Stage a new issue; give the body with --body or --body-file."""

    def run() -> None:
        store = _guard()
        if as_json and batch is not None:
            raise OutboxCommandError(
                "--json cannot be combined with --batch; commit the draft first", 2
            )
        if not title:
            raise OutboxCommandError("missing title", 2)
        text = _body(body, body_file, True)
        assert text is not None
        _stage(
            store,
            create_action(repo, title, text, label or [], ref or "issue"),
            batch,
            body_file is not None,
            as_json,
        )

    _run(run)


@issue_app.command("edit")
def edit(
    issue: IssueArg = None,
    title: Annotated[str | None, typer.Option(help="New title.")] = None,
    body: BodyOpt = None,
    body_file: BodyFileOpt = None,
    repo: RepoOpt = ".",
    batch: BatchOpt = None,
) -> None:
    """Stage a title and/or body replacement for an issue."""

    def run() -> None:
        store = _guard()
        target = _parse_target(issue)
        text = _body(body, body_file, False)
        if title is None and text is None:
            raise OutboxCommandError("missing title or body", 2)
        _stage(
            store,
            edit_action(repo, target, title, text, _view(repo, target)),
            batch,
            body_file is not None,
        )

    _run(run)


@issue_app.command("comment")
def comment(
    issue: IssueArg = None,
    body: BodyOpt = None,
    body_file: BodyFileOpt = None,
    repo: RepoOpt = ".",
    batch: BatchOpt = None,
) -> None:
    """Stage a comment on an issue."""

    def run() -> None:
        store = _guard()
        target = _parse_target(issue)
        text = _body(body, body_file, True)
        assert text is not None
        _stage(store, comment_action(repo, target, text), batch, body_file is not None)

    _run(run)


@issue_app.command("labels")
def labels(
    issue: IssueArg = None,
    add: Annotated[list[str] | None, typer.Option(help="Label to add; repeatable.")] = None,
    remove: Annotated[list[str] | None, typer.Option(help="Label to remove; repeatable.")] = None,
    repo: RepoOpt = ".",
    batch: BatchOpt = None,
) -> None:
    """Stage label additions and removals for an issue."""

    def run() -> None:
        store = _guard()
        target = _parse_target(issue)
        view = _view(repo, target)
        if view is not None:
            _check_labels(add or [], remove or [], view)
        _stage(store, labels_action(repo, target, add or [], remove or [], view), batch)

    _run(run)


@issue_app.command("assign")
def assign(
    issue: IssueArg = None,
    me: Annotated[
        bool, typer.Option("--me", help="Assign the publishing account (resolved on the host).")
    ] = False,
    add: Annotated[
        list[str] | None, typer.Option(help="GitHub login to assign; repeatable.")
    ] = None,
    remove: Annotated[
        list[str] | None, typer.Option(help="GitHub login to unassign; repeatable.")
    ] = None,
    unassign_me: Annotated[
        bool, typer.Option("--unassign-me", help="Unassign the publishing account.")
    ] = False,
    repo: RepoOpt = ".",
    batch: BatchOpt = None,
) -> None:
    """Stage assignee changes for an issue; give at least one of the flags."""

    def run() -> None:
        store = _guard()
        target = _parse_target(issue)
        if not (me or unassign_me or add or remove):
            raise OutboxCommandError("choose --me, --unassign-me, --add or --remove", 2)
        add_list = ([ASSIGNEE_ME] if me else []) + list(add or [])
        remove_list = ([ASSIGNEE_ME] if unassign_me else []) + list(remove or [])
        view = _view(repo, target)
        _check_assignees(add_list, remove_list, view)
        _stage(store, assignees_action(repo, target, add_list, remove_list, view), batch)

    _run(run)


@issue_app.command("state")
def state(
    issue: IssueArg = None,
    state: Annotated[str | None, typer.Argument(help="New state: open or closed.")] = None,
    reason: Annotated[
        str | None,
        typer.Option(help="Close reason, e.g. completed or not_planned; reopened for open."),
    ] = None,
    repo: RepoOpt = ".",
    batch: BatchOpt = None,
) -> None:
    """Stage closing or reopening an issue."""

    def run() -> None:
        store = _guard()
        target = _parse_target(issue)
        if state is None:
            raise OutboxCommandError("missing state", 2)
        _stage(store, state_action(repo, target, state, reason, _view(repo, target)), batch)

    _run(run)
