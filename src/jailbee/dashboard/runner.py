"""Run one dashboard action: policy check, child with a prompt channel, audit.

Frontend-agnostic: the terminal dashboard and the web dashboard both call
:func:`run_action`. It checks the action against the transport's remote policy
*before* anything is started, runs JailBee's own CLI as a child with a prompt
channel (`jailbee.prompt_channel`), relays the child's questions to a UI
callback, streams its output to a sink and returns the exit status. Remote
transports are audited (one JSONL record per action, never its arguments).

Attach-style actions (shell, tmux, GUI apps) need the client's own terminal or
display and are not run through here; see :func:`is_attach_style`.
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

from jailbee.config.models_remote import RemotePolicy
from jailbee.dashboard import actions as dact
from jailbee.dashboard.commands import Transport, check_dashboard_command
from jailbee.dashboard.menus import APPS_RUN_PREFIX, ATTACH_VERBS
from jailbee.prompt_channel import PROMPT_FD_ENV, Prompt, UiEnd
from jailbee.remote_ssh import router
from jailbee.remote_ssh.router import RouteError

log = logging.getLogger(__name__)


# A prompt handler returns the wire value (see `prompt_channel`: bool for
# confirm, option id for choose_one, list of ids for choose_many, str for
# text/secret) or None to cancel the question.
PromptHandler = Callable[[Prompt], Any]
OutputSink = Callable[[str], None]
AuditSink = Callable[[dict[str, Any]], None]


class ChildProcess(Protocol):
    """What the runner needs from a started command."""

    stdout: IO[bytes] | None  # merged stdout+stderr; None = no output

    def wait(self) -> int: ...

    def kill(self) -> None: ...


# spawn(argv, cwd=, env=, prompt_sock=) -> child. The spawner owns `prompt_sock`
# (the child's end of the channel) from the call on and must close this
# process's copy once the child has its own.
Spawner = Callable[..., ChildProcess]


@dataclass(frozen=True)
class RunResult:
    """How an action ended. ``refused`` set means no child was started."""

    status: int | None = None
    refused: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.refused is None and self.error is None and self.status == 0


@dataclass(frozen=True)
class ActionRun:
    """One action of a batch: ``jailbee <argv>`` in ``cwd``."""

    argv: list[str]  # without the program name, already addressed at its repo
    cwd: Path


@dataclass(frozen=True)
class ActionOutcome:
    """How a batch of runs ended, and what they printed."""

    results: list[RunResult]  # one per run that was started, in order
    output: str


# The orphan rows' own attach verbs (terminal dashboard only, never remote).
_ORPHAN_ATTACH_VERBS: frozenset[str] = frozenset({"orphan tmux", "orphan shell"})


def is_attach_style(argv: Sequence[str]) -> bool:
    """True for actions that must own the client's terminal or display."""
    label = _command_path(argv)
    return (
        label in ATTACH_VERBS
        or label in _ORPHAN_ATTACH_VERBS
        or (label + " ").startswith(APPS_RUN_PREFIX)
    )


def _command_path(argv: Sequence[str]) -> str:
    """The command path of ``argv`` (``git pull``), never its arguments."""
    try:
        return router.command_path(argv)
    except RouteError:
        return "unknown"


def cli_argv(argv: Sequence[str]) -> list[str]:
    """``argv`` behind JailBee's own entry point (the running interpreter)."""
    return [sys.executable, "-m", "jailbee", *argv]


def default_audit_path() -> Path:
    from jailbee.db import state_dir

    return state_dir() / "audit" / "remote-actions.jsonl"


def jsonl_audit(path: Path | None = None) -> AuditSink:
    """An audit sink appending one JSON object per line to ``path`` (0600)."""
    lock = threading.Lock()

    def write(record: dict[str, Any]) -> None:
        target = path or default_audit_path()
        line = json.dumps(record, sort_keys=True) + "\n"
        with lock:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(line)

    return write


class _PopenChild:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.stdout = proc.stdout

    def wait(self) -> int:
        return self._proc.wait()

    def kill(self) -> None:
        self._proc.kill()


def popen_spawn(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], prompt_sock: socket.socket
) -> ChildProcess:
    """Start ``argv`` with the child's end of the channel inherited (``pass_fds``)."""
    fd = prompt_sock.fileno()
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env={**env, PROMPT_FD_ENV: str(fd)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            pass_fds=(fd,),
        )
    finally:
        prompt_sock.close()
    return _PopenChild(proc)


