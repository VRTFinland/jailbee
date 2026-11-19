"""Browse and manage container outbox proposals and batches."""

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

import typer

from jailbee.container_outbox import broker
from jailbee.container_outbox.common import Kind, OutboxCommandError, require_role
from jailbee.container_outbox.issue_cmds import _run

outbox_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and manage staged issue/PR proposals; the host operator publishes them.",
)
TargetArg = Annotated[str | None, typer.Argument(help="Staged item as issue/NAME or pr/NAME.")]
DraftArg = Annotated[str | None, typer.Argument(help="Draft batch name.")]
KindOpt = Annotated[
    str | None, typer.Option(help="issue or pr; needed if both have a draft of that name.")
]


def _target(value: str | None) -> tuple[Kind, str]:
    if not value:
        raise OutboxCommandError("missing KIND/NAME", 2)
    kind, sep, name = value.partition("/")
    if not sep or kind not in ("issue", "pr") or not name:
        raise OutboxCommandError("expected issue/NAME or pr/NAME", 2)
    return cast(Kind, kind), name


def _draft_exists(kind: Kind, name: str) -> bool:
    # Issue and PR drafts live in the host store.
    return any(row["name"] == name for row in broker.draft_list(kind))


def _resolve(name: str | None, kind: str | None) -> tuple[Kind, str]:
    if not name:
        raise OutboxCommandError("missing NAME", 2)
    if kind is not None and kind not in ("issue", "pr"):
        raise OutboxCommandError("kind must be issue or pr", 2)
    kinds: tuple[Kind, ...] = (cast(Kind, kind),) if kind else ("issue", "pr")
    found = [item for item in kinds if _draft_exists(item, name)]
    if len(found) > 1:
        raise OutboxCommandError("draft exists in both kinds; specify --kind", 2)
    if not found:
        raise OutboxCommandError(f"draft {name!r} not found", 2)
    return found[0], name


def _read_bounded(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path} must be a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} exceeds the {limit} byte limit")
    return data


