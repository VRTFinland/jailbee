"""Stage PR comments, descriptions, reviews and imported sources locally."""

from pathlib import Path
from typing import Annotated, cast
from uuid import uuid4
import socket

import typer

from jailbee.container_outbox import gh
from jailbee.container_outbox.issue_cmds import _body, _guard, _run
from jailbee.container_outbox.store import OutboxCommandError, Store

pr_app = typer.Typer(no_args_is_help=True)
review_app = typer.Typer(no_args_is_help=True)
pr_app.add_typer(review_app, name="review")


def _number(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise OutboxCommandError("PR must be a positive number", 2)
    return int(value)


def _view(value: str | None) -> gh.PrView:
    number = _number(value)
    try:
        return gh.pr_view(number)
    except gh.GhError as exc:
        if number is None:
            raise OutboxCommandError(f"{exc}; name the PR number", 2) from exc
        raise


def _envelope(view: gh.PrView | None, repo: str | None = None) -> dict[str, object]:
    if view is None and not repo:
        repo = gh.pr_view(None).repo
    return {"version": 1, "repo": view.repo if view else repo,
            "pr": view.number if view else None, "head_sha": view.head_sha if view else None,
            "actions": []}


def _file_body(item: dict[str, object], file_body: bool) -> dict[str, str]:
    body = item.get("body")
    if isinstance(body, str) and (file_body or len(body.encode("utf-8")) > 4096):
        name = f"{uuid4().hex}.md"
        del item["body"]
        item["body_file"] = name
        return {name: body}
    return {}


def _same_target(manifest: dict[str, object], envelope: dict[str, object]) -> None:
    if any(manifest.get(key) != envelope.get(key) for key in ("repo", "pr")):
        raise OutboxCommandError("draft targets a different repo or PR", 2)


def _review_actions(manifest: dict[str, object]) -> list[dict[str, object]]:
    actions = manifest.get("actions")
    if not isinstance(actions, list) or (actions and (
        len(actions) != 1 or not isinstance(actions[0], dict)
        or actions[0].get("type") != "review"
        or not isinstance(actions[0].get("comments"), list)
    )):
        raise OutboxCommandError("not a review draft; choose a different batch name", 2)
    return cast(list[dict[str, object]], actions)


def _announce(name: str) -> None:
    typer.echo(f"staged pr/{name} - publish on the host: jb review apply {socket.gethostname()} {name}")


def _stage(store: Store, envelope: dict[str, object], action: dict[str, object], batch: str | None, file_body: bool = False) -> None:
    bodies = _file_body(action, file_body)
    if batch is not None:
        def prepare(manifest: dict[str, object], existing: dict[str, str], item: dict[str, object]) -> dict[str, object]:
            if not manifest.get("actions"):
                manifest.update(envelope)
            _same_target(manifest, envelope)
            actions = manifest.get("actions")
            if isinstance(actions, list) and any(
                isinstance(existing_action, dict) and existing_action.get("type") == "review"
                for existing_action in actions
            ):
                raise OutboxCommandError("cannot append a batch action to a review draft", 2)
            return item
        store.draft_append("pr", batch, action, bodies, prepare=prepare)
        typer.echo(f"staged pr draft {batch}")
    else:
        name = store.write_manifest("pr", str(action["type"]), {**envelope, "actions": [action]}, bodies)
        _announce(name)


@pr_app.command("comment")
def comment(pr: Annotated[str | None, typer.Argument()] = None, body: str | None = None, body_file: Path | None = None, reply_to: int | None = None, batch: str | None = None) -> None:
    def run() -> None:
        store = _guard()
        text = _body(body, body_file, True)
        action: dict[str, object] = {"type": "comment", "body": text}
        if reply_to is not None:
            action["reply_to"] = reply_to
        _stage(store, _envelope(_view(pr)), action, batch, body_file is not None)
    _run(run)


@pr_app.command("reply")
def reply(pr: Annotated[str | None, typer.Argument()] = None, to: int | None = None, body: str | None = None, body_file: Path | None = None, batch: str | None = None) -> None:
    def run() -> None:
        store = _guard()
        if to is None:
            raise OutboxCommandError("missing --to comment ID", 2)
        text = _body(body, body_file, True)
        _stage(store, _envelope(_view(pr)), {"type": "reply", "comment_id": to, "body": text}, batch, body_file is not None)
    _run(run)


def _description(title: str | None, text: str, branch: str | None) -> dict[str, object]:
    action: dict[str, object] = {"type": "description", "body": text}
    if title is not None:
        action["title"] = title
    if branch is not None:
        action["branch"] = branch
    return action


@pr_app.command("description")
def description(pr: Annotated[str | None, typer.Argument()] = None, title: str | None = None, body: str | None = None, body_file: Path | None = None, branch: str | None = None, repo: str | None = None, batch: str | None = None) -> None:
    def run() -> None:
        store = _guard()
        text = _body(body, body_file, False)
        if text is None and title is None:
            raise OutboxCommandError("missing title or body", 2)
        view = _view(pr) if pr is not None else None
        if text is None:
            # A title edit must not turn the full replacement body into an empty one.
            if view is None:
                raise OutboxCommandError("missing body for a new PR description", 2)
            text = gh.pr_body(view.number)
        _stage(store, _envelope(view, repo), _description(title, text, branch), batch, body_file is not None)
    _run(run)


@review_app.command("add")
def review_add(pr: Annotated[str | None, typer.Argument()] = None, file: str | None = None, line: int | None = None, start_line: int | None = None, side: str = "RIGHT", body: str | None = None, body_file: Path | None = None) -> None:
    def run() -> None:
        store = _guard()
        if not file:
            raise OutboxCommandError("missing --file path", 2)
        if line is None:
            raise OutboxCommandError("missing --line", 2)
        if start_line is not None and start_line >= line:
            raise OutboxCommandError("start_line must be before line", 2)
        text = _body(body, body_file, True)
        view = _view(pr)
        envelope = _envelope(view)
        item: dict[str, object] = {"path": file, "line": line, "side": side, "body": text}
        if start_line is not None:
            item["start_line"] = start_line
        bodies = _file_body(item, body_file is not None)
        def update(manifest: dict[str, object], existing: dict[str, str]) -> tuple[dict[str, object], dict[str, str]]:
            _same_target(manifest, envelope)
            actions = _review_actions(manifest)
            comments = cast(list[dict[str, object]], actions[0]["comments"]) if actions else []
            return ({**manifest, "actions": [{"type": "review", "event": "COMMENT", "body": "", "comments": [*comments, item]}]}, {**existing, **bodies})
        store.draft_update("pr", f"review-{view.number}", update, initial=envelope)
        typer.echo(f"staged pr review-{view.number}")
    _run(run)


@review_app.command("submit")
def review_submit(pr: Annotated[str | None, typer.Argument()] = None, body: str | None = None, body_file: Path | None = None) -> None:
    def run() -> None:
        store = _guard()
        text = _body(body, body_file, False)
        view = _view(pr)
        envelope = _envelope(view)
        def prepare(manifest: dict[str, object], existing: dict[str, str]) -> tuple[dict[str, object], dict[str, str]]:
            _same_target(manifest, envelope)
            actions = _review_actions(manifest)
            review = {**actions[0], "body": text if text is not None else ""}
            review.pop("body_file", None)
            bodies = _file_body(review, body_file is not None)
            return {**manifest, "head_sha": view.head_sha, "actions": [review]}, {**existing, **bodies}
        name = store.draft_submit("pr", f"review-{view.number}", "review", prepare=prepare)
        _announce(name)
    _run(run)


@review_app.command("discard")
def review_discard(pr: Annotated[str | None, typer.Argument()] = None) -> None:
    def run() -> None:
        store = _guard()
        view = _view(pr)
        envelope = _envelope(view)
        def validate(manifest: dict[str, object], bodies: dict[str, str]) -> None:
            _same_target(manifest, envelope)
            _review_actions(manifest)
        store.draft_discard("pr", f"review-{view.number}", validate=validate)
        typer.echo(f"discarded pr review-{view.number}")
    _run(run)


@pr_app.command("source")
def source(ref: Annotated[str | None, typer.Option("--ref", "--head")] = None, sha: str | None = None, base: str | None = None, pr: str | None = None, title: str | None = None, body: str | None = None, body_file: Path | None = None, branch: str | None = None, repo: str | None = None) -> None:
    def run() -> None:
        store = _guard()
        if not ref:
            raise OutboxCommandError("missing --ref (--head)", 2)
        if not sha:
            raise OutboxCommandError("missing --sha", 2)
        number = _number(pr)
        text = _body(body, body_file, False)
        if text is None and (title is not None or branch is not None or number is None):
            raise OutboxCommandError("missing body for source description", 2)
        resolved_repo = repo if repo else _view(pr).repo
        manifest = _envelope(None, resolved_repo)
        manifest.update(pr=number, source=ref, source_sha=sha)
        if base is not None:
            manifest["base"] = base
        bodies: dict[str, str] = {}
        if text is not None:
            action = _description(title, text, branch)
            bodies = _file_body(action, body_file is not None)
            manifest["actions"] = [action]
        name = store.write_manifest("pr", "source", manifest, bodies)
        typer.echo(f"staged pr/{name} - publish on the host: jb pr push {socket.gethostname()} --source-manifest {name}")
    _run(run)
