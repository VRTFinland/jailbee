"""Parse remote SSH requests into restricted Jailbee routes."""

from __future__ import annotations

import functools
import shlex
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from sqlalchemy.engine import Engine
from sqlmodel import Session

from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
from jailbee.db import get_engine
from jailbee.db.models import RegisteredRepo
from jailbee.remote_ssh.session import host_restricted

if TYPE_CHECKING:
    from typer._click.core import Parameter
    from typer.core import TyperCommand

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

    `public_groups` maps a public *group* path (e.g. "git", "net egress") to
    whether invoking it bare — no subcommand, no `--help` — would only print
    help, i.e. whether its Click `invoke_without_command` is false. Every
    group in this codebase leaves that at Click's default (false), but the
    check stays generic instead of assuming so.

    `top_level_names` is every name mounted directly on the root command,
    public or hidden, used only to tell "genuinely unknown command" apart
    from "known but hidden or otherwise disallowed" (see `unknown_command`).

    `public_short_help` maps each public leaf path to its Click short help
    text (`Command.get_short_help_str()`), read by the console's `help` in
    `allowlist` mode to list each allowed command with the same one-line
    description Typer itself would show for it.

    `leaf_commands` maps every routable leaf path — public and alias alike,
    as typed — to its Click command, whose parameters `check_arguments`
    parses a remote argv against.
    """

    public_leaves: frozenset[str]
    aliases: dict[str, str]
    public_groups: dict[str, bool]
    top_level_names: frozenset[str]
    public_short_help: dict[str, str]
    leaf_commands: dict[str, TyperCommand]


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
    public_short_help: dict[str, str] = {}
    hidden_leaves: list[tuple[str, int]] = []
    commands: dict[str, TyperCommand] = {}
    public_groups: dict[str, bool] = {}

    def walk(
        command: TyperCommand | TyperGroup, prefix: tuple[str, ...], hidden_ancestor: bool
    ) -> None:
        is_hidden = hidden_ancestor or getattr(command, "hidden", False)
        if isinstance(command, TyperGroup):
            if prefix and not is_hidden:
                public_groups[" ".join(prefix)] = not getattr(
                    command, "invoke_without_command", False
                )
            for name, child in command.commands.items():
                walk(cast(TyperCommand | TyperGroup, child), (*prefix, name), is_hidden)
            return
        if not prefix:
            return
        path = " ".join(prefix)
        commands[path] = command
        if is_hidden:
            hidden_leaves.append((path, _leaf_identity(command)))
        else:
            public_leaves.add(path)
            public_by_identity[_leaf_identity(command)] = path
            public_short_help[path] = command.get_short_help_str()

    root = cast(TyperCommand | TyperGroup, get_command(app))
    walk(root, (), False)

    aliases = {
        path: public_by_identity[identity]
        for path, identity in hidden_leaves
        if identity in public_by_identity and public_by_identity[identity] != path
    }
    top_level_names = frozenset(cast(TyperGroup, root).commands)
    return _CommandTree(
        public_leaves=frozenset(public_leaves),
        aliases=aliases,
        public_groups=public_groups,
        top_level_names=top_level_names,
        public_short_help=public_short_help,
        leaf_commands={
            path: commands[path] for path in (*public_leaves, *aliases) if path in commands
        },
    )


def known_command_paths() -> frozenset[str]:
    """Return the public leaf paths in Jailbee's Click command tree."""
    return _command_tree().public_leaves


def known_command_short_help() -> dict[str, str]:
    """Return each public command path's Click short help text."""
    return _command_tree().public_short_help


def _resolve_leaf(argv: Sequence[str]) -> tuple[str, str]:
    """(typed, canonical) paths of the longest public or aliased leaf `argv` names."""
    tree = _command_tree()
    candidates = tree.public_leaves | tree.aliases.keys()
    matches = [
        path for path in candidates if tuple(path.split()) == tuple(argv[: len(path.split())])
    ]
    if not matches:
        raise RouteError("unknown Jailbee command")
    longest = max(matches, key=lambda path: len(path.split()))
    return longest, tree.aliases.get(longest, longest)


