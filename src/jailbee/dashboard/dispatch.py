"""Running ``jailbee`` children for the terminal dashboard: paged, output, plain.

The single ``subprocess`` use here spawns jailbee's own CLI — a NON-incus
subprocess, in the same spirit as ``gui.py`` launching GUI processes, so
each action reuses the real command's behaviour and the target repo's config.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import actions as dact
from jailbee.dashboard.commands import (
    check_dashboard_command,
    dashboard_action_argv,
)
from jailbee.dashboard.menus import APPS_RUN_PREFIX, ATTACH_VERBS, PRINTING_VERBS, _is_gui_verb
from jailbee.dashboard.model import RepoTarget
from jailbee.remote_ssh.session import waypipe_session
from jailbee.tui import console

log = logging.getLogger(__name__)

# The printing verbs long enough to want a pager instead of a pause. `Live`
# repaints the moment the dashboard resumes, so the rest get the pause: without
# one their output is gone before it can be read.
_PAGED_VERBS: frozenset[str] = frozenset({"git diff"})
_OUTPUT_VERBS: frozenset[str] = PRINTING_VERBS - _PAGED_VERBS

DispatchStyle = Literal["paged", "output", "plain"]


def dispatch_style(verb: str) -> DispatchStyle:
    """How the TUI should run ``verb``: through a pager, with a pause, or bare."""
    if verb in _PAGED_VERBS:
        return "paged"
    if verb in _OUTPUT_VERBS:
        return "output"
    return "plain"


def command_needs_pause(typed: str) -> bool:
    """Retain output for noninteractive commands entered in the editor."""
    return typed not in ATTACH_VERBS and not typed.startswith(APPS_RUN_PREFIX)


def pager_argv() -> list[str] | None:
    """The pager to page long output through, or None when the host has none.

    ``$PAGER`` wins (split as a shell word list, so ``PAGER="bat -p"`` works);
    otherwise ``less -R``, which renders the ANSI colour the diff is asked to
    emit, then ``more``.
    """
    env = os.environ.get("PAGER")
    if env:
        return shlex.split(env)
    for candidate in (["less", "-R"], ["more"]):
        if shutil.which(candidate[0]):
            return candidate
    return None


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


class _PagerUnavailableError(OSError):
    """The pager process itself could not be started.

    Raised only from the viewer ``Popen`` below — never from the producer's —
    so a caller catching this specifically (rather than bare ``OSError``) can
    tell "the pager is missing" apart from "the producer couldn't even start"
    (e.g. its ``cwd`` has vanished) and fall back to the unpaged path only for
    the former. A bare ``OSError`` out of this function is always the
    producer's.
    """


def _run_paged(argv: list[str], pager: list[str], cwd: Path) -> int:
    """Pipe ``argv``'s stdout into ``pager``; return the command's exit code.

    Two processes rather than a shell string, so there is no quoting to get
    wrong. Stderr stays attached to the terminal: an error message must not be
    swallowed by the pager. The pager owns the terminal until the user quits
    it, which is why the paged path needs no keypress pause of its own.

    ``cwd`` applies to the producer only — it is how a repo with no config file
    is addressed at all (see :class:`RepoTarget`). The pager is a plain viewer
    on a pipe and has no repo of its own.

    Raises a bare ``OSError`` (uncaught here) if the producer itself cannot be
    started — most notably a ``cwd`` that has disappeared out from under a
    dispatch — and :class:`_PagerUnavailableError` if the pager cannot be started,
    so callers do not conflate the two.
    """
    producer = subprocess.Popen(argv, stdout=subprocess.PIPE, cwd=cwd)
    out = producer.stdout
    if out is None:  # unreachable with stdout=PIPE; keeps mypy honest
        return producer.wait()
    try:
        viewer = subprocess.Popen(pager, stdin=out)
    except OSError as exc:
        # The pager vanished between which() and exec. Nothing will ever read
        # the pipe, so kill the producer rather than leaving it blocked on a
        # full one, and let the caller fall back to the unpaged path.
        producer.kill()
        producer.wait()
        raise _PagerUnavailableError(str(exc)) from exc
    finally:
        # The viewer owns the read end now. Keeping this copy open would stop
        # the pager ever seeing EOF, so it would hang on a finished command.
        out.close()
    viewer.wait()
    return producer.wait()


def _dispatch_action(
    target: RepoTarget,
    verb: str,
    name: str,
    *,
    remote: bool = False,
    over_ssh: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
) -> int:
    """Run ``jailbee <verb> <name>`` against ``target``; return its exit code.

    The single dispatch point shared by the inline action menu and the
    quick-action keys, so both reuse the real command's behaviour and the
    target repo's own config. ``verb`` may be multi-token (``"net loose"``,
    ``"pr --open"``, ``"job log --follow"``).

    Every child runs in ``target.cwd()``, and a configured repo additionally
    gets ``--config``: a repo with no config file has no path to pass, so the
    working directory is the only thing that says which repo this is.

    Verbs in :data:`ATTACH_VERBS`, and any verb `_app_menu_verb` composed
    with the :data:`APPS_RUN_PREFIX` (a config-sourced `apps:` entry), gain
    ``--force``; ``--force`` means something different on every other
    command (and most don't accept it), so nothing else gets it.

    The verb's :func:`dispatch_style` decides what happens to its output: a
    pager for the diff (with ``--color`` forced, because the pipe would
    otherwise turn colour off), a keypress pause for the other printing verbs,
    and nothing at all for the rest. A missing or unstartable pager degrades to
    the pause rather than losing the output.

    A remote session (``remote``) never gets a pager: every pager worth the
    name can run commands (`less`'s ``!``, ``v`` and ``|``, `more`'s ``!``),
    and here they would run on the host. The paged verbs fall back to the
    keypress pause instead, and the client's own scrollback does the paging.

    Raises ``OSError`` (uncaught here) if ``target.cwd()`` has disappeared out
    from under the dispatch — the caller (:func:`run`'s ``dispatch``) turns
    that into a notice naming the directory rather than letting it take the
    whole TUI down. That is deliberately *not* caught as "pager failed": see
    :class:`_PagerUnavailableError`.
    """
    action_argv = dashboard_action_argv(
        verb, name, force=verb in ATTACH_VERBS or verb.startswith(APPS_RUN_PREFIX)
    )
    check_dashboard_command(action_argv, ssh_policy, over_ssh=over_ssh)
    argv = ["jailbee", *verb.split(), name, *(target.flags() if not over_ssh else [])]
    if verb in ATTACH_VERBS or verb.startswith(APPS_RUN_PREFIX):
        argv.append("--force")
    style = dispatch_style(verb)
    if (
        over_ssh
        and ssh_policy is not None
        and ssh_policy.gui
        and _is_gui_verb(verb)
        and waypipe_session() is None
    ):
        # The launch prints how to reach the shared RDP display; "plain" would
        # throw that away the moment the dashboard repaints. A waypipe session
        # has nothing to show: the window simply opens on the laptop.
        style = "output"
    if style == "paged" and remote:
        style = "output"
    if style == "paged":
        pager = pager_argv()
        if pager is not None:
            try:
                return _run_paged([*argv, "--color"], pager, target.cwd())
            except _PagerUnavailableError as exc:
                log.debug("pager %s failed: %s", pager, exc)
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
    over_ssh: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
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
    ``style`` works as in :func:`_dispatch_action`. A remote session never gets
    a pager, because a pager can run host commands, so it gets the pause. A
    pager that cannot start also degrades to the pause.

    Raises :class:`RouteError` before anything runs when the policy refuses
    ``argv``, and ``OSError`` when ``target.cwd()`` has vanished (the caller
    turns that into a notice).
    """
    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
    full = ["jailbee", *dact.addressed(argv, target.flags(), over_ssh=over_ssh)]
    if style == "paged" and (remote or over_ssh):
        style = "output"
    if style == "paged":
        pager = pager_argv()
        if pager is not None:
            try:
                return _run_paged(full, pager, target.cwd())
            except _PagerUnavailableError as exc:
                log.debug("pager %s failed: %s", pager, exc)
    rc = subprocess.run(full, check=False, cwd=target.cwd()).returncode
    if style != "plain":
        _wait_for_return()
    return rc


def _run_bulk_foreground(
    runs: Sequence[tuple[RepoTarget, Sequence[str]]],
    *,
    over_ssh: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
) -> list[int]:
    """Run each ``(repo, argv)`` in the terminal, in order; return the exit codes.

    A bulk git verb over several repos: one run per repo, because one
    ``--config`` addresses one repo. Every argv is checked against the SSH
    policy before the first one runs, so a refusal leaves nothing half done.
    One pause after the last run, not one per repo: the CLI prints a roll-up
    per run, and they read better together.
    """
    for _target, argv in runs:
        check_dashboard_command(list(argv), ssh_policy, over_ssh=over_ssh)
    codes = [
        subprocess.run(
            ["jailbee", *dact.addressed(list(argv), target.flags(), over_ssh=over_ssh)],
            check=False,
            cwd=target.cwd(),
        ).returncode
        for target, argv in runs
    ]
    _wait_for_return()
    return codes
