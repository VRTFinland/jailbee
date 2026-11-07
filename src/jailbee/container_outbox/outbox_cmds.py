"""Browse and manage container outbox proposals and batches."""

import json
from datetime import UTC, datetime
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
