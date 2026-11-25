"""The small command tree available inside a jailbee container."""

from pathlib import Path
from typing import Annotated

import typer
from typer._click import Command, Context
from typer.core import TyperGroup

from jailbee import __version__, cli_context
from jailbee.container_outbox.issue_cmds import issue_app
from jailbee.container_outbox.outbox_cmds import outbox_app
from jailbee.container_outbox.pr_cmds import pr_app
from jailbee.inbox.container_cmd import inbox_app
from jailbee.services.container_cmd import service_app
from jailbee.version_info import display_version

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

# Explicit host-only branches: unknown names must still be reported as typos.
HOST_SUBCOMMANDS = {
    "agent": frozenset({"use", "show", "open"}),
    "config": frozenset({"show", "migrate", "edit", "init"}),
    "pr": frozenset({"push", "bind", "unbind", "ls", "open", "fetch"}),
    "issue": frozenset({"apply", "resolve"}),
    # `outbox sent` exists on both sides: the container lists only its own records.
    # `outbox drop` is host-only; the container verb is `outbox discard`.
    "outbox": frozenset(
        {"apply", "browse", "clear", "attachment-resolve", "attachments", "drop", "resolve"}
    ),
}


class SharedGroup(TyperGroup):
    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        if cmd_name in HOST_SUBCOMMANDS.get(ctx.info_name or "", frozenset()):
            if (ctx.info_name, cmd_name) == ("outbox", "drop"):
                typer.echo(
                    "`jailbee outbox drop` is gone here: use `jailbee outbox discard "
                    "issue/NAME` (or pr/NAME), or `outbox discard --draft NAME` for a draft.",
                    err=True,
                )
                raise typer.Exit(2)
            typer.echo(
                f"`jailbee {ctx.info_name} {cmd_name}` runs on the host, not inside a container.",
                err=True,
            )
            raise typer.Exit(2)
        return super().get_command(ctx, cmd_name)


class ContainerGroup(TyperGroup):
    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        # Click handles eager help/version before the root callback, regardless
        # of option order. Check the root handshake before parsing either.
        for index, arg in enumerate(args):
            if arg == "--" or not arg.startswith("-"):
                break
            if arg == "--expect-version" and index + 1 < len(args):
                _check_version(args[index + 1])
                break
            if arg.startswith("--expect-version="):
                _check_version(arg.partition("=")[2])
                break
        return super().parse_args(ctx, args)


def _check_version(expected: str) -> None:
    try:
        actual = VERSION_FILE.read_text().strip()
    except OSError:
        actual = "unknown"
    if expected != actual:
        typer.echo(f"jailbee version mismatch: {actual}", err=True)
        raise typer.Exit(cli_context.EXIT_SKEW)


container_app = typer.Typer(
    no_args_is_help=True,
    cls=ContainerGroup,
    help=(
        "JailBee inside a container. Issue, PR and outbox writes are staged for the host "
        "operator to publish (gh here is read-only); other management runs on the host."
    ),
)
agent_app = typer.Typer(no_args_is_help=True, help="Read live agent model guidance from the host.")
container_app.add_typer(agent_app, name="agent", cls=SharedGroup)
host_app = typer.Typer(no_args_is_help=True, help="Read live host state over the broker socket.")
container_app.add_typer(host_app, name="host")
config_app = typer.Typer(no_args_is_help=True, help="Check the repository configuration.")
container_app.add_typer(config_app, name="config", cls=SharedGroup)
container_app.add_typer(
    service_app, name="service", help="List, start, stop, refresh and read logs of services."
)
container_app.add_typer(
    inbox_app, name="inbox", help="Wait for, list, read and send orchestration messages."
)
container_app.add_typer(
    issue_app,
    name="issue",
    cls=SharedGroup,
    help="Stage issue changes in the outbox for the host operator to publish.",
)
container_app.add_typer(
    pr_app,
    name="pr",
    cls=SharedGroup,
    help="Stage PR comments, reviews and descriptions for the host operator to publish.",
)
container_app.add_typer(
    outbox_app,
    name="outbox",
    cls=SharedGroup,
    help="Inspect, submit and discard staged issue and PR proposals.",
)


def _version(value: bool) -> None:
    if value:
        typer.echo(display_version(__version__))
        raise typer.Exit()


@container_app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Print the version and exit."
        ),
    ] = False,
    expect_version: Annotated[str | None, typer.Option("--expect-version", hidden=True)] = None,
) -> None:
    """Commands available inside a JailBee container."""
    if expect_version is not None:
        _check_version(expect_version)


@container_app.command()
def version() -> None:
    """Print the JailBee version."""
    typer.echo(display_version(__version__))


@agent_app.command()
def models(
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """Show the effective model map and model selection policy for this repo."""
    from jailbee.agent_models import models as agent_models

    raise typer.Exit(agent_models(as_json=as_json))


@host_app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """Show current host load, memory, storage space and running container count."""
    from jailbee.host_status import status as host_status

    raise typer.Exit(host_status(as_json=as_json))


@config_app.command()
def validate(
    config: Annotated[
        Path | None, typer.Option("--config", help="Path to repository config.yaml.")
    ] = None,
) -> None:
    """Validate the repository's .jailbee/config.yaml; exit nonzero on errors."""
    from jailbee.config_validate_cmd import validate_repo_config

    raise typer.Exit(validate_repo_config(config))
