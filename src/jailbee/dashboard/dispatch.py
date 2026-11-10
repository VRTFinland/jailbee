"""Running ``jailbee`` children for the terminal dashboard: paged, output, plain.

The single ``subprocess`` use here spawns jailbee's own CLI — a NON-incus
subprocess, in the same spirit as ``gui.py`` launching GUI processes, so
each action reuses the real command's behaviour and the target repo's config.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from jailbee.config.models_remote import RemotePolicy
from jailbee.dashboard import actions as dact
from jailbee.dashboard.commands import (
    Transport,
    check_dashboard_command,
    dashboard_action_argv,
)
from jailbee.dashboard.menus import APPS_RUN_PREFIX, ATTACH_VERBS, PRINTING_VERBS, _is_gui_verb
from jailbee.dashboard.model import RepoTarget
from jailbee.remote_ssh.session import waypipe_session
from jailbee.tui import DIAGNOSTICS_ENV, console

log = logging.getLogger(__name__)

# The printing verbs long enough to want a pager instead of a pause. `Live`
# repaints the moment the dashboard resumes, so the rest get the pause: without
# one their output is gone before it can be read.
_PAGED_VERBS: frozenset[str] = frozenset({"git diff"})
# The printing verbs whose clean run leaves nothing to read: the pause is kept
# for a failure or a warning only. `git push --pr` is not one — its PR URL is.
_REPORT_VERBS: frozenset[str] = frozenset({"git push", "git pull"})
# The orphan lifecycle verbs: terminal-dashboard only (so not in the Qt-shared
# `PRINTING_VERBS`), and nothing else says whether they worked.
_ORPHAN_OUTPUT_VERBS: frozenset[str] = frozenset({"orphan start", "orphan stop", "orphan destroy"})
_OUTPUT_VERBS: frozenset[str] = PRINTING_VERBS - _PAGED_VERBS - _REPORT_VERBS

# "report" is "output" whose pause is kept only for a failure or a warning:
# a clean run has nothing the user still needs to read.
DispatchStyle = Literal["paged", "output", "report", "plain"]


def dispatch_style(verb: str) -> DispatchStyle:
    """How the TUI should run ``verb``: through a pager, with a pause, or bare."""
    if verb in _PAGED_VERBS:
        return "paged"
    if verb in _REPORT_VERBS:
        return "report"
    if verb in _OUTPUT_VERBS or verb in _ORPHAN_OUTPUT_VERBS:
        return "output"
    return "plain"


def command_needs_pause(typed: str) -> bool:
    """Retain output for noninteractive commands entered in the editor."""
    return typed not in ATTACH_VERBS and not typed.startswith(APPS_RUN_PREFIX)


def _wait_for_return() -> None:
    """Hold the terminal until the user has read the output.

    Called only from inside ``run``'s ``foreground`` helper, where ``Live`` is
    stopped and the terminal is back in cooked mode — so a plain read is
    enough. EOF (piped stdin, Ctrl-D) and Ctrl-C return immediately rather
    than propagating: neither is a reason to take the dashboard down.
    """
    console.print("\n[dim]── press Enter to return to the dashboard ──[/dim]")
    try:
        sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        pass


@contextlib.contextmanager
def _diagnostics_marker() -> Iterator[Path]:
    """A not-yet-existing file for children to note their warnings and errors in.

    A directory of our own holds it, so its absence before the runs is
    guaranteed and it is cleaned up with the directory. Pass it to a child
    with :func:`_noting_env`; it exists afterwards only if something warned.
    """
    with tempfile.TemporaryDirectory(prefix="jailbee-dashboard-") as tmp:
        yield Path(tmp) / "diagnostics"


def _noting_env(marker: Path) -> dict[str, str]:
    """This process's environment, plus where `jailbee.tui` notes diagnostics."""
    return {**os.environ, DIAGNOSTICS_ENV: str(marker)}


def read_diagnostic_text(path: Path) -> str | None:
    """Read bounded child diagnostic text without trusting its encoding."""
    try:
        with path.open("rb") as stream:
            data = stream.read(4097)
    except OSError:
        return None
    truncated = len(data) > 4096
    text = data[:4096].decode("utf-8", errors="replace").strip()
    if truncated:
        text += " [truncated]"
    return text or None


def _run_reporting(argv: list[str], cwd: Path) -> int:
    """Run ``argv``; pause only when it failed or printed a warning or error."""
    with _diagnostics_marker() as marker:
        rc = subprocess.run(argv, check=False, cwd=cwd, env=_noting_env(marker)).returncode
        noisy = marker.exists()
    if rc != 0 or noisy:
        _wait_for_return()
    return rc


def action_cli_argv(
    target: RepoTarget, verb: str, name: str, *, transport: Transport = "none"
) -> list[str]:
    """The ``jailbee`` argument list (without the program name) for ``<verb> <name>``.

    Addressed at ``target`` (``--config``) only for the local transport, and
    carrying ``--force`` for the attach verbs and registry apps, which are the
    only commands that mean something by it.
    """
    argv = [*verb.split(), name, *(target.flags() if transport == "none" else [])]
    if verb in ATTACH_VERBS or verb.startswith(APPS_RUN_PREFIX):
        argv.append("--force")
    return argv