def _child_env(
    transport: Transport, policy: RemotePolicy | None, client: str | None = None
) -> dict[str, str]:
    if transport == "none" or policy is None:
        return dict(os.environ)
    from jailbee.remote_ssh.repo_scope import scope_for_session
    from jailbee.remote_ssh.session import child_environment, host_restricted

    excluded = sorted(scope_for_session().excluded | frozenset(policy.excluded_repos))
    return child_environment(
        os.environ,
        transport=transport,
        restricted=host_restricted(policy.restrict_host),
        excluded_repos=excluded,
        client=client,
    )


def run_actions(
    runs: Sequence[ActionRun],
    *,
    run: Callable[..., RunResult] | None = None,
    **common: Any,
) -> ActionOutcome:
    """Run ``runs`` one after another through ``run`` (default `run_action`).

    ``common`` is what every run shares (`transport`, `policy`, `on_prompt`,
    `client`, `spawn`...). The output of all runs is one text: with more than
    one run each starts with a ``── jailbee <command> ──`` header. The batch
    stops at the first run that was refused or could not start; a run that
    merely exits non-zero does not stop the others. Frontend-agnostic: the
    terminal and web dashboards both run bulk actions through it.
    """
    runner_fn = run if run is not None else run_action
    chunks: list[str] = []
    results: list[RunResult] = []
    try:
        for item in runs:
            if len(runs) > 1:
                chunks.append(f"── jailbee {_command_path(item.argv)} ──\n")
            result = runner_fn(item.argv, cwd=item.cwd, on_output=chunks.append, **common)
            results.append(result)
            if result.error is not None or result.refused is not None:
                break
    except Exception as exc:
        results.append(RunResult(error=str(exc)))
    return ActionOutcome(results, "".join(chunks))


def run_action(
    argv: Sequence[str],
    *,
    transport: Transport,
    policy: RemotePolicy | None,
    cwd: Path,
    on_prompt: PromptHandler,
    on_output: OutputSink,
    client: str | None = None,
    address_flags: Sequence[str] = (),
    spawn: Spawner = popen_spawn,
    audit: AuditSink | None = None,
) -> RunResult:
    """Run ``jailbee <argv>`` and return how it ended; blocks until it exits.

    ``argv`` is the action without the program name and is policy-checked
    exactly as given. ``address_flags`` (``--config`` ...) are added only for
    the local transport. ``client`` is the remote client's identity (key
    fingerprint, device name) for the audit record. ``on_prompt`` runs on the
    calling thread, one prompt at a time; ``on_output`` runs on a helper thread.
    A refusal returns ``RunResult(refused=...)`` and starts nothing. A handler
    exception kills the child, is audited as ``error`` and propagates.
    """
    remote = transport != "none"
    sink = audit if audit is not None else (jsonl_audit() if remote else None)
    label = _command_path(argv)

    def record(outcome: str) -> None:
        if not remote or sink is None:
            return
        try:
            sink(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "transport": transport,
                    "client": client,
                    "command": label,
                    "outcome": outcome,
                }
            )
        except OSError as exc:
            log.warning("could not write the remote action audit record: %s", exc)

    if is_attach_style(argv):
        reason = f"{label}: attach-style actions are not run through the action runner"
        record("refused")
        return RunResult(refused=reason)
    try:
        check_dashboard_command(argv, policy, transport=transport)
    except (RouteError, ValueError) as exc:  # ValueError: unreadable repo scope snapshot
        record("refused")
        return RunResult(refused=str(exc))

    full = cli_argv(dact.addressed(argv, address_flags, transport=transport))
    try:
        env = _child_env(transport, policy, client)
    except Exception as exc:  # scope snapshot unreadable: fail closed, nothing started
        record("refused")
        return RunResult(refused=f"remote policy snapshot unavailable: {exc}")

    ui_sock, child_sock = socket.socketpair()
    try:
        child = spawn(full, cwd=cwd, env=env, prompt_sock=child_sock)
    except OSError as exc:
        ui_sock.close()
        child_sock.close()
        record("error")
        return RunResult(error=str(exc))

    reader = threading.Thread(target=_pump, args=(child, on_output), daemon=True)
    reader.start()
    ui = UiEnd(ui_sock)
    try:
        ui.serve(on_prompt)
    except BaseException:
        child.kill()
        child.wait()
        reader.join()
        ui.close()
        record("error")
        raise
    ui.close()
    status = child.wait()
    reader.join()
    record(f"exit:{status}")
    return RunResult(status=status)


def _pump(child: ChildProcess, on_output: OutputSink) -> None:
    stream = child.stdout
    if stream is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = os.read(stream.fileno(), 4096)
        if not chunk:
            break
        text = decoder.decode(chunk)
        if text:
            on_output(text)
    tail = decoder.decode(b"", final=True)
    if tail:
        on_output(tail)
    stream.close()
