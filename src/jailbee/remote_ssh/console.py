"""Restricted interactive Jailbee console for remote SSH sessions."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import typer.rich_utils as typer_rich_utils
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.keys import Keys
from pydantic import ValidationError
from rich.console import Console as RichConsole
from rich.panel import Panel
from rich.table import Table
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.db import get_engine, state_dir
from jailbee.db.models import RegisteredRepo
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.remote_ssh.router import (
    RouteError,
    known_command_paths,
    known_command_short_help,
    policy_allows,
    resolve_repo,
    unknown_command,
)

if TYPE_CHECKING:
    from jailbee.config.models_remote import RemoteCommandPolicy

_LOCAL_COMMANDS = ("dashboard", "exit", "help", "repos", "use")

# Distinguishes an intentional cancel (Esc/Ctrl-D) from every real answer in
# `_select_repo`, mirroring the `questionary.Choice(value=None)` trap noted
# there: only this object, never `None` alone, means "no selection" once the
# two extra key bindings are added, since Ctrl-C already answers `None`.
_CANCELLED = object()


@dataclass(frozen=True)
class RepoChoice:
    prefix: str
    root: Path


def registered_repos(*, engine: Engine | None = None) -> list[RepoChoice]:
    """Return registered repositories whose host directories still exist."""
    with Session(engine or get_engine()) as session:
        rows = session.exec(select(RegisteredRepo)).all()
    return sorted(
        [
            RepoChoice(row.container_prefix, Path(row.repo_root))
            for row in rows
            if Path(row.repo_root).is_dir()
        ],
        key=lambda repo: repo.prefix,
    )


def parse_console_line(raw: str) -> tuple[str, ...]:
    """Split one console line into argv without executing shell syntax."""
    try:
        return tuple(shlex.split(raw))
    except ValueError as error:
        raise RouteError(f"cannot parse console command: {error}") from error


def _print_repos(repos: Sequence[RepoChoice]) -> None:
    for repo in repos:
        print(f"{repo.prefix}\t{repo.root}")


def _console_command_rows(*, dashboard_enabled: bool) -> list[tuple[str, str]]:
    """The console's own local commands, in the order `help` lists them."""
    rows = [
        ("repos", "list registered repositories"),
        ("use [PREFIX]", "switch repository (menu when PREFIX is omitted)"),
    ]
    if dashboard_enabled:
        rows.append(("dashboard", "open the registered-repository dashboard"))
    rows.append(("help", "show this help"))
    rows.append(("exit", "leave the console"))
    return rows


def _render_command_panel(
    rich_console: RichConsole, title: str, rows: Sequence[tuple[str, str]]
) -> None:
    """Render one command/description panel styled like Typer's own help panels.

    Reuses `typer.rich_utils`'s own style constants (title alignment, panel
    border, first-column color, help-text style) instead of hardcoding
    equivalents, so this tracks Typer's look if it ever changes. `title` may
    carry Rich markup (`Panel` parses a `str` title with `Text.from_markup`).
    """
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
    table.add_column(style=typer_rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN, no_wrap=True)
    table.add_column(style=typer_rich_utils.STYLE_OPTION_HELP)
    for name, description in rows:
        table.add_row(name, description)
    rich_console.print(
        Panel(
            table,
            title=title,
            title_align=typer_rich_utils.ALIGN_COMMANDS_PANEL,
            border_style=typer_rich_utils.STYLE_COMMANDS_PANEL_BORDER,
        )
    )


def _print_help(policy: RemoteCommandPolicy, *, dashboard_enabled: bool, repo_root: Path) -> int:
    """Render the console-local command panel, then this policy's Jailbee help.

    `full` mode hands off entirely to the real `python -m jailbee --help`,
    run through `_run_foreground` in the session's own PTY, so the user sees
    Typer's own help output byte-for-byte instead of a hand-rolled
    approximation of it. `allowlist` mode has no single Jailbee invocation
    that prints only the allowed subset, so it renders a matching panel
    itself from each allowed path's own Click short help
    (`known_command_short_help`, resolved from the same cached command tree
    `known_command_paths`/`policy_allows` already read). `disabled` mode has
    nothing further to show.

    A Rich `Console` built fresh here (rather than at import time) picks up
    whatever `sys.stdout` currently is — the session's real PTY in
    production, so panels render in color; a captured, non-TTY stream in
    tests, so they render as plain text.

    Returns the real `jb --help` child's exit status in `full` mode, for the
    caller to fold into `last_status` exactly like every other command this
    console runs; 0 in `allowlist`/`disabled` mode, where nothing is run.
    """
    rich_console = RichConsole(file=sys.stdout)
    _render_command_panel(
        rich_console,
        "[bold]Console[/bold]",
        _console_command_rows(dashboard_enabled=dashboard_enabled),
    )

    if policy.mode == "disabled":
        rich_console.print("[dim]Jailbee commands are disabled by the remote.ssh policy.[/dim]")
        return 0

    status = 0
    if policy.mode == "full":
        completed = _run_foreground([sys.executable, "-m", "jailbee", "--help"], repo_root)
        status = _returncode(completed)
    else:
        short_help = known_command_short_help()
        rows = [(path, short_help.get(path, "")) for path in sorted(policy.allow)]
        _render_command_panel(rich_console, "[bold]Allowed Jailbee commands[/bold]", rows)

    rich_console.print("Run `<command> --help` for details on any of them.")
    return status


