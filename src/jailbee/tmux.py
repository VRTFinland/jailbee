"""tmux session management inside containers.

All operations route through ``Incus.exec`` so the module is unit-testable
with a mocked ``Incus`` instance. The session ``autostart`` is created on
demand and shared across all autostart steps for a container.
"""

from __future__ import annotations

import itertools
import os
import re
import shlex
import time
from collections.abc import Sequence
from dataclasses import dataclass

from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import Incus, IncusError

SESSION_NAME = "autostart"
SENTINEL_DIR = "/tmp/.jailbee"
BACKGROUND_PROBE_SEC = 2
STEP_REMAIN_ON_EXIT = "failed"
"""Keep a step's window after it exits only when the step failed (tmux >= 3.2)."""

_WINDOW_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_sig_counter = itertools.count()


class TmuxStepError(RuntimeError):
    """A tmux-run step failed. Carries structured fields so callers can
    render a friendly message without parsing the str() form.

    ``reason`` is one of:
      - ``"exit"``       — step finished with non-zero ``exit_code``
      - ``"timeout"``    — ``timeout`` seconds elapsed before exit
      - ``"crashed"``    — tmux died / sentinel file missing
      - ``"died_early"`` — background step exited within the probe window
    """

    def __init__(
        self,
        message: str,
        *,
        step_name: str,
        reason: str,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.step_name = step_name
        self.reason = reason
        self.exit_code = exit_code


def _runuser(tmux_cmd: str) -> list[str]:
    """Wrap a shell command to run as the container user.

    Uses ``bash -lc`` (login shell) so the user's PATH additions from
    ``~/.profile`` (pnpm, nvm, etc.) are loaded — both for the initial
    tmux server start and for any tmux command we issue afterwards.
    """
    return ["runuser", "-u", CONTAINER_USERNAME, "--", "bash", "-lc", tmux_cmd]


def ensure_session(incus: Incus, container: str, start_dir: str | None = None) -> None:
    """Create the autostart tmux session if it doesn't exist.

    Idempotent. Also ensures the sentinel directory exists.

    ``remain-on-exit`` is deliberately left at its default here, and reset to
    it when the session already exists. It used to be set globally to ``on``,
    which kept *every* exited window in the list as ``[dead]`` — including the
    ones the user opens themselves, so quitting a shell or an agent left
    litter nobody could clear except by hand. Step windows now opt in per
    window instead (see :func:`_new_window`).

    ``start_dir``, if given, is passed via ``-c`` so window 0 (the empty
    shell users see when attaching) opens there. Autostart steps set
    their own ``cwd`` per window and are unaffected.
    """
    try:
        # The trailing group heals a session an older jailbee created: the
        # server-wide `remain-on-exit on` it set outlives every upgrade for as
        # long as the container keeps running, so without this the dead
        # windows stay until the next restart. Folded into the probe to keep
        # this one `incus exec`, and its failure swallowed so a tmux that
        # dislikes the reset cannot turn a live session into a missing one.
        incus.exec(
            container,
            _runuser(
                f"tmux has-session -t {SESSION_NAME} && "
                f"(tmux set-option -gu remain-on-exit || true)"
            ),
        )
        return
    except IncusError:
        pass  # session missing — create it

    incus.exec(container, _runuser(f"mkdir -p {SENTINEL_DIR}"))
    new_session = f"tmux new-session -d -s {SESSION_NAME} -x 200 -y 50"
    if start_dir is not None:
        new_session += f" -c {shlex.quote(start_dir)}"
    try:
        incus.exec(container, _runuser(new_session))
    except IncusError as new_err:
        # Concurrent-creation race: a background `jailbee new` autostart can create
        # the session between our has-session check above and this new-session
        # call, so tmux reports "duplicate session". Tolerate it iff the session
        # now exists; otherwise the creation genuinely failed and the
        # original error must surface.
        try:
            incus.exec(container, _runuser(f"tmux has-session -t {SESSION_NAME}"))
        except IncusError:
            raise new_err from None
        return


def kill_window(incus: Incus, container: str, window: str) -> None:
    """Kill a window by name. Ignores 'no such window' errors."""
    try:
        incus.exec(
            container,
            _runuser(f"tmux kill-window -t {SESSION_NAME}:{window}"),
        )
    except IncusError:
        pass  # window didn't exist — fine


def select_window(incus: Incus, container: str, window: str) -> bool:
    """Select ``window`` as the active one in the autostart session.

    Returns True on success, False if the window does not exist or the
    command otherwise failed. Best-effort: callers (e.g. `jailbee tmux` with
    `claude.autostart`) use this for focus hinting and shouldn't fail
    when the target window died.
    """
    try:
        incus.exec(
            container,
            _runuser(f"tmux select-window -t {SESSION_NAME}:{window}"),
        )
    except IncusError:
        return False
    return True


def _sanitize_window_name(name: str) -> str:
    """Replace tmux-unsafe characters in a window name with underscores."""
    return _WINDOW_NAME_SAFE.sub("_", name)


def _env_flags(env: dict[str, str]) -> str:
    """Build a string of `-e KEY=VALUE` flags for tmux new-window."""
    parts = []
    for k, v in env.items():
        parts.append(f"-e {shlex.quote(f'{k}={v}')}")
    return " ".join(parts)


def _new_window(incus: Incus, container: str, window: str, shell_cmd: str, env_flags: str) -> None:
    """Create one detached tmux window running ``shell_cmd`` in a login shell.

    ``-d`` is what keeps the window out of the way: without it tmux makes
    every new window the session's current one, so a run that starts steps
    while somebody is attached with `jailbee tmux` yanked their view to each
    step in turn. Focus is chosen explicitly instead — `_attach_tmux` calls
    :func:`select_window` on the agent window before it attaches.

    ``remain-on-exit`` is then scoped to this window alone, and only to a
    failing exit: a step that fails keeps its output on screen to be read,
    while a step that succeeds closes and leaves the window list clean.
    Setting it per window rather than globally is what keeps the user's own
    windows out of it — theirs close whatever they exit with.

    Both run in one ``incus exec``: the window creation still decides the
    call's fate, while the option is set in a group whose failure is
    swallowed, so a tmux older than 3.2 (which has no ``failed`` value)
    gets a plain window instead of a failed step.
    """
    inner = f"bash -lc {shlex.quote(shell_cmd)}"
    new_window = (
        f"tmux new-window -d -t {SESSION_NAME}: -n {window} {env_flags} {shlex.quote(inner)}"
    )
    set_remain = (
        f"tmux set-option -w -t {SESSION_NAME}:{window} remain-on-exit {STEP_REMAIN_ON_EXIT}"
    )
    incus.exec(container, _runuser(f"{new_window} && ({set_remain} || true)"))


def run_step(
    incus: Incus,
    container: str,
    *,
    name: str,
    command: str,
    env: dict[str, str],
    cwd: str,
    background: bool,
    timeout: int,
) -> None:
    """Run a step in a tmux window.

    For ``background=True``, returns immediately after creating the window.
    For ``background=False``, blocks until the step exits and raises
    ``RuntimeError`` if the exit code is non-zero or the step times out.

    ``command`` is stripped before it is composed into the surrounding shell
    line. Both paths append to it (``; rc=$?; …`` in the sync path), so a
    trailing newline — which YAML's ``run: |`` block scalar always produces,
    and which ``AutostartStep.run`` does not strip — would start the
    continuation on a fresh line and make the whole line a bash syntax error.
    The sync path would then never write its sentinel and the caller would
    block on ``tmux wait-for`` for the full timeout.
    """
    command = command.strip()
    window = _sanitize_window_name(name)
    kill_window(incus, container, window)
    env_flags = _env_flags(env)

    if background:
        probe_sig = f"bg_{window}_{os.getpid()}_{next(_sig_counter)}"
        shell_cmd = (
            f"trap 'tmux wait-for -S {shlex.quote(probe_sig)}' EXIT; "
            f"cd {shlex.quote(cwd)} && {command}"
        )
        _new_window(incus, container, window, shell_cmd, env_flags)

        # Brief probe: surface early failure (e.g. command-not-found)
        # instead of silently continuing. If the EXIT trap fires within
        # BACKGROUND_PROBE_SEC, the step is considered to have died.
        try:
            incus.exec(
                container,
                _runuser(f"timeout {BACKGROUND_PROBE_SEC} tmux wait-for {shlex.quote(probe_sig)}"),
            )
        except IncusError:
            return  # timeout — still alive after probe
        raise TmuxStepError(
            f"background step '{name}' died within "
            f"{BACKGROUND_PROBE_SEC}s — check `jailbee tmux <container>`",
            step_name=name,
            reason="died_early",
        )

    # sync path
    sig = f"step_{window}_{os.getpid()}_{next(_sig_counter)}"
    sentinel = f"{SENTINEL_DIR}/{sig}.exit"
    shell_cmd = (
        f"cd {shlex.quote(cwd)} && {command}; "
        f"rc=$?; echo $rc > {shlex.quote(sentinel)}; "
        f"tmux wait-for -S {shlex.quote(sig)}; exit $rc"
    )
    _new_window(incus, container, window, shell_cmd, env_flags)

    try:
        incus.exec(
            container,
            _runuser(f"timeout {timeout} tmux wait-for {shlex.quote(sig)}"),
        )
    except IncusError:
        interrupt_window(incus, container, window)
        raise TmuxStepError(
            f"step '{name}' timed out after {timeout}s",
            step_name=name,
            reason="timeout",
        ) from None

    exit_text = incus.exec(
        container,
        _runuser(f"cat {shlex.quote(sentinel)} 2>/dev/null || true"),
    ).strip()
    incus.exec(container, _runuser(f"rm -f {shlex.quote(sentinel)}"))

    if not exit_text:
        raise TmuxStepError(
            f"step '{name}' exit code missing — tmux likely crashed",
            step_name=name,
            reason="crashed",
        )
    rc = int(exit_text)
    if rc != 0:
        raise TmuxStepError(
            f"step '{name}' exit code {rc}",
            step_name=name,
            reason="exit",
            exit_code=rc,
        )


@dataclass(frozen=True)
class StepHandle:
    """A step that has been started in a window and not yet reaped.

    ``deadline`` is a ``time.monotonic()`` stamp: timeouts are tracked on
    the host because a stage runs several steps at once and the
    ``timeout N tmux wait-for`` trick only serves one channel at a time.
    """

    name: str
    window: str
    sentinel: str
    background: bool
    deadline: float


def launch_step(
    incus: Incus,
    container: str,
    *,
    name: str,
    command: str,
    env: dict[str, str],
    cwd: str,
    background: bool,
    timeout: int,
) -> StepHandle:
    """Start a step in its own window and return without waiting.

    The non-blocking half of :func:`run_step`, for stages that run several
    chains at once. ``background=True`` keeps the early-death probe, so a
    step that exits inside ``BACKGROUND_PROBE_SEC`` still raises rather
    than being silently reported as a running service.
    """
    command = command.strip()
    window = _sanitize_window_name(name)
    kill_window(incus, container, window)
    env_flags = _env_flags(env)

    if background:
        probe_sig = f"bg_{window}_{os.getpid()}_{next(_sig_counter)}"
        shell_cmd = (
            f"trap 'tmux wait-for -S {shlex.quote(probe_sig)}' EXIT; "
            f"cd {shlex.quote(cwd)} && {command}"
        )
        _new_window(incus, container, window, shell_cmd, env_flags)
        try:
            incus.exec(
                container,
                _runuser(f"timeout {BACKGROUND_PROBE_SEC} tmux wait-for {shlex.quote(probe_sig)}"),
            )
        except IncusError:
            return StepHandle(name=name, window=window, sentinel="", background=True, deadline=0.0)
        raise TmuxStepError(
            f"background step '{name}' died within "
            f"{BACKGROUND_PROBE_SEC}s — check `jailbee tmux <container>`",
            step_name=name,
            reason="died_early",
        )

    sig = f"step_{window}_{os.getpid()}_{next(_sig_counter)}"
    sentinel = f"{SENTINEL_DIR}/{sig}.exit"
    shell_cmd = (
        f"cd {shlex.quote(cwd)} && {command}; "
        f"rc=$?; echo $rc > {shlex.quote(sentinel)}; "
        f"tmux wait-for -S {shlex.quote(sig)}; exit $rc"
    )
    _new_window(incus, container, window, shell_cmd, env_flags)
    return StepHandle(
        name=name,
        window=window,
        sentinel=sentinel,
        background=False,
        deadline=time.monotonic() + timeout,
    )


def poll_steps(incus: Incus, container: str, handles: Sequence[StepHandle]) -> dict[str, int]:
    """Reap whichever of ``handles`` have finished, as {step name: exit code}.

    One ``incus exec`` covers every pending step, so a stage's poll cost
    does not grow with its chain count. Background handles are skipped —
    they are fire-and-forget by definition.
    """
    pending = [h for h in handles if not h.background and h.sentinel]
    if not pending:
        return {}
    quoted = " ".join(shlex.quote(h.sentinel) for h in pending)
    script = (
        f'for f in {quoted}; do if [ -f "$f" ]; then echo "$f $(cat "$f")"; rm -f "$f"; fi; done'
    )
    out = incus.exec(container, _runuser(script))

    by_sentinel = {h.sentinel: h for h in pending}
    done: dict[str, int] = {}
    for line in out.splitlines():
        path, _, code = line.strip().partition(" ")
        handle = by_sentinel.get(path)
        if handle is None:
            continue
        try:
            done[handle.name] = int(code)
        except ValueError:
            # Sentinel present but unreadable — tmux crashed mid-write.
            done[handle.name] = -1
    return done


def window_for(step_name: str) -> str:
    """The tmux window a step of this name runs in.

    Public because a caller with no :class:`StepHandle` sometimes has to reach
    the window anyway: the serial driver blocks inside :func:`run_step` and
    holds nothing, yet a cancelled run must still interrupt what is running
    before its stage unmounts. The name is derived, not stored, so deriving it
    the same way here is exact rather than a guess.
    """
    return _sanitize_window_name(step_name)


def interrupt_window(incus: Incus, container: str, window: str) -> None:
    """Send C-c to a window. Best-effort: a container or window that is
    already gone is not an error, and nothing waits for the command to die."""
    try:
        incus.exec(
            container,
            _runuser(f"tmux send-keys -t {SESSION_NAME}:{window} C-c"),
        )
    except IncusError:
        pass


def interrupt_step(incus: Incus, container: str, handle: StepHandle) -> None:
    """Send C-c to a step's window. Best-effort, used on timeout and cancellation."""
    interrupt_window(incus, container, handle.window)
