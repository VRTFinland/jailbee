"""Container service command line."""

import json
import socket
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import typer
from typer._click import Context

from jailbee import prompting
from jailbee.config.errors import ConfigError
from jailbee.config.repo_layer import default_repo_config_path, load_repo_layer
from jailbee.services import graph, refresh
from jailbee.services.local_proc import LocalProc
from jailbee.services.runtime import RunReport, ServiceRuntime

service_app = typer.Typer(invoke_without_command=True)


def _runtime(repo: Path | None) -> ServiceRuntime:
    path = repo.resolve() / ".jailbee" / "config.yaml" if repo else default_repo_config_path()
    if not path.is_file():
        typer.echo("no .jailbee/config.yaml in this repository", err=True)
        raise typer.Exit(1)
    try:
        cfg = load_repo_layer(path)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    return ServiceRuntime(cfg, path.parent.parent, LocalProc(), Path.home() / ".jailbee/services")


@service_app.callback()
def group(
    ctx: Context,
    repo: Annotated[Path | None, typer.Option("--repo")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    ctx.obj = repo
    if ctx.invoked_subcommand is None:
        _list(_runtime(repo), json_output)


def _list(rt: ServiceRuntime, json_output: bool) -> None:
    statuses = rt.status()
    if json_output:
        typer.echo(json.dumps([asdict(s) for s in statuses]))
    else:
        typer.echo("NAME  STATE  WINDOW  SINCE  DETAIL")
        for s in statuses:
            typer.echo(f"{s.name}  {s.state}  {s.window or '-'}  {s.since or '-'}  {s.detail}")


@service_app.command("ls")
def ls(ctx: Context, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    _list(_runtime(ctx.obj), json_output)


def _names(rt: ServiceRuntime, names: list[str] | None, *, destructive: bool = False) -> list[str]:
    if not names:
        names = [
            prompting.choose_one(
                "service",
                [prompting.Option(n, n, n) for n in rt.cfg.services],
                destructive=destructive,
            )
        ]
    try:
        graph.topo_order(rt.cfg.services, names)
    except graph.UnknownServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    return names


def _report(report: RunReport) -> None:
    if not report.ok:
        typer.echo(report.tail, err=True)
        typer.echo(
            "the service failed to start — if dependencies changed, try "
            f"`jailbee service refresh {report.failed}`",
            err=True,
        )
        raise typer.Exit(1)


@service_app.command()
def start(ctx: Context, names: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    rt = _runtime(ctx.obj)
    _report(rt.start(_names(rt, names)))


@service_app.command()
def stop(ctx: Context, names: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    rt = _runtime(ctx.obj)
    _report(rt.stop(_names(rt, names, destructive=True)))


@service_app.command()
def restart(ctx: Context, names: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    rt = _runtime(ctx.obj)
    _report(rt.restart(_names(rt, names)))


@service_app.command("refresh")
def refresh_cmd(ctx: Context, names: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    rt = _runtime(ctx.obj)
    selected = _names(rt, names)
    refusal = refresh.elevation_refusal(rt.cfg, selected, socket.gethostname())
    if refusal:
        typer.echo(refusal, err=True)
        raise typer.Exit(refresh.EXIT_NEEDS_ELEVATION)
    _report(refresh.run_refresh(rt, rt.cfg, selected))


@service_app.command()
def plan(
    ctx: Context,
    names: Annotated[list[str] | None, typer.Argument()] = None,
    op: Annotated[Literal["start", "restart", "refresh"], typer.Option("--op")] = "start",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    rt = _runtime(ctx.obj)
    try:
        payload = graph.plan_payload(
            rt.cfg.services, names or [], op, step_timeout=rt.cfg.autostart.step_timeout
        )
    except graph.UnknownServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if json_output:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(json.dumps(payload, indent=2))


@service_app.command()
def logs(
    ctx: Context,
    name: Annotated[str | None, typer.Argument()] = None,
    lines: Annotated[int, typer.Option("-n", min=1)] = 30,
    follow: Annotated[bool, typer.Option("--follow")] = False,
) -> None:
    rt = _runtime(ctx.obj)
    selected = _names(rt, [name] if name else None)[0]
    try:
        if not follow:
            typer.echo(rt.logs(selected, lines), nl=False)
            return
        for chunk in rt.follow_logs(selected, lines):
            typer.echo(chunk, nl=False)
    except KeyboardInterrupt:
        return
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