def _error(message: str) -> None:
    """Report a console-side rejection, styled like Jailbee's own CLI errors.

    Children of this console already run in the session's real PTY (see
    `pty.py`), so a colored `error_plain` here (bold red, "✗" marker) reaches
    the client exactly like any other Jailbee error — instead of a console
    rejection looking unstyled next to everything else on screen.
    """
    from jailbee.tui import error_plain

    error_plain(message)


def _history() -> FileHistory:
    path = state_dir() / "ssh-console-history"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(descriptor)
    path.chmod(0o600)
    return FileHistory(str(path))


def _allowed_paths(policy: RemoteCommandPolicy) -> frozenset[str]:
    """Command paths this session may complete, per its own command policy."""
    if policy.mode == "full":
        return known_command_paths()
    if policy.mode == "allowlist":
        return frozenset(policy.allow)
    return frozenset()


def _command_tree(paths: Sequence[str]) -> dict[str, Any]:
    """Build a `NestedCompleter.from_nested_dict` tree from public command paths.

    Every intermediate word is a Typer group (never itself a leaf — the walk
    in `known_command_paths` only records commands, not groups), so a word
    is unambiguously either a dict (more words follow) or `None` (a leaf).
    """
    tree: dict[str, Any] = {}
    for path in paths:
        node = tree
        words = path.split()
        for word in words[:-1]:
            child = node.setdefault(word, {})
            assert isinstance(child, dict), f"{word!r} is both a leaf and a group"
            node = child
        node[words[-1]] = None
    return tree


def _completer(paths: frozenset[str], repos: Sequence[RepoChoice]) -> NestedCompleter:
    """Nested completer over local commands, allowed Jailbee paths, and repos.

    A flat `WordCompleter` only ever completes the first word, so `git p`
    offered nothing past `git`. Each multi-word Jailbee command path now
    completes word by word, and `use ` completes registered repo prefixes.
    """
    tree = _command_tree(sorted(paths))
    for command in _LOCAL_COMMANDS:
        tree.setdefault(command, None)
    tree["use"] = {repo.prefix: None for repo in repos}
    return NestedCompleter.from_nested_dict(tree)


def _session(repos: Sequence[RepoChoice], policy: RemoteCommandPolicy) -> PromptSession[str]:
    return PromptSession(
        history=_history(),
        completer=_completer(_allowed_paths(policy), repos),
    )


def _resolve_choice(prefix: str) -> RepoChoice:
    return RepoChoice(prefix, resolve_repo(prefix))


def _select_repo(repos: Sequence[RepoChoice], **kwargs: Any) -> RepoChoice | None:
    """Arrow-key menu over registered repos; `None` on Esc/Ctrl-C/Ctrl-D.

    `questionary.select` only cancels on Ctrl-C/Ctrl-Q out of the box; a bare
    Esc or Ctrl-D is swallowed by its catch-all key binding and leaves the
    menu hanging forever (verified empirically, not just by reading the
    upstream source). Two extra *eager* key bindings, added onto the
    already-built `Application` before `ask()` runs it, make Esc and Ctrl-D
    cancel the same way Ctrl-C does — without forking `questionary.select`
    itself. `**kwargs` (e.g. `input=`/`output=` in tests) are forwarded to
    `questionary.select`.

    TRAP (see `pr_flow.py`): a `questionary.Choice` with `value=None` would
    answer with its *title* string instead of `None` on a real selection —
    irrelevant here since every choice's value is a `RepoChoice`, never
    `None`, but Ctrl-C already answers plain `None` on cancel. Esc/Ctrl-D
    must not collide with a real answer either, hence the `_CANCELLED`
    sentinel distinct from both.
    """
    import questionary

    choices = [
        questionary.Choice(title=f"{repo.prefix}\t{repo.root}", value=repo) for repo in repos
    ]
    from prompt_toolkit.key_binding import KeyBindings

    question = questionary.select("Select repository:", choices=choices, **kwargs)
    # questionary.select always builds a concrete `KeyBindings()` for this;
    # the attribute's declared type is the more abstract `KeyBindingsBase`.
    bindings = cast(KeyBindings, question.application.key_bindings)
    for key in (Keys.ControlD, Keys.Escape):
        bindings.add(key, eager=True)(lambda event: event.app.exit(result=_CANCELLED))
    result = question.ask()
    if result is None or result is _CANCELLED:
        return None
    return cast(RepoChoice, result)


def _returncode(completed: subprocess.CompletedProcess[bytes]) -> int:
    """Return a real child status while keeping loose prompt test doubles harmless."""
    return completed.returncode if isinstance(completed.returncode, int) else 0


