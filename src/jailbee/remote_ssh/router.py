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


@dataclass(frozen=True)
class _CommandTree:
    """One cached walk of Jailbee's Click command tree, indexed for routing.

    `public_leaves` is exactly what `known_command_paths()` has always
    returned, and stays public-leaf-only: it is still what allowlist config
    validation (`overrides.py`, `global_config.py`) and console
    completion/help read — an alias is deliberately never a valid allowlist
    entry or a completion of its own.

    `aliases` maps a hidden leaf's path (e.g. "merge", "git pr") to the
    public leaf path it is byte-for-byte identical to, keyed on Click
    callback identity (see `_leaf_identity`) rather than name or docstring —
    the only signal that survives a `hidden=True` re-registration of the very
    same function, and the only one an unrelated command that merely *looks*
    like an alias can't spoof. A hidden command with no such public twin
    (`_remote-console`, the `_*-worker` internals, and — despite reading like
    aliases — `submodule checkout`, `claude ...`, `chrome-pool ...`, which
    each warn-then-delegate through a distinct wrapper function rather than
    reusing the public callback) is simply absent from this map and stays
    unknown to every router lookup below.
    """

    public_leaves: frozenset[str]
    aliases: dict[str, str]


def _leaf_identity(command: object) -> int:
    """Identity of a Click command's underlying Python callback.

    Typer wraps every callback in a fresh closure each time it builds a Click
    tree (to marshal parameters — see `typer.main.get_callback`), so
    `command.callback` itself is never the same object across two
    registrations of one function, even two builds of the same registration.
    `get_callback` runs `functools.update_wrapper(wrapper, callback)`, which
    sets `__wrapped__` to the original function — that reference IS stable,
    and is what alias detection compares.
    """
    callback = getattr(command, "callback", None)
    return id(getattr(callback, "__wrapped__", callback))


@functools.cache
def _command_tree() -> _CommandTree:
    """Walk the Click command tree exactly once per process.

    A second, uncached walk here would rebuild the whole Typer/Click tree a
    second time (see the project's test-suite-speed-traps note on
    `typer.main.get_command` rebuilding the tree) — this is the single walk
    every routing lookup below reads from, including `known_command_paths()`.
    """
    from typer.core import TyperCommand, TyperGroup
    from typer.main import get_command

    from jailbee.cli import app

    public_leaves: set[str] = set()
    public_by_identity: dict[int, str] = {}
    hidden_leaves: list[tuple[str, int]] = []

    def walk(
        command: TyperCommand | TyperGroup, prefix: tuple[str, ...], hidden_ancestor: bool
    ) -> None:
        is_hidden = hidden_ancestor or getattr(command, "hidden", False)
        if isinstance(command, TyperGroup):
            for name, child in command.commands.items():
                walk(cast(TyperCommand | TyperGroup, child), (*prefix, name), is_hidden)
            return
        if not prefix:
            return
        path = " ".join(prefix)
        if is_hidden:
            hidden_leaves.append((path, _leaf_identity(command)))
        else:
            public_leaves.add(path)
            public_by_identity[_leaf_identity(command)] = path

    walk(cast(TyperCommand | TyperGroup, get_command(app)), (), False)

    aliases = {
        path: public_by_identity[identity]
        for path, identity in hidden_leaves
        if identity in public_by_identity and public_by_identity[identity] != path
    }
    return _CommandTree(public_leaves=frozenset(public_leaves), aliases=aliases)


def known_command_paths() -> frozenset[str]:
    """Return the public leaf paths in Jailbee's Click command tree."""
    return _command_tree().public_leaves


def command_path(argv: Sequence[str]) -> str:
    """Resolve an argument vector to its longest public or aliased command leaf.

    A hidden alias (its Click callback is byte-identical to some public
    leaf's — see `_CommandTree`) resolves to that public leaf's path, the
    canonical path every policy decision is made against, even though `argv`
    itself keeps whichever spelling the caller actually typed.
    """
    tree = _command_tree()
    candidates = tree.public_leaves | tree.aliases.keys()
    matches = [
        path for path in candidates if tuple(path.split()) == tuple(argv[: len(path.split())])
    ]
    if not matches:
        raise RouteError("unknown Jailbee command")
    longest = max(matches, key=lambda path: len(path.split()))
    return tree.aliases.get(longest, longest)


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