def command_path(argv: Sequence[str]) -> str:
    """Resolve an argument vector to its longest public or aliased command leaf.

    A hidden alias (its Click callback is byte-identical to some public
    leaf's — see `_CommandTree`) resolves to that public leaf's path, the
    canonical path every policy decision is made against, even though `argv`
    itself keeps whichever spelling the caller actually typed.
    """
    return _resolve_leaf(argv)[1]


# Commands a restricted remote session may never run, whatever `commands.mode`
# says — `full` included. An entry names a leaf or a whole group. Each one
# reaches past the containers and the git bridge into the host itself:
#   - host configuration and the SSH service itself: `config edit`/`init`
#     (a config decides host mounts and this very policy), `remote ...`;
#   - host installation and host-level infrastructure: `setup`, `init`,
#     `apply`, `base build`/`prune`, `net install`/`refresh`/`unregister`,
#     `registry up`/`down`;
#   - persistent network policy: `net egress add`/`rm` accept any address,
#     the host's own and its LAN's included;
#   - host credentials shared by every container: `account` writes;
#   - a host path or service brought into a container: `mount` (an
#     `optional_mounts` entry), `port to-container`;
#   - windows on the host's display: `gui`, `ide`, the browsers, `apps run`.
# `tests/test_remote_ssh_router.py` partitions every public leaf between this
# set and the container-side rest, so a new command fails the suite until
# someone decides which side it is on.
_HOST_COMMANDS: frozenset[str] = frozenset(
    {
        "config edit",
        "config init",
        "remote",
        "setup",
        "init",
        "apply",
        "base build",
        "base prune",
        "net install",
        "net refresh",
        "net unregister",
        "net egress add",
        "net egress rm",
        "registry up",
        "registry down",
        "account use",
        "account park",
        "account rm",
        "account group create",
        "account group rm",
        "account group set",
        "account group unset",
        "account group use",
        "account group reset",
        "mount",
        "port to-container",
        "gui",
        "ide",
        "chrome",
        "firefox",
        "browser",
        "apps run",
    }
)


def is_host_command(path: str) -> bool:
    """True when canonical `path` is, or lies under, a `_HOST_COMMANDS` entry."""
    return any(path == entry or path.startswith(entry + " ") for entry in _HOST_COMMANDS)


# Parameters a remote caller may never set, by canonical command path, on top
# of every path-typed one (see `_host_reaching_params`). Each reaches the host
# in a way no argument type reveals:
#   - `new --mount` bind-mounts the host repo read-write, `.git` included, so
#     the container could plant a hook or `core.fsmonitor` that the host's own
#     git later runs.
_REMOTE_DENIED_PARAMS: dict[str, frozenset[str]] = {
    "new": frozenset({"mount"}),
}


def _host_reaching_params(command: TyperCommand, canonical: str) -> list[Parameter]:
    """The parameters of `command` a remote argv must leave at their default.

    Every path-typed parameter qualifies without being listed: a host path
    chosen by the SSH client is a host file read (`--config` reports what it
    could not parse, contents included) or a host directory treated as a repo
    (a config it points at decides host mounts). Typer builds each `Path`
    annotation as a `TyperPath`, and a file argument as Click's `File`.
    """
    from typer._click.types import File
    from typer.models import TyperPath

    denied = _REMOTE_DENIED_PARAMS.get(canonical, frozenset())
    return [
        param
        for param in command.params
        if isinstance(param.type, TyperPath | File) or param.name in denied
    ]


def check_arguments(argv: Sequence[str]) -> None:
    """Refuse a remote argv that sets a host-reaching parameter of its leaf.

    The argv is parsed by the leaf's own Click command, exactly as the real
    invocation will parse it, and only the resulting parameter sources are
    consulted. No token scan could be trusted with this: `-c` hides inside a
    short-option cluster, `--config=x` carries its value, and a `--` taken
    as an option's value leaves the options after it live. A parse this
    cannot complete is refused — the real one would fail too.
    """
    from typer._click.core import ParameterSource

    typed, canonical = _resolve_leaf(argv)
    command = _command_tree().leaf_commands[typed]
    params = _host_reaching_params(command, canonical)
    if not params:
        return
    words = typed.split()
    try:
        ctx = command.make_context(words[-1], list(argv[len(words) :]), resilient_parsing=True)
    except Exception as error:
        raise RouteError(f"cannot parse remote command arguments: {canonical}") from error
    with ctx:
        for param in params:
            if param.name is None:
                continue
            if ctx.get_parameter_source(param.name) is ParameterSource.COMMANDLINE:
                shown = param.opts[0] if param.opts else param.name
                raise RouteError(f"remote Jailbee commands may not set {shown}: {canonical}")