def action_style(
    verb: str,
    *,
    remote: bool = False,
    transport: Transport = "none",
    ssh_policy: RemotePolicy | None = None,
) -> DispatchStyle:
    """:func:`dispatch_style` with the remote adjustments of :func:`_dispatch_action`."""
    style = dispatch_style(verb)
    if (
        transport != "none"
        and ssh_policy is not None
        and ssh_policy.rdp
        and _is_gui_verb(verb)
        and waypipe_session() is None
    ):
        # The launch prints how to reach the shared RDP display; "plain" would
        # throw that away the moment the dashboard repaints. A waypipe session
        # has nothing to show: the window simply opens on the laptop.
        style = "output"
    if style == "paged" and remote:
        style = "output"
    return style


def _dispatch_action(
    target: RepoTarget,
    verb: str,
    name: str,
    *,
    remote: bool = False,
    transport: Transport = "none",
    ssh_policy: RemotePolicy | None = None,
) -> int:
    """Run ``jailbee <verb> <name>`` against ``target``; return its exit code.

    The single dispatch point shared by the inline action menu and the
    quick-action keys, so both reuse the real command's behaviour and the
    target repo's own config. ``verb`` may be multi-token (``"net loose"``,
    ``"pr open"``, ``"job log --follow"``).

    Every child runs in ``target.cwd()``, and a configured repo additionally
    gets ``--config``: a repo with no config file has no path to pass, so the
    working directory is the only thing that says which repo this is.

    Verbs in :data:`ATTACH_VERBS`, and any verb `_app_menu_verb` composed
    with the :data:`APPS_RUN_PREFIX` (a config-sourced `apps:` entry), gain
    ``--force``; ``--force`` means something different on every other
    command (and most don't accept it), so nothing else gets it.

    Only commands that keep the terminal come here (attach verbs, a followed
    log); everything else runs through the action runner. The verb's style
    decides what happens to its output: a keypress pause for a printing verb
    (for `git push`/`git pull` only after a failure or warning), nothing at
    all for the rest. There is no pager: a pager can run host commands.

    Raises ``OSError`` (uncaught here) if ``target.cwd()`` has disappeared out
    from under the dispatch — the caller turns that into a notice naming the
    directory rather than letting it take the whole TUI down.
    """
    action_argv = dashboard_action_argv(
        verb, name, force=verb in ATTACH_VERBS or verb.startswith(APPS_RUN_PREFIX)
    )
    check_dashboard_command(action_argv, ssh_policy, transport=transport)
    argv = ["jailbee", *action_cli_argv(target, verb, name, transport=transport)]
    style = action_style(verb, remote=remote, transport=transport, ssh_policy=ssh_policy)
    if style == "report":
        return _run_reporting(argv, target.cwd())
    rc = subprocess.run(argv, check=False, cwd=target.cwd()).returncode
    if style != "plain":
        _wait_for_return()
    return rc


def _run_cli_foreground(
    target: RepoTarget,
    argv: list[str],
    *,
    style: DispatchStyle,
    remote: bool = False,
    transport: Transport = "none",
    ssh_policy: RemotePolicy | None = None,
    diagnostic_path: Path | None = None,
) -> int:
    """Run a dashboard-built ``jailbee <argv>`` against ``target``; return its exit code.

    The counterpart of :func:`_dispatch_action` for entries that are not
    ``<verb> <container>``: repo-level commands (``apply``, ``doctor``) and argv
    carrying a ``--``-guarded answer (``snapshot create -- NAME TAG``).
    ``_dispatch_action`` is deliberately not rebuilt on top of this. Its argv
    order (``--force`` before ``--config``) is pinned by many tests, and nothing
    would change for the user.

    ``argv`` is checked exactly as given, then addressed: ``--config`` goes
    before any ``--``, and nothing is added over SSH (`jailbee.dashboard.actions.addressed`).
    ``style`` works as in :func:`_dispatch_action`, plus ``"report"``: the
    pause only when the command failed or warned (see :func:`_run_reporting`).
    Raises :class:`RouteError` before anything runs when the policy refuses
    ``argv``, and ``OSError`` when ``target.cwd()`` has vanished (the caller
    turns that into a notice).
    """
    check_dashboard_command(argv, ssh_policy, transport=transport)
    full = ["jailbee", *dact.addressed(argv, target.flags(), transport=transport)]
    if style == "report":
        return _run_reporting(full, target.cwd())
    env = None
    if diagnostic_path is not None:
        from jailbee.tui import DIAGNOSTIC_TEXT_ENV

        env = {**os.environ, DIAGNOSTIC_TEXT_ENV: str(diagnostic_path)}
    if env is None:
        rc = subprocess.run(full, check=False, cwd=target.cwd()).returncode
    else:
        rc = subprocess.run(full, check=False, cwd=target.cwd(), env=env).returncode
    if style != "plain":
        _wait_for_return()
    return rc
