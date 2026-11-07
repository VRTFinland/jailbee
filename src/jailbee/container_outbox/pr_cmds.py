"""Stage PR comments, descriptions, reviews and imported sources in the host outbox store."""

import json
import socket
from pathlib import Path
from typing import Annotated, cast
from uuid import uuid4

import typer

from jailbee.container_outbox import bindings, broker, gh
from jailbee.container_outbox.common import OutboxCommandError
from jailbee.container_outbox.issue_cmds import BatchOpt, BodyFileOpt, BodyOpt, _body, _guard, _run

pr_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Stage pull request comments, replies and descriptions in the outbox; the host "
        "operator publishes them (the container's gh is read-only)."
    ),
)
review_app = typer.Typer(
    no_args_is_help=True,
    help="Build an inline review draft (comments plus a verdict body) and stage it.",
)
PrArg = Annotated[
    str | None,
    typer.Argument(help="PR number; omit to use the PR bound to this container."),
]
RepoOpt = Annotated[str | None, typer.Option(help="Repository as OWNER/NAME.")]
pr_app.add_typer(review_app, name="review")


def _number(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise OutboxCommandError("PR must be a positive number", 2)
    return int(value)


def _view(value: str | None) -> gh.PrView:
    number = _number(value)
    binding = bindings.resolve(number)
    if binding is not None:
        view = gh.pr_view(binding.number, cwd=binding.cwd)
    else:
        view = gh.pr_view(number)
    if view.number != (binding.number if binding else number):
        raise OutboxCommandError("GitHub returned a different PR than the requested binding", 2)
    return view


def _envelope(view: gh.PrView | None, repo: str | None = None) -> dict[str, object]:
    if view is None and not repo:
        repo = gh.repo_view()
    return {
        "version": 1,
        "repo": view.repo if view else repo,
        "pr": view.number if view else None,
        "head_sha": view.head_sha if view else None,
        "actions": [],
    }


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
    if not isinstance(actions, list) or (
        actions
        and (
            len(actions) != 1
            or not isinstance(actions[0], dict)
            or actions[0].get("type") != "review"
            or not isinstance(actions[0].get("comments"), list)
        )
    ):
        raise OutboxCommandError("not a review draft; choose a different batch name", 2)
    return cast(list[dict[str, object]], actions)


def _announce(name: str) -> None:
    typer.echo(
        f"staged pr/{name} - publish on the host: jb review apply {socket.gethostname()} {name}"
    )


def _stage(
    envelope: dict[str, object],
    action: dict[str, object],
    batch: str | None,
    file_body: bool = False,
) -> None:
    """Stage one action through the host broker: alone, or into the draft BATCH.

    The broker re-validates the manifest and keeps the draft; it refuses a
    batch action for another PR and one appended to a review draft."""
    bodies = _file_body(action, file_body)
    if batch is not None:
        broker.draft_append("pr", batch, action, bodies, envelope=envelope)
        typer.echo(f"staged pr draft {batch}")
    else:
        _announce(broker.put("pr", str(action["type"]), {**envelope, "actions": [action]}, bodies))


@pr_app.command("comment")
def comment(
    pr: PrArg = None,
    body: BodyOpt = None,
    body_file: BodyFileOpt = None,
    reply_to: Annotated[
        int | None, typer.Option(help="Reply to this review comment ID instead of commenting.")
    ] = None,
    batch: BatchOpt = None,
) -> None:
    """Stage a general PR comment."""

    def run() -> None:
        _guard()
        text = _body(body, body_file, True)
        action: dict[str, object] = {"type": "comment", "body": text}
        if reply_to is not None:
            if reply_to < 1:
                raise OutboxCommandError("--reply-to must be positive", 2)
            action["reply_to"] = reply_to
        _stage(_envelope(_view(pr)), action, batch, body_file is not None)

    _run(run)


@pr_app.command("reply")
def reply(
    pr: PrArg = None,
    to: Annotated[int | None, typer.Option(help="ID of the review comment to reply to.")] = None,
    body: BodyOpt = None,
    body_file: BodyFileOpt = None,
    batch: BatchOpt = None,
) -> None:
    """Stage a reply to a review comment."""

    def run() -> None:
        _guard()
        if to is None:
            raise OutboxCommandError("missing --to comment ID", 2)
        if to < 1:
            raise OutboxCommandError("--to must be positive", 2)
        text = _body(body, body_file, True)
        _stage(
            _envelope(_view(pr)),
            {"type": "reply", "comment_id": to, "body": text},
            batch,
            body_file is not None,
        )

    _run(run)


def _description(title: str | None, text: str, branch: str | None) -> dict[str, object]:
    action: dict[str, object] = {"type": "description", "body": text}
    if title is not None:
        action["title"] = title
    if branch is not None:
        action["branch"] = branch
    return action


@pr_app.command("description")
def description(
    pr: PrArg = None,
    title: Annotated[str | None, typer.Option(help="New PR title.")] = None,
    body: Annotated[str | None, typer.Option(help="Replacement body, inline.")] = None,
    body_file: Annotated[
        Path | None, typer.Option(help="Read the replacement body from a file.")
    ] = None,
    branch: Annotated[str | None, typer.Option(help="Branch the description applies to.")] = None,
    repo: RepoOpt = None,
    batch: BatchOpt = None,
    new: Annotated[
        bool, typer.Option(help="Stage for an unbound container, not force-create a PR.")
    ] = False,
) -> None:
    """Stage a PR title and body replacement (a body is required)."""

    def run() -> None:
        _guard()
        if new and pr is not None:
            raise OutboxCommandError("--new cannot be combined with a PR number", 2)
        text = _body(body, body_file, False)
        if text is None and title is None:
            raise OutboxCommandError("missing title or body", 2)
        if text is None:
            raise OutboxCommandError(
                "title-only staging is not supported; provide an explicit replacement body "
                "with --body or --body-file to avoid replaying an outdated PR body",
                2,
            )
        if new and bindings.read():
            raise OutboxCommandError(
                "--new requires no PR bindings; it does not force-create a PR. "
                "Name the existing PR or unbind it on the host first",
                2,
            )
        binding = None if new else bindings.resolve(_number(pr), allow_new=True)
        view = _view(pr) if pr is not None or binding is not None else None
        if view is not None and repo is not None and repo != view.repo:
            raise OutboxCommandError("--repo does not match the existing PR repository", 2)
        _stage(
            _envelope(view, repo),
            _description(title, text, branch),
            batch,
            body_file is not None,
        )

    _run(run)


@review_app.command("add")
def review_add(
    pr: PrArg = None,
    file: Annotated[str | None, typer.Option(help="Path of the file in the diff.")] = None,
    line: Annotated[int | None, typer.Option(help="Line to comment on.")] = None,
    start_line: Annotated[
        int | None, typer.Option(help="First line of a multi-line comment; before --line.")
    ] = None,
    side: Annotated[str, typer.Option(help="Diff side: RIGHT (new) or LEFT (old).")] = "RIGHT",
    body: BodyOpt = None,
    body_file: BodyFileOpt = None,
) -> None:
    """Add an inline comment to the review draft for a PR."""

    def run() -> None:
        _guard()
        if not file:
            raise OutboxCommandError("missing --file path", 2)
        if line is None:
            raise OutboxCommandError("missing --line", 2)
        if line < 1 or (start_line is not None and start_line < 1):
            raise OutboxCommandError("--line and --start-line must be positive", 2)
        if start_line is not None and start_line >= line:
            raise OutboxCommandError("start_line must be before line", 2)
        text = _body(body, body_file, True)
        view = _view(pr)
        envelope = _envelope(view)
        item: dict[str, object] = {"path": file, "line": line, "side": side, "body": text}
        if start_line is not None:
            item["start_line"] = start_line
        bodies = _file_body(item, body_file is not None)
        review: dict[str, object] = {
            "type": "review",
            "event": "COMMENT",
            "body": "",
            "comments": [item],
        }
        # The broker merges the comment into the draft's one review action and
        # refuses it when the PR head moved since the draft was started.
        broker.draft_append("pr", f"review-{view.number}", review, bodies, envelope=envelope)
        typer.echo(f"staged pr review-{view.number}")

    _run(run)


@review_app.command("submit")
def review_submit(
    pr: PrArg = None,
    body: Annotated[str | None, typer.Option(help="Review summary, inline.")] = None,
    body_file: Annotated[
        Path | None, typer.Option(help="Read the review summary from a file.")
    ] = None,
) -> None:
    """Stage the review draft with an optional summary body."""

    def run() -> None:
        _guard()
        text = _body(body, body_file, False)
        view = _view(pr)
        summary: dict[str, object] = {"body": text if text is not None else ""}
        bodies = _file_body(summary, body_file is not None)
        review: dict[str, object] = {
            "repo": view.repo,
            "pr": view.number,
            "head_sha": view.head_sha,
            **summary,
        }
        name = broker.draft_submit("pr", f"review-{view.number}", review=review, bodies=bodies)
        _announce(name)

    _run(run)


@review_app.command("discard")
def review_discard(pr: PrArg = None) -> None:
    """Discard the review draft for a PR."""

    def run() -> None:
        _guard()
        view = _view(pr)
        envelope = _envelope(view)
        name = f"review-{view.number}"
        manifest = json.loads(broker.draft_show("pr", name)["manifest"])
        _same_target(manifest, envelope)
        _review_actions(manifest)
        broker.draft_discard("pr", name)
        typer.echo(f"discarded pr {name}")

    _run(run)


@pr_app.command("source")
def source(
    ref: Annotated[
        str | None, typer.Option("--ref", "--head", help="Branch or ref to open the PR from.")
    ] = None,
    sha: Annotated[str | None, typer.Option(help="Commit the source ref must point at.")] = None,
    base: Annotated[str | None, typer.Option(help="Base branch of the PR.")] = None,
    pr: Annotated[str | None, typer.Option(help="Existing PR number to update.")] = None,
    title: Annotated[str | None, typer.Option(help="PR title.")] = None,
    body: Annotated[str | None, typer.Option(help="PR body, inline.")] = None,
    body_file: Annotated[Path | None, typer.Option(help="Read the PR body from a file.")] = None,
    branch: Annotated[str | None, typer.Option(help="Branch the description applies to.")] = None,
    repo: RepoOpt = None,
) -> None:
    """Stage a PR source manifest; the host pushes the ref and opens or updates the PR."""

    def run() -> None:
        _guard()
        if not ref:
            raise OutboxCommandError("missing --ref (--head)", 2)
        if not sha:
            raise OutboxCommandError("missing --sha", 2)
        number = _number(pr)
        text = _body(body, body_file, False)
        if text is None and (title is not None or branch is not None or number is None):
            raise OutboxCommandError("missing body for source description", 2)
        resolved_repo = repo if repo else (_view(pr).repo if pr is not None else gh.repo_view())
        manifest = _envelope(None, resolved_repo)
        manifest.update(pr=number, source=ref, source_sha=sha)
        if base is not None:
            manifest["base"] = base
        bodies: dict[str, str] = {}
        if text is not None:
            action = _description(title, text, branch)
            bodies = _file_body(action, body_file is not None)
            manifest["actions"] = [action]
        # PR source proposals live in the host outbox store (broker socket);
        # the broker re-validates them and refuses a child container.
        name = broker.put("pr", "source", manifest, bodies)
        typer.echo(
            f"staged pr/{name} - publish on the host: "
            f"jb pr push {socket.gethostname()} --source-manifest {name}"
        )

    _run(run)
