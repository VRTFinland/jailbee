"""The small command tree available inside a jailbee container."""

from pathlib import Path
from typing import Annotated

import typer

from jailbee import __version__, cli_context

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
container_app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True)
container_app.add_typer(config_app, name="config")


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
        try:
            actual = VERSION_FILE.read_text().strip()
        except OSError:
            actual = "unknown"
        if expect_version != actual:
            typer.echo(f"jailbee version mismatch: {actual}", err=True)
            raise typer.Exit(cli_context.EXIT_SKEW)


@container_app.command()
def version() -> None:
    typer.echo(__version__)


@config_app.command()
def validate(config: Annotated[Path | None, typer.Option("--config")] = None) -> None:
    from jailbee.config_validate_cmd import report_repo_layer

    raise typer.Exit(report_repo_layer(config))
