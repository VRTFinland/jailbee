"""The small command tree available inside a jailbee container."""

from pathlib import Path
from typing import Annotated

import typer
from typer._click import Context
from typer.core import TyperGroup

from jailbee import __version__, cli_context
from jailbee.container_outbox.issue_cmds import issue_app
from jailbee.container_outbox.outbox_cmds import outbox_app
from jailbee.container_outbox.pr_cmds import pr_app
from jailbee.services.container_cmd import service_app

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


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


container_app = typer.Typer(no_args_is_help=True, cls=ContainerGroup)
config_app = typer.Typer(no_args_is_help=True)
container_app.add_typer(config_app, name="config")
container_app.add_typer(service_app, name="service")
container_app.add_typer(issue_app, name="issue")
container_app.add_typer(pr_app, name="pr")
container_app.add_typer(outbox_app, name="outbox")


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@container_app.callback()
def root(
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True)] = False,
    expect_version: Annotated[str | None, typer.Option("--expect-version", hidden=True)] = None,
) -> None:
    if expect_version is not None:
        _check_version(expect_version)


@container_app.command(
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "help_option_names": [],
    },
    help="Orchestration inbox: wait for, list, read and send messages (see `jailbee inbox --help`)",
)
def inbox(ctx: typer.Context) -> None:
    from jailbee.inbox.cli import main as inbox_main

    raise typer.Exit(inbox_main(ctx.args))


@container_app.command()
def version() -> None:
    typer.echo(__version__)


@config_app.command()
def validate(
    config: Annotated[
        Path | None, typer.Option("--config", help="Path to repository config.yaml.")
    ] = None,
) -> None:
    from jailbee.config_validate_cmd import validate_repo_config

    raise typer.Exit(validate_repo_config(config))
