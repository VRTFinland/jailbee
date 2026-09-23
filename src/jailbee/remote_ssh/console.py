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

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.db import get_engine, state_dir
from jailbee.db.models import RegisteredRepo
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.remote_ssh.router import (
    RouteError,
    known_command_paths,
    policy_allows,
    resolve_repo,
)

_LOCAL_COMMANDS = ("dashboard", "exit", "help", "repos", "use")


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


def _print_help() -> None:
    print("Console commands:")
    print("  repos       list registered repositories")
    print("  use PREFIX  switch repository")
    print("  dashboard   open the registered-repository dashboard")
    print("  help        show this help")
    print("  exit        leave the console")


def _error(message: str) -> None:
    print(message, file=sys.stderr)


def _history() -> FileHistory:
    path = state_dir() / "ssh-console-history"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(descriptor)
    path.chmod(0o600)
    return FileHistory(str(path))


def _session(repos: Sequence[RepoChoice]) -> PromptSession[str]:
    words = sorted({*_LOCAL_COMMANDS, *(repo.prefix for repo in repos), *known_command_paths()})
    return PromptSession(
        history=_history(),
        completer=WordCompleter(words, ignore_case=True),
    )


def _resolve_choice(prefix: str) -> RepoChoice:
    return RepoChoice(prefix, resolve_repo(prefix))


def _pick_repo(session: PromptSession[str], repos: Sequence[RepoChoice]) -> RepoChoice | None:
    _print_repos(repos)
    while True:
        try:
            prefix = session.prompt("Select repository: ").strip()
        except KeyboardInterrupt:
            print()
            continue
        except EOFError:
            print()
            return None
        if not prefix:
            continue
        try:
            return _resolve_choice(prefix)
        except RouteError as error:
            _error(str(error))


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

    session = _session(repos)
    if initial_repo is None:
        current = _pick_repo(session, repos)
        if current is None:
            return 0
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
            _print_help()
            continue
        if command == "repos":
            if len(argv) != 1:
                _error("usage: repos")
                continue
            _print_repos(registered_repos())
            continue
        if command == "use":
            if len(argv) != 2:
                _error("usage: use PREFIX")
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
                [sys.executable, "-m", "jailbee", "dashboard", "--registered-only"],
                current.root,
            )
            last_status = _returncode(completed)
            continue

        try:
            policy_allows(argv, ssh_config.commands)
        except RouteError as error:
            _error(str(error))
            continue

        completed = _run_foreground([sys.executable, "-m", "jailbee", *argv], current.root)
        last_status = _returncode(completed)
