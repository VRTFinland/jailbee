"""Focused unified outbox CLI; business logic is imported only on invocation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from typer import _click as click
from typer.core import TyperGroup

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.outbox.models import ProposalId
    from jailbee.outbox_io import JournalStore

_COMMANDS = frozenset(("browse", "ls", "show", "drop", "apply"))


def _normalize_local(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    index = 0
    while index < len(args):
        word = args[index]
        if word in ("--help", "-h"):
            return args
        if word in ("--config", "-c"):
            index += 2
        elif word.startswith("--config=") or (word.startswith("-c") and len(word) > 2):
            index += 1
        else:
            break
    if index == len(args) or (
        index < len(args) and args[index] not in _COMMANDS and not args[index].startswith("-")
    ):
        args.insert(index, "browse")
    return args


def normalize_outbox_argv(argv: Sequence[str]) -> list[str]:
    """Normalize the outbox segment, preserving option values and other argv."""
    args = list(argv)
    index = 0
    while index < len(args):
        word = args[index]
        if word in ("--config", "-c"):
            index += 2
            continue
        if word == "outbox":
            return args[: index + 1] + _normalize_local(args[index + 1 :])
        if not word.startswith("-"):
            return args
        index += 1
    return args


class OutboxGroup(TyperGroup):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        return super().parse_args(ctx, _normalize_local(args))


app = typer.Typer(
    name="outbox",
    cls=OutboxGroup,
    help=(
        "Inspect and manage staged proposals. Shorthand: outbox [container]. "
        "Use outbox browse ls for a container named ls."
    ),
)
ConfigOption = Annotated[
    Path | None, typer.Option("--config", "-c", help="Repository config file.")
]
ContainerArgument = Annotated[
    str | None, typer.Argument(help="Full or short container name. Asked for when omitted.")
]
ProposalArgument = Annotated[
    str | None,
    typer.Argument(
        help="Proposal: pr/<manifest>.json or issue/<manifest>.json. Asked for when omitted."
    ),
]
RevisionOption = Annotated[
    str | None,
    typer.Option("--revision", help="Refuse changes since this inspected revision token."),
]
YesOption = Annotated[
    bool, typer.Option("--yes", "-y", help="Confirm only; never bypass safety checks.")
]


class Output(StrEnum):
    table = "table"
    json = "json"


OutputOption = Annotated[
    Output, typer.Option("--format", "--output", "-o", help="Output format: table or json.")
]


@app.callback()
def group(ctx: typer.Context, config: ConfigOption = None) -> None:
    ctx.obj = config


def _run(
    ctx: typer.Context, config: Path | None, operation: Callable[[Config, Incus], int]
) -> None:
    from jailbee.config import ConfigError, load_config, load_repo_config
    from jailbee.incus import Incus, IncusError
    from jailbee.outbox.inspect import safe_text
    from jailbee.outbox.models import OutboxError, OutboxExecutionError
    from jailbee.remote_ssh.repo_scope import RepoScopeError
    from jailbee.tui import error_plain

    try:
        path = config if config is not None else ctx.obj
        cfg = load_config(path) if path is not None else load_repo_config(Path.cwd())
        status = operation(cfg, Incus())
    except (ConfigError, OutboxError, RepoScopeError) as exc:
        error_plain(safe_text(str(exc)))
        raise typer.Exit(2) from exc
    except (OutboxExecutionError, IncusError, OSError) as exc:
        error_plain(safe_text(str(exc)))
        raise typer.Exit(1) from exc
    if status:
        raise typer.Exit(status)


def _pick_target(
    cfg: Config,
    incus: Incus,
    container: str | None,
    proposal: str | None,
    *,
    journal_store: JournalStore,
    destructive: bool,
) -> tuple[str, ProposalId]:
    """The container and proposal a command acts on, asking for what is missing."""
    from jailbee import prompting
    from jailbee.outbox.commands import discover
    from jailbee.outbox.models import ProposalId

    if container is not None and proposal is not None:
        return container, ProposalId.parse(proposal)
    views = [
        v
        for v in discover(cfg, incus, container, all_repos=False, journal_store=journal_store)
        if v.proposals
    ]
    if container is not None:
        # Named, so nothing to ask: `discover` already resolved it to one container.
        if not views:
            raise prompting.MissingValue("proposal", reason=f"no pending proposals in {container}")
        view = views[0]
    else:
        chosen = prompting.choose_one(
            "container",
            [prompting.Option(v.name, v.name, v.name) for v in views],
            destructive=destructive,
            empty_reason="no container has pending proposals",
        )
        view = next(v for v in views if v.name == chosen)
    if proposal is not None:
        return view.name, ProposalId.parse(proposal)
    pid = prompting.choose_one(
        "proposal",
        [prompting.Option(p.id, f"{p.id}  {p.state}", str(p.id)) for p in view.proposals],
        destructive=destructive,
        empty_reason=f"no pending proposals in {view.name}",
    )
    return view.name, pid


def browser_read_only() -> bool:
    """Browser policy for UI wiring: never supply mutation callbacks over SSH.

    This includes unrestricted SSH sessions. Mutations must use explicit
    drop/apply commands so the router checks each command's permissions.
    """
    from jailbee.remote_ssh.session import is_ssh_session

    return is_ssh_session()


@app.command()
def browse(
    ctx: typer.Context,
    container: Annotated[str | None, typer.Argument(help="Full or short container name.")] = None,
    config: ConfigOption = None,
) -> None:
    """Overview (also off-TTY). Unambiguous spelling for subcommand-name containers."""
    from jailbee import prompting
    from jailbee.outbox.browser import BrowserActions, run_browser
    from jailbee.outbox.commands import (
        apply_selected,
        discover,
        drop_selected,
        show_overview,
    )
    from jailbee.outbox.delete import DeletePlan
    from jailbee.outbox.markdown_view import print_lines
    from jailbee.outbox.models import OutboxChanged
    from jailbee.outbox.publish import PublishOptions
    from jailbee.outbox_io import JournalStore

    def operation(cfg: Config, incus: Incus) -> int:
        journals = JournalStore()
        read_only = browser_read_only()
        if read_only:
            print_lines(
                (
                    "Outbox browser is read-only over SSH. Use explicit outbox drop or "
                    "outbox apply commands when permitted by the remote command policy.",
                )
            )
        if not prompting.is_interactive():
            return show_overview(
                cfg, incus, container, all_repos=False, output="table", journal_store=journals
            )

        def choose(message: str, options: Sequence[tuple[str, str]]) -> str | None:
            import questionary

            result = questionary.select(
                message,
                choices=[questionary.Choice(title=label, value=key) for key, label in options],
            ).ask()
            return result if isinstance(result, str) else None

        def delete(name: str, plan: DeletePlan) -> tuple[str, ...]:
            def exact_scope(fresh: DeletePlan) -> bool:
                if fresh != plan:
                    raise OutboxChanged("deletion scope changed; refresh required")
                return True

            drop_selected(
                cfg,
                incus,
                name,
                plan.proposal,
                selection=plan.selection,
                journal_store=journals,
                confirm=exact_scope,
                expected_revision=plan.expected_revision,
            )
            return ("Deletion completed.",)

        def publish(name: str, proposal: ProposalId, revision: str) -> int:
            return apply_selected(
                cfg,
                incus,
                name,
                proposal,
                options=PublishOptions(),
                journal_store=journals,
                confirm=lambda total: typer.confirm(
                    f"Publish {total} pending actions in the whole manifest?", default=False
                ),
                expected_revision=revision,
            )

        return run_browser(
            BrowserActions(
                load=lambda name: discover(
                    cfg, incus, name, all_repos=False, journal_store=journals
                ),
                delete=None if read_only else delete,
                publish=None if read_only else publish,
                confirm=lambda message: typer.confirm(message, default=False),
                choose=choose,
                show=lambda text: print_lines((text,)),
            ),
            container,
        )

    _run(ctx, config, operation)


@app.command("ls")
def list_cmd(
    ctx: typer.Context,
    container: Annotated[str | None, typer.Argument(help="Full or short container name.")] = None,
    all_repos: Annotated[
        bool, typer.Option("--all-repos", help="Include registered repositories.")
    ] = False,
    output: OutputOption = Output.table,
    config: ConfigOption = None,
) -> None:
    """List proposals, including structured unavailable containers."""
    from jailbee.outbox.commands import show_overview
    from jailbee.outbox_io import JournalStore

    _run(
        ctx,
        config,
        lambda cfg, incus: show_overview(
            cfg,
            incus,
            container,
            all_repos=all_repos,
            output=output.value,
            journal_store=JournalStore(),
        ),
    )


@app.command()
def show(
    ctx: typer.Context,
    container: ContainerArgument = None,
    proposal: ProposalArgument = None,
    output: OutputOption = Output.table,
    config: ConfigOption = None,
) -> None:
    """Inspect a complete proposal with zero-based action and comment indices."""
    from jailbee.outbox.commands import show_selected
    from jailbee.outbox_io import JournalStore

    def operation(cfg: Config, incus: Incus) -> int:
        store = JournalStore()
        name, pid = _pick_target(
            cfg, incus, container, proposal, journal_store=store, destructive=False
        )
        return show_selected(cfg, incus, name, pid, output=output.value, journal_store=store)

    _run(ctx, config, operation)


@app.command()
def drop(
    ctx: typer.Context,
    container: ContainerArgument = None,
    proposal: ProposalArgument = None,
    action: Annotated[int | None, typer.Option("--action", help="Zero-based action index.")] = None,
    comment: Annotated[
        int | None,
        typer.Option("--comment", help="Zero-based inline comment index; requires --action."),
    ] = None,
    with_dependents: Annotated[
        bool, typer.Option("--with-dependents", help="Include dependent issue-create actions.")
    ] = False,
    archive_journal: Annotated[
        bool,
        typer.Option("--archive-journal", help="Archive settled journal on whole issue deletion."),
    ] = False,
    yes: YesOption = False,
    revision: RevisionOption = None,
    config: ConfigOption = None,
) -> None:
    """Delete locally after displaying exact scope; selectors are zero-based."""
    from jailbee.outbox.commands import drop_selected
    from jailbee.outbox.delete import DeletePlan, DeleteSelection
    from jailbee.outbox.markdown_view import print_lines
    from jailbee.outbox_io import JournalStore

    def confirm(plan: DeletePlan) -> bool:
        print_lines(plan.summary)
        accepted = yes or typer.confirm("Delete this exact scope?", default=False)
        if not accepted:
            print_lines(("Nothing deleted.",))
        return accepted

    def operation(cfg: Config, incus: Incus) -> int:
        store = JournalStore()
        name, pid = _pick_target(
            cfg, incus, container, proposal, journal_store=store, destructive=True
        )
        return drop_selected(
            cfg,
            incus,
            name,
            pid,
            selection=DeleteSelection(action, comment, with_dependents, archive_journal),
            journal_store=store,
            confirm=confirm,
            expected_revision=revision,
        )

    _run(ctx, config, operation)


@app.command()
def apply(
    ctx: typer.Context,
    container: ContainerArgument = None,
    proposal: ProposalArgument = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show plan without publishing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="PR stale-anchor override only; invalid for issues.")
    ] = False,
    yes: YesOption = False,
    revision: RevisionOption = None,
    config: ConfigOption = None,
) -> None:
    """Publish one whole manifest through its existing domain gates."""
    from jailbee.outbox.commands import apply_selected
    from jailbee.outbox.publish import PublishOptions
    from jailbee.outbox_io import JournalStore

    def operation(cfg: Config, incus: Incus) -> int:
        store = JournalStore()
        name, pid = _pick_target(
            cfg, incus, container, proposal, journal_store=store, destructive=True
        )
        return apply_selected(
            cfg,
            incus,
            name,
            pid,
            options=PublishOptions(dry_run, force),
            journal_store=store,
            confirm=lambda total: (
                yes or typer.confirm(f"Publish {total} pending actions?", default=False)
            ),
            expected_revision=revision,
        )

    _run(ctx, config, operation)
