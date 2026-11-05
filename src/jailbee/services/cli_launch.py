"""Shared parsing for service launch options."""

import re

import typer
from typer.core import TyperCommand
from typer._click import Context

from jailbee.services.launch_store import LaunchRequest


class LaunchCommand(TyperCommand):
    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        # An option's value can itself be "--"; it is not a separator.
        skip = False
        for index, value in enumerate(args):
            if skip:
                skip = False
            elif value in ("--env", "--config"):
                skip = True
            elif value == "--":
                ctx.meta["service_launch_args"] = args[index + 1:]
                args = args[:index]
                break
        return super().parse_args(ctx, args)


def request(ctx: Context, names: list[str] | None, env: list[str] | None, persist: bool, reset: bool) -> LaunchRequest | None:
    args = ctx.meta.get("service_launch_args")
    if args is None and not env and not persist and not reset:
        return None
    if len(names or []) != 1:
        typer.echo("runtime options apply to exactly one service", err=True)
        raise typer.Exit(2)
    values: dict[str, str] = {}
    for entry in env or []:
        key, sep, value = entry.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            typer.echo("invalid --env; expected KEY=VALUE", err=True)
            raise typer.Exit(2)
        values[key] = value
    return LaunchRequest(tuple(args) if args is not None else None, values, persist, reset)


def extra(request: LaunchRequest | None) -> list[str]:
    if request is None:
        return []
    result = [item for key, value in request.env.items() for item in ("--env", f"{key}={value}")]
    if request.persist:
        result.append("--persist")
    if request.reset:
        result.append("--reset-args")
    if request.args is not None:
        result.extend(("--", *request.args))
    return result