def _help_only_path(argv: Sequence[str]) -> str | None:
    """The public group path (or "" for top-level) a pure help invocation names.

    `None` for anything else, including a bare group path whose group would
    actually run an action (`invoke_without_command`) rather than just print
    help — only that group's path *plus* `--help`/`-h` counts then, since
    Click's own `--help` handling always short-circuits before a callback
    runs. Options ahead of the command path are still rejected exactly like
    `command_path`: an unrecognized prefix here simply fails to match a known
    group path and this returns `None`, falling through to the same
    "unknown Jailbee command" `command_path` already raises for those.
    """
    words = tuple(argv)
    if not words:
        return None
    if words in (("--help",), ("-h",)):
        return ""
    trailing_help = words[-1] in ("--help", "-h")
    group_words = words[:-1] if trailing_help else words
    if not group_words:
        return None
    path = " ".join(group_words)
    bare_is_help = _command_tree().public_groups.get(path)
    if bare_is_help is None:
        return None
    return path if (trailing_help or bare_is_help) else None


def unknown_command(argv: Sequence[str], policy: RemoteCommandPolicy) -> bool:
    """True when argv's first token names no command anywhere in the tree.

    Used to hand a request straight through to `python -m jailbee` instead of
    rejecting it here, so Typer prints its own "No such command" error (with
    suggestions) — see `console.run` and `route`. A leading option other than
    the `--help`/`-h` `_help_only_path` already handles is left to the
    existing rejection path, and so is a genuinely hidden command with no
    public alias: this only catches a name that matches nothing at all, at
    any visibility. Always false in `disabled` mode, where every command is
    rejected the same way regardless of what it names.
    """
    if policy.mode == "disabled" or not argv:
        return False
    token = argv[0]
    if token.startswith("-"):
        return False
    return token not in _command_tree().top_level_names


def policy_allows(
    argv: Sequence[str], policy: RemoteCommandPolicy, *, restrict_host: bool = True
) -> str:
    """Return the public command path when the remote policy permits it.

    A pure help invocation (see `_help_only_path`) is checked directly rather
    than through `command_path`, since it never resolves to a leaf: `full`
    permits it outright; `allowlist` permits it only when some allowed leaf
    lies under that group, or — for the bare top-level `--help`/`-h` — only
    when the allowlist is non-empty at all.

    A permitted command is then held, in every mode `full` included, to
    `is_host_command` and `check_arguments`: the policy names which commands
    a remote caller may run, never that they may manage the host or hand a
    command a host path. Only
    `remote.ssh.restrict_host: false` (``restrict_host``) skips it, and not
    even that inside an already restricted session (`host_restricted`).
    """
    if policy.mode == "disabled":
        raise RouteError("remote Jailbee commands are disabled")
    help_path = _help_only_path(argv)
    if help_path is not None:
        if policy.mode == "allowlist":
            allowed = (
                bool(policy.allow)
                if help_path == ""
                else any(
                    leaf == help_path or leaf.startswith(help_path + " ") for leaf in policy.allow
                )
            )
            if not allowed:
                raise RouteError(f"Jailbee command is not allowed: {help_path or '--help'}")
        return help_path
    path = command_path(argv)
    if policy.mode == "allowlist" and path not in policy.allow:
        raise RouteError(f"Jailbee command is not allowed: {path}")
    if host_restricted(restrict_host):
        if is_host_command(path):
            raise RouteError(
                f"`{path}` manages the host itself, which a restricted remote session never does"
            )
        check_arguments(argv)
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
        return Route("dashboard", ("dashboard",), None, None, True)

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
    # A genuinely unknown command name is handed straight through so
    # `python -m jailbee` reports it, rather than this router inventing its
    # own "unknown Jailbee command" — see `unknown_command`.
    if not unknown_command(command_argv, config.commands):
        policy_allows(command_argv, config.commands, restrict_host=config.restrict_host)
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
