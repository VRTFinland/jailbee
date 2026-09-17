"""Parse remote SSH requests into restricted Jailbee routes."""

from __future__ import annotations

import functools
import shlex
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from sqlalchemy.engine import Engine
from sqlmodel import Session

from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
from jailbee.db import get_engine
from jailbee.db.models import RegisteredRepo

RouteKind = Literal["help", "dashboard", "console", "command"]


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    argv: tuple[str, ...]
    repo_prefix: str | None
    repo_root: Path | None
    requires_pty: bool


class RouteError(ValueError):
    """A remote request does not match an enabled, permitted route."""


@functools.cache
def known_command_paths() -> frozenset[str]:
    """Return the public leaf paths in Jailbee's Click command tree."""
    from typer.core import TyperCommand, TyperGroup
    from typer.main import get_command

    from jailbee.cli import app

    found: set[str] = set()

    def walk(command: TyperCommand | TyperGroup, prefix: tuple[str, ...]) -> None:
        if getattr(command, "hidden", False):
            return
        if isinstance(command, TyperGroup):
            for name, child in command.commands.items():
                walk(cast(TyperCommand | TyperGroup, child), (*prefix, name))
        elif prefix:
            found.add(" ".join(prefix))

    walk(cast(TyperCommand | TyperGroup, get_command(app)), ())
    return frozenset(found)


def command_path(argv: Sequence[str]) -> str:
    """Resolve an argument vector to its longest public command leaf."""
    matches = [
        path
        for path in known_command_paths()
        if tuple(path.split()) == tuple(argv[: len(path.split())])
    ]
    if not matches:
        raise RouteError("unknown Jailbee command")
    return max(matches, key=lambda path: len(path.split()))


def policy_allows(argv: Sequence[str], policy: RemoteCommandPolicy) -> str:
    """Return the public command path when the remote policy permits it."""
    path = command_path(argv)
    if policy.mode == "disabled":
        raise RouteError("remote Jailbee commands are disabled")
    if policy.mode == "allowlist" and path not in policy.allow:
        raise RouteError(f"Jailbee command is not allowed: {path}")
    return path


def resolve_repo(prefix: str, *, engine: Engine | None = None) -> Path:
    """Resolve an exact registered-repository prefix to an existing directory."""
    with Session(engine or get_engine()) as session:
        row = session.get(RegisteredRepo, prefix)
    if row is None:
        raise RouteError(f"unknown registered repo: {prefix}")
    root = Path(row.repo_root)
    if not root.is_dir():
        raise RouteError(f"registered repo directory is missing: {prefix}")
    return root


def _parse(raw: str) -> tuple[str, ...]:
    if any(unicodedata.category(char) == "Cc" for char in raw):
        raise RouteError("remote command contains a control character")
    try:
        return tuple(shlex.split(raw))
    except ValueError as error:
        raise RouteError(f"cannot parse remote command: {error}") from error


def route(
    raw: str | None,
    config: RemoteSSHConfig,
    *,
    engine: Engine | None = None,
) -> Route:
    """Route one remote SSH command according to the restricted grammar."""
    if raw is None:
        return Route("help", (), None, None, False)

    argv = _parse(raw)
    if not argv:
        return Route("help", (), None, None, False)

    if argv[0] == "dashboard":
        if argv != ("dashboard",):
            raise RouteError("dashboard does not accept remote arguments")
        if not config.dashboard:
            raise RouteError("remote dashboard is disabled")
        return Route("dashboard", ("dashboard", "--registered-only"), None, None, True)

    if argv[0] == "shell":
        if not config.shell:
            raise RouteError("remote shell is disabled")
        prefix: str | None
        root: Path | None
        console_argv: tuple[str, ...]
        if len(argv) == 1:
            prefix = None
            root = None
            console_argv = ("_remote-console",)
        elif len(argv) == 3 and argv[1] == "--repo":
            prefix = argv[2]
            root = resolve_repo(prefix, engine=engine)
            console_argv = ("_remote-console", "--repo", prefix)
        else:
            raise RouteError("remote shell accepts only an optional --repo PREFIX")
        return Route("console", console_argv, prefix, root, True)

    if not config.exec:
        raise RouteError("remote command execution is disabled")
    if len(argv) < 3 or argv[0] != "--repo":
        raise RouteError("remote commands require --repo PREFIX followed by a command")

    prefix = argv[1]
    command_argv = argv[2:]
    policy_allows(command_argv, config.commands)
    root = resolve_repo(prefix, engine=engine)
    return Route("command", command_argv, prefix, root, False)


def help_text(config: RemoteSSHConfig) -> str:
    """Describe only the remote entry points enabled by ``config``."""
    lines = ["Available remote commands:"]
    if config.dashboard:
        lines.append("  dashboard")
    if config.shell:
        lines.append("  shell [--repo PREFIX]")
    if config.exec:
        lines.append("  --repo PREFIX COMMAND [ARGS...]")
    return "\n".join(lines) + "\n"
