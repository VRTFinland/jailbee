"""Make the host's terminal usable inside a container.

`incus exec` hands the host's `$TERM` to the process it starts. A terminal
with a name of its own (`xterm-kitty`, `xterm-ghostty`, ...) then meets a
terminfo database that has never heard of it, and tmux refuses to start
("missing or unsuitable terminal"). The golden image ships the entries of the
terminals jailbee knows by name; this module covers every other one at attach
time, from the host's own database:

* the container already knows `$TERM` → nothing to do;
* otherwise the host's entry is compiled into the container user's
  `~/.terminfo`;
* a host with no entry to give, or a failed compile, falls back to
  `xterm-256color`, which every image has.

The one non-incus binary here is `infocmp` (on the host); the container side
goes through the `Incus` wrapper.
"""

from __future__ import annotations

import base64
import shlex
import subprocess
from collections.abc import Mapping
from typing import TYPE_CHECKING

from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.incus import Incus

FALLBACK_TERM = "xterm-256color"
"""What a terminal the container cannot learn about is shown as."""

_BASELINE_TERMS = frozenset(
    {"xterm", "xterm-256color", "screen", "screen-256color", "tmux", "tmux-256color", "linux"}
)
"""Entries every image carries: not worth an `incus exec` to confirm."""

_FORWARDED = ("COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")
"""Host hints about the terminal's capabilities; unlike `TERM`, `incus exec`
does not carry them over."""


def _host_entry(term: str) -> str | None:
    """The host's terminfo source for ``term``, or None if it has none."""
    try:
        result = subprocess.run(
            ["infocmp", "-x", term], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def _container_knows(
    incus: Incus, container: str, term: str, *, uid: int, gid: int, home: str
) -> bool:
    try:
        incus.exec(container, ["infocmp", term], uid=uid, gid=gid, env={"HOME": home})
    except IncusError:
        return False
    return True


def _push_entry(incus: Incus, container: str, entry: str, *, uid: int, gid: int, home: str) -> bool:
    encoded = base64.b64encode(entry.encode()).decode()
    script = f'echo {shlex.quote(encoded)} | base64 -d | tic -x -o "$HOME/.terminfo" -'
    try:
        incus.exec(container, ["bash", "-c", script], uid=uid, gid=gid, env={"HOME": home})
    except IncusError:
        return False
    return True


def terminal_env(
    incus: Incus,
    container: str,
    *,
    uid: int,
    gid: int,
    home: str,
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Env overrides that make the host terminal work for the container user.

    Teaches the container ``environ["TERM"]`` when it is unknown there (see the
    module docstring) and returns the extra ``incus exec --env`` values: a
    ``TERM`` fallback only when the terminal could not be taught, plus the
    host's colour hints. Never raises: a terminal problem must not keep the
    user out of their container.
    """
    env = {key: environ[key] for key in _FORWARDED if environ.get(key)}
    term = environ.get("TERM", "")
    if not term or term == "dumb" or term in _BASELINE_TERMS:
        return env
    if _container_knows(incus, container, term, uid=uid, gid=gid, home=home):
        return env
    entry = _host_entry(term)
    if entry is None or not _push_entry(incus, container, entry, uid=uid, gid=gid, home=home):
        env["TERM"] = FALLBACK_TERM
    return env
