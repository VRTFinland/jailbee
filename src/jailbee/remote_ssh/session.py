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

`remote.ssh.restrict_host: false` is the one way to leave it off, and only
for a server that is not itself running inside a restricted session: a
marker already in the server's own environment is inherited, never removed,
so a server started from a restricted session cannot lift the restriction
it runs under (see `host_restricted`).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

REMOTE_SESSION_ENV = "JAILBEE_REMOTE_SSH"


def child_environment(
    base: Mapping[str, str], *, term: str | None = None, restricted: bool = True
) -> dict[str, str]:
    """The environment for a child of the SSH server, built from ``base``.

    A ``restricted`` child is marked and gets `LESSSECURE=1` as well, so a
    `less` reached through any path — a pager, a tool's own paging — cannot
    start a shell (`!`), an editor (`v`) or a pipe (`|`) on the host. An
    unrestricted one gets ``base`` unchanged, marker included if ``base``
    already carries it.
    """
    env = dict(base)
    if restricted:
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


def host_tree_refusal(action: str, hint: str | None = None) -> str:
    """Why a restricted remote session may not do ``action`` to the host tree.

    A remote session moves refs only: whatever it would put into the host's
    checked-out working tree — a repo config that decides host mounts, a
    build script, a submodule's files — the host's own tools read next.
    """
    message = (
        f"{action} would change the host's checked-out working tree, which a "
        f"remote SSH session never does."
    )
    if hint:
        message += f" {hint}"
    return message + " Run it on the host itself."


def escalation_refusal(baseline_source: str) -> str:
    """Why a restricted remote session cannot approve a privilege widening.

    The branch-autostart gate asks the operator; over SSH the person
    answering is the remote user the gate exists to hold back, so neither
    the prompt nor `--yes` counts as an answer there.
    """
    return (
        f"The target branch's autostart config widens privileges beyond "
        f"{baseline_source}, and a remote SSH session cannot approve that, not "
        f"even with --yes. Run `jailbee new` on the host itself, or edit the "
        f"branch's .jailbee/config.yaml."
    )


def host_restricted(restrict_host: bool, environ: Mapping[str, str] | None = None) -> bool:
    """Whether a session under `remote.ssh.restrict_host` is held off the host.

    True when the setting says so, and also whenever this process already
    runs inside a restricted session, whatever the setting says.
    """
    return restrict_host or is_remote_session(environ)
