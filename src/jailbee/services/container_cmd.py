"""Container service command line."""

from __future__ import annotations

import json
import math
import re
import socket
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer
from typer._click import Context

from jailbee import prompting
from jailbee.services import log_query
from jailbee.services.cli_launch import LaunchCommand, request

if TYPE_CHECKING:
    from jailbee.services.runtime import RunReport, ServiceRuntime

service_app = typer.Typer(invoke_without_command=True)


def _runtime(repo: Path | None) -> ServiceRuntime:
    from jailbee.config.errors import ConfigError
    from jailbee.config.repo_layer import default_repo_config_path, load_repo_layer
    from jailbee.services.local_proc import LocalProc
    from jailbee.services.runtime import ServiceRuntime

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
    from jailbee.services import graph

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
def wait(
    ctx: Context,
    names: Annotated[list[str] | None, typer.Argument()] = None,
    until: Annotated[
        Literal["stopped", "starting", "ready", "failed", "exited", "unhealthy"],
        typer.Option("--until"),
    ] = "ready",
    timeout: Annotated[float, typer.Option("--timeout", min=0)] = 0,
    interval: Annotated[float, typer.Option("--interval", min=1)] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    rt = _runtime(ctx.obj)
    selected = _names(rt, names)
    code, item = rt.wait(selected, until, timeout=timeout, interval=interval)
    if item is not None:
        if json_output:
            typer.echo(json.dumps(asdict(item)))
        else:
            typer.echo(f"{item.name} {item.state} {item.detail}")
    if code:
        raise typer.Exit(code)


@service_app.command(cls=LaunchCommand)
def start(
    ctx: Context,
    names: Annotated[list[str] | None, typer.Argument()] = None,
    env: Annotated[list[str] | None, typer.Option("--env")] = None,
    persist: Annotated[bool, typer.Option("--persist")] = False,
    reset: Annotated[bool, typer.Option("--reset-args")] = False,
) -> None:
    launch = request(ctx, names, env, persist, reset)
    rt = _runtime(ctx.obj)
    try:
        _report(rt.start(_names(rt, names), launch=launch))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@service_app.command()
def stop(ctx: Context, names: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    rt = _runtime(ctx.obj)
    _report(rt.stop(_names(rt, names, destructive=True)))


@service_app.command(cls=LaunchCommand)
def restart(
    ctx: Context,
    names: Annotated[list[str] | None, typer.Argument()] = None,
    env: Annotated[list[str] | None, typer.Option("--env")] = None,
    persist: Annotated[bool, typer.Option("--persist")] = False,
    reset: Annotated[bool, typer.Option("--reset-args")] = False,
) -> None:
    launch = request(ctx, names, env, persist, reset)
    rt = _runtime(ctx.obj)
    try:
        _report(rt.restart(_names(rt, names), launch=launch))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@service_app.command("refresh")
def refresh_cmd(ctx: Context, names: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    from jailbee.services import refresh

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
    from jailbee.services import graph

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
    grep: Annotated[
        list[str] | None,
        typer.Option("--grep", help="Select log lines matching this regex; repeat to combine."),
    ] = None,
    filters: Annotated[
        list[str] | None,
        typer.Option("--filter", help="Use a named service log regex; repeatable."),
    ] = None,
    before: Annotated[
        int, typer.Option("-B", min=0, help="Include this many lines before matches.")
    ] = 0,
    after: Annotated[
        int, typer.Option("-A", min=0, help="Include this many lines after matches.")
    ] = 0,
    context: Annotated[
        int | None, typer.Option("-C", min=0, help="Include this many lines before and after.")
    ] = None,
    wait_pattern: Annotated[
        str | None, typer.Option("--wait", help="Wait for new output matching this regex.")
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Wait timeout in seconds; 0 is unlimited.")
    ] = None,
) -> None:
    if timeout is not None and (wait_pattern is None or not math.isfinite(timeout) or timeout < 0):
        typer.echo("--timeout requires --wait and a finite nonnegative value", err=True)
        raise typer.Exit(2)
    if wait_pattern is not None and (grep or filters or follow):
        typer.echo("--wait cannot be combined with --grep, --filter or --follow", err=True)
        raise typer.Exit(2)
    if context is not None:
        before = after = context
    rt = _runtime(ctx.obj)
    selected = _names(rt, [name] if name else None)[0]
    if follow and (grep or filters or before or after or context is not None):
        typer.echo("--follow cannot be combined with log queries", err=True)
        raise typer.Exit(2)
    expressions = list(grep or [])
    for name in filters or []:
        expression = rt.cfg.services[selected].logs.filters.get(name)
        if expression is None:
            typer.echo(f"unknown log filter: {name}", err=True)
            raise typer.Exit(2)
        expressions.append(expression)
    try:
        patterns = [re.compile(expression) for expression in expressions]
    except re.error as exc:
        typer.echo(f"invalid log regex: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        wait_regex = re.compile(wait_pattern) if wait_pattern is not None else None
    except re.error as exc:
        typer.echo(f"invalid log regex: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        if wait_regex is not None:
            seconds = timeout if timeout is not None else 300
            code, output = rt.wait_log(
                selected, wait_regex, timeout=seconds, before=before, after=after
            )
            if code == 1:
                typer.echo(f"no match for {wait_pattern} within {seconds:g}s", err=True)
            elif code == 3:
                typer.echo("\n".join(output), err=True)
            elif output:
                typer.echo("\n".join(output))
            if code:
                raise typer.Exit(code)
            return
        if not follow:
            if patterns:
                text = log_query.strip_terminal(rt.logs(selected, lines, all_history=True))
                selected_lines = log_query.select_lines(
                    text.splitlines(),
                    patterns,
                    limit=lines,
                    before=before,
                    after=after,
                )
                if selected_lines:
                    typer.echo("\n".join(selected_lines))
            else:
                typer.echo(rt.logs(selected, lines), nl=False)
            return
        for chunk in rt.follow_logs(selected, lines):
            typer.echo(chunk, nl=False)
    except KeyboardInterrupt:
        return
    except typer.Exit:
        raise
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