@outbox_app.command("stage")
def stage(
    kind: Annotated[str | None, typer.Argument(help="issue or pr.")] = None,
    manifest_file: Annotated[
        Path | None, typer.Argument(help="JSON manifest with local inputs.")
    ] = None,
    slug: Annotated[str, typer.Option(help="Proposal name suffix.")] = "evidence",
    batch: Annotated[str | None, typer.Option(help="Append to a named draft instead.")] = None,
) -> None:
    """Capture manifest bodies and images into an immutable host-owned proposal."""

    def run() -> None:
        from jailbee.container_outbox.attachments import capture_inputs

        require_role()
        if kind not in ("issue", "pr"):
            raise OutboxCommandError("supply kind issue or pr", 2)
        if manifest_file is None:
            raise OutboxCommandError("missing manifest file", 2)
        if not re.fullmatch(r"[a-z0-9-]{1,80}", slug):
            raise OutboxCommandError("slug must match [a-z0-9-]{1,80}", 2)
        revision = None
        if batch is not None:
            if not re.fullmatch(r"[a-z0-9-]{1,40}", batch):
                raise OutboxCommandError("batch must match [a-z0-9-]{1,40}", 2)
            if any(row["name"] == batch for row in broker.draft_list(cast(Kind, kind))):
                revision = broker.draft_show(cast(Kind, kind), batch)["revision"]
        try:
            value = json.loads(_read_bounded(manifest_file, 256 * 1024).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("manifest must be a JSON object")
            bodies: dict[str, str] = {}

            def body_inputs(item: object) -> object:
                if isinstance(item, list):
                    return [body_inputs(child) for child in item]
                if not isinstance(item, dict):
                    return item
                result: dict[str, object] = {}
                for key, child in item.items():
                    if key == "body_file":
                        if not isinstance(child, str):
                            raise ValueError("body_file must be a path string")
                        text = _read_bounded(manifest_file.parent / child, 64 * 1024).decode(
                            "utf-8"
                        )
                        name = f"body-{len(bodies) + 1}.md"
                        bodies[name] = text
                        result[key] = name
                    else:
                        result[key] = body_inputs(child)
                return result

            prepared = cast(dict[str, object], body_inputs(value))
            converted = capture_inputs(prepared, manifest_file.parent)
        except (OSError, ValueError) as exc:
            raise OutboxCommandError(f"cannot capture proposal inputs: {exc}", 2) from exc
        if batch is not None:
            actions = converted.get("actions")
            if (
                not isinstance(actions, list)
                or not actions
                or not all(isinstance(action, dict) for action in actions)
            ):
                raise OutboxCommandError("draft input must have a nonempty actions list", 2)
            declarations = cast(dict[str, object], converted.get("attachments", {}))
            if kind == "issue":
                broker.draft_append_many(
                    "issue",
                    batch,
                    actions,
                    bodies,
                    attachments=declarations,
                    expected_revision=revision,
                )
            else:
                if len(actions) != 1:
                    raise OutboxCommandError("append one PR action per draft command", 2)
                envelope = {key: converted.get(key) for key in ("repo", "pr", "head_sha")}
                broker.draft_append(
                    "pr",
                    batch,
                    actions[0],
                    bodies,
                    envelope=envelope,
                    attachments=declarations,
                    expected_revision=revision,
                )
            typer.echo(f"staged {kind} draft {batch}")
        else:
            name = broker.put(cast(Kind, kind), slug, converted, bodies)
            typer.echo(f"staged {kind}/{name}; inspect and publish on the host")

    _run(run)


@outbox_app.command("ls")
def ls(
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON document.")] = False,
) -> None:
    """List staged proposals and open draft batches."""

    def run() -> None:
        data: dict[str, list[dict[str, object]]] = {"issue": [], "pr": [], "drafts": []}
        # Every proposal and draft lives in the host outbox store.
        data["issue"] = [
            {"name": row["name"], "summary": row["summary"]}
            for row in broker.list_proposals("issue")
        ]
        data["pr"] = [
            {"name": row["name"], "summary": row["summary"]} for row in broker.list_proposals("pr")
        ]
        data["drafts"] = [
            {"kind": kind, "name": row["name"], "actions": row["actions"]}
            for kind in ("issue", "pr")
            for row in broker.draft_list(kind)
        ]
        if as_json:
            typer.echo(json.dumps(data))
        else:
            for kind in ("issue", "pr"):
                for item in data[kind]:
                    typer.echo(f"{kind}/{item['name']}  {item['summary']}")
            for draft in data["drafts"]:
                typer.echo(f"draft {draft['kind']}/{draft['name']}  {draft['actions']} actions")

    _run(run)


@outbox_app.command("sent")
def sent(
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON document.")] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, max=200, help="At most this many (default 50).")
    ] = None,
    kind: Annotated[str | None, typer.Option(help="Only issue or pr publications.")] = None,
) -> None:
    """List what the host published from this container, newest first."""

    def run() -> None:
        if kind is not None and kind not in ("issue", "pr"):
            raise OutboxCommandError("kind must be issue or pr", 2)
        records = broker.sent(limit=limit, kind=cast(Kind | None, kind))
        if as_json:
            typer.echo(json.dumps({"records": records}))
            return
        if not records:
            typer.echo("Nothing published from this container yet.")
            return
        for record in records:
            when = datetime.fromtimestamp(float(record["sent_at"]), UTC).strftime("%Y-%m-%d %H:%M")
            target = record["repo"] + (f"#{record['target']}" if record["target"] else "")
            state = "uncertain?" if record["state"] == "uncertain" else record["state"]
            line = (
                f"{when}  {record['outbox']}/{record['kind']}  {target}  {state}  "
                f"{record['manifest']}#{record['index']}  {record['url'] or '-'}"
            )
            typer.echo(line)

    _run(run)


@outbox_app.command("show")
def show(target: TargetArg = None) -> None:
    """Print a staged proposal."""

    def run() -> None:
        kind, name = _target(target)
        typer.echo(broker.show(kind, name)["manifest"])

    _run(run)


@outbox_app.command("drop")
def drop(target: TargetArg = None) -> None:
    """Remove a staged proposal that is not yet published."""

    def run() -> None:
        require_role()
        kind, name = _target(target)
        broker.drop(kind, name)
        typer.echo(f"dropped {kind}/{name}")

    _run(run)


@outbox_app.command("submit")
def submit(name: DraftArg = None, kind: KindOpt = None) -> None:
    """Stage a draft batch for publishing; review drafts need pr review submit."""

    def run() -> None:
        require_role()
        domain, draft = _resolve(name, kind)
        # The broker refuses a review draft here: it needs `jailbee pr review submit`.
        result = broker.draft_submit(domain, draft)
        typer.echo(f"staged {domain}/{result}")

    _run(run)


@outbox_app.command("discard")
def discard(name: DraftArg = None, kind: KindOpt = None) -> None:
    """Discard a draft batch without staging it."""

    def run() -> None:
        require_role()
        domain, draft = _resolve(name, kind)
        broker.draft_discard(domain, draft)
        typer.echo(f"discarded {domain} draft {draft}")

    _run(run)
