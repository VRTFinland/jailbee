"""Browse and manage container outbox proposals and batches."""

import json
from pathlib import Path
from typing import Annotated, cast

import typer

from jailbee.container_outbox import broker
from jailbee.container_outbox.issue_cmds import _guard, _run
from jailbee.container_outbox.store import Kind, OutboxCommandError, Store

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


def _resolve(store: Store, name: str | None, kind: str | None) -> tuple[Kind, str]:
    if not name:
        raise OutboxCommandError("missing NAME", 2)
    if kind is not None and kind not in ("issue", "pr"):
        raise OutboxCommandError("kind must be issue or pr", 2)
    kinds: tuple[Kind, ...] = (cast(Kind, kind),) if kind else ("issue", "pr")
    found = [item for item in kinds if store.draft_load_state(item, name) is not None]
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
        store = Store(Path.home())
        data: dict[str, list[dict[str, object]]] = {"issue": [], "pr": [], "drafts": []}
        # Issue proposals live in the host store; PR proposals are still files here.
        data["issue"] = [
            {"name": row["name"], "summary": row["summary"]}
            for row in broker.list_proposals("issue")
        ]
        for entry in store.list():
            if entry.kind == "pr":
                data["pr"].append({"name": entry.name, "summary": entry.summary})
        data["drafts"] = store.list_drafts()
        if as_json:
            typer.echo(json.dumps(data))
        else:
            for kind in ("issue", "pr"):
                for item in data[kind]:
                    typer.echo(f"{kind}/{item['name']}  {item['summary']}")
            for draft in data["drafts"]:
                typer.echo(f"draft {draft['kind']}/{draft['name']}  {draft['actions']} actions")

    _run(run)


@outbox_app.command("show")
def show(target: TargetArg = None) -> None:
    """Print a staged proposal."""

    def run() -> None:
        kind, name = _target(target)
        if kind == "issue":
            typer.echo(broker.show(kind, name)["manifest"])
        else:
            typer.echo(Store(Path.home()).show(kind, name))

    _run(run)


@outbox_app.command("drop")
def drop(target: TargetArg = None) -> None:
    """Remove a staged proposal that is not yet published."""

    def run() -> None:
        store = _guard()
        kind, name = _target(target)
        store.drop(kind, name)
        typer.echo(f"dropped {kind}/{name}")

    _run(run)


def _not_review(
    manifest: dict[str, object], bodies: dict[str, str]
) -> tuple[dict[str, object], dict[str, str]]:
    actions = manifest.get("actions")
    if isinstance(actions, list) and any(
        isinstance(action, dict) and action.get("type") == "review" for action in actions
    ):
        raise OutboxCommandError(
            "review drafts require fresh PR metadata; use jailbee pr review submit PR", 2
        )
    return manifest, bodies


@outbox_app.command("submit")
def submit(name: DraftArg = None, kind: KindOpt = None) -> None:
    """Stage a draft batch for publishing; review drafts need pr review submit."""

    def run() -> None:
        store = _guard()
        domain, draft = _resolve(store, name, kind)
        result = store.draft_submit(
            domain,
            draft,
            draft,
            prepare=_not_review if domain == "pr" else None,
            write=broker.put if domain == "issue" else None,
        )
        typer.echo(f"staged {domain}/{result}")

    _run(run)


@outbox_app.command("discard")
def discard(name: DraftArg = None, kind: KindOpt = None) -> None:
    """Discard a draft batch without staging it."""

    def run() -> None:
        store = _guard()
        domain, draft = _resolve(store, name, kind)
        store.draft_discard(domain, draft)
        typer.echo(f"discarded {domain} draft {draft}")

    _run(run)
