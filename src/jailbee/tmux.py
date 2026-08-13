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

from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import Incus, IncusError

SESSION_NAME = "autostart"
SENTINEL_DIR = "/tmp/.jailbee"
BACKGROUND_PROBE_SEC = 2

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

    Idempotent. Also ensures the sentinel directory exists and that the
    session has ``remain-on-exit on`` so failed windows stay visible.

    ``start_dir``, if given, is passed via ``-c`` so window 0 (the empty
    shell users see when attaching) opens there. Autostart steps set
    their own ``cwd`` per window and are unaffected.
    """
    try:
        incus.exec(container, _runuser(f"tmux has-session -t {SESSION_NAME}"))
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
        # now exists (the winning caller owns remain-on-exit); otherwise the
        # creation genuinely failed and the original error must surface.
        try:
            incus.exec(container, _runuser(f"tmux has-session -t {SESSION_NAME}"))
        except IncusError:
            raise new_err from None
        return
    incus.exec(
        container,
        _runuser(f"tmux set-option -t {SESSION_NAME} -g remain-on-exit on"),
    )


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
    """
    window = _sanitize_window_name(name)
    kill_window(incus, container, window)
    env_flags = _env_flags(env)

    if background:
        probe_sig = f"bg_{window}_{os.getpid()}_{next(_sig_counter)}"
        shell_cmd = (
            f"trap 'tmux wait-for -S {shlex.quote(probe_sig)}' EXIT; "
            f"cd {shlex.quote(cwd)} && {command}"
        )
        inner = f"bash -lc {shlex.quote(shell_cmd)}"
        new_window = (
            f"tmux new-window -t {SESSION_NAME}: -n {window} {env_flags} {shlex.quote(inner)}"
        )
        incus.exec(container, _runuser(new_window))

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
    inner = f"bash -lc {shlex.quote(shell_cmd)}"
    new_window = f"tmux new-window -t {SESSION_NAME}: -n {window} {env_flags} {shlex.quote(inner)}"
    incus.exec(container, _runuser(new_window))

    try:
        incus.exec(
            container,
            _runuser(f"timeout {timeout} tmux wait-for {shlex.quote(sig)}"),
        )
    except IncusError:
        try:
            incus.exec(
                container,
                _runuser(f"tmux send-keys -t {SESSION_NAME}:{window} C-c"),
            )
        except IncusError:
            pass
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
