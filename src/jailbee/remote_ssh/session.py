"""Mark the processes a remote SSH session starts, and recognise them later.

Every child the SSH server starts (`pty.py`) gets `REMOTE_SESSION_ENV` in its
environment, and so does everything that child starts in turn — dashboard
actions, console commands, background workers. A command that behaves
differently for a remote caller asks `is_remote_session()` rather than taking
a flag, because a flag has to be threaded through every re-exec by hand and
the one that is forgotten fails open.

The marker only ever takes capability away, so a local user who sets it by
hand restricts their own session and nothing else. The SSH client cannot
remove it: client environment requests never reach the child (see
`server.handle_process`).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

REMOTE_SESSION_ENV = "JAILBEE_REMOTE_SSH"


def child_environment(base: Mapping[str, str], *, term: str | None = None) -> dict[str, str]:
    """The environment for a child of the SSH server, built from ``base``.

    `LESSSECURE=1` is set as well, so a `less` reached through any path — a
    pager, a tool's own paging — cannot start a shell (`!`), an editor
    (`v`) or a pipe (`|`) on the host.
    """
    env = dict(base)
    env[REMOTE_SESSION_ENV] = "1"
    env["LESSSECURE"] = "1"
    if term is not None:
        env["TERM"] = term
    return env


def is_remote_session(environ: Mapping[str, str] | None = None) -> bool:
    """True when this process descends from a remote SSH session.

    Any non-empty value counts: the marker fails closed.
    """
    return bool((os.environ if environ is None else environ).get(REMOTE_SESSION_ENV))