def _run_foreground(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run one Jailbee child in the foreground, letting it handle its own Ctrl-C.

    A cooked-mode SIGINT reaches every process in the terminal's foreground
    process group at once, including this console — not just the child it
    starts. An interactive shell ignores SIGINT while its foreground job
    runs so the job's own interrupt handling decides what happens; without
    that, `subprocess.run`'s internal `Popen.wait()` catches the resulting
    KeyboardInterrupt, waits briefly, and its bare `except` kills the child
    before re-raising — defeating the child's own Ctrl-C handling (final
    review finding I1). The child itself is unaffected: it still receives
    the same SIGINT directly from the terminal, under its own disposition.
    """
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.run(argv, cwd=cwd, check=False)
    finally:
        signal.signal(signal.SIGINT, previous)


def _load_policy(policy_json: str | None) -> RemoteSSHConfig | None:
    """Validate a server-supplied effective policy, or signal to fall back.

    `None` means "load `global.yaml` myself" — the only path left for a
    console started outside `jb remote ssh serve` (there is none today, but
    nothing here assumes one). A non-`None` value is never trusted as-is:
    it goes through the very same `RemoteSSHConfig` pydantic model
    `global.yaml` itself is validated with, exactly like `apply_ssh_overrides`
    revalidates CLI overrides. Returns `None` on invalid input too, after
    printing why — the caller treats that as a startup failure, never as
    "fall back to global.yaml", since a server that sent a policy at all
    must have a bug worth surfacing, not a silent downgrade.
    """
    if policy_json is None:
        return None
    try:
        return RemoteSSHConfig.model_validate_json(policy_json)
    except ValidationError as error:
        _error(f"invalid remote SSH policy: {error}")
        return None


def run(initial_repo: str | None = None, policy_json: str | None = None) -> int:
    """Run a policy-restricted interactive Jailbee command console.

    `policy_json`, when given, is the SSH server's already-computed
    *effective* `RemoteSSHConfig` for this session (`global.yaml` merged
    with any `jb remote ssh serve` overrides) — see `server.handle_process`.
    Using it instead of reloading `global.yaml` here is the fix for the bug
    where every override flag (`--commands full`, `--shell`, ...) was
    silently ignored inside the console, which reads its own config. The
    policy is loaded once, at startup, and never reloaded for the rest of
    this session, matching the console's existing "load once" contract for
    everything else it decides with.
    """
    if policy_json is not None:
        ssh_config = _load_policy(policy_json)
        if ssh_config is None:
            return 1
    else:
        global_config, _ = load_global_config(default_global_config_path())
        ssh_config = global_config.remote.ssh
    repos = registered_repos()
    if not repos:
        _error("No registered repositories are available.")
        return 1

    session = _session(repos, ssh_config.commands)
    if initial_repo is None:
        if len(repos) == 1:
            current = repos[0]
            print(f"Only one registered repository; starting in {current.prefix} ({current.root}).")
        else:
            selected = _select_repo(repos)
            if selected is None:
                return 0
            current = selected
    else:
        try:
            current = _resolve_choice(initial_repo)
        except RouteError as error:
            _error(str(error))
            return 1

    last_status = 0
    while True:
        try:
            raw = session.prompt(f"jb[{current.prefix}]> ")
        except KeyboardInterrupt:
            print()
            continue
        except EOFError:
            print()
            return last_status

        try:
            argv = parse_console_line(raw)
        except RouteError as error:
            _error(str(error))
            continue
        if not argv:
            continue

        command = argv[0]
        if command == "exit":
            if len(argv) != 1:
                _error("usage: exit")
                continue
            return last_status
        if command == "help":
            if len(argv) != 1:
                _error("usage: help")
                continue
            last_status = _print_help(
                ssh_config.commands, dashboard_enabled=ssh_config.dashboard, repo_root=current.root
            )
            continue
        if command == "repos":
            if len(argv) != 1:
                _error("usage: repos")
                continue
            _print_repos(registered_repos())
            continue
        if command == "use":
            if len(argv) == 1:
                candidates = registered_repos()
                if not candidates:
                    _error("No registered repositories are available.")
                    continue
                selected = _select_repo(candidates)
                if selected is not None:
                    current = selected
                continue
            if len(argv) != 2:
                _error("usage: use [PREFIX]")
                continue
            try:
                current = _resolve_choice(argv[1])
            except RouteError as error:
                _error(str(error))
            continue
        if command == "dashboard":
            if len(argv) != 1:
                _error("usage: dashboard")
                continue
            if not ssh_config.dashboard:
                _error("remote dashboard is disabled")
                continue
            completed = _run_foreground(
                [sys.executable, "-m", "jailbee", "dashboard"],
                current.root,
            )
            last_status = _returncode(completed)
            continue

        if not unknown_command(argv, ssh_config.commands):
            try:
                policy_allows(argv, ssh_config.commands)
            except RouteError as error:
                _error(str(error))
                continue
        # A genuinely unknown command name (Problem C) is run as-is: Typer
        # itself reports "No such command", with suggestions, in its own
        # style — better than this console inventing its own message for a
        # name it never had an opinion about. A leading option, or a hidden
        # internal command with no public alias, is left to `policy_allows`
        # above and stays rejected here as before.

        completed = _run_foreground([sys.executable, "-m", "jailbee", *argv], current.root)
        last_status = _returncode(completed)
