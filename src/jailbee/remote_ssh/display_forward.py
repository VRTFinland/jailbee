"""The one TCP forward the SSH server admits: the tunnel to the shared display.

`JailbeeSSHServer.connection_requested` allows `ssh -L ...` to exactly this
destination, the host loopback port the display's Incus proxy device listens
on, for any authenticated key while `remote.ssh.rdp` is on. Every key that
can log in can also launch an app on the shared screen, so binding the
tunnel to keys that already have would add a step without protecting
anything (see `docs/security.md`).
"""

from __future__ import annotations

DISPLAY_FORWARD_HOST = "127.0.0.1"
DISPLAY_FORWARD_PORT = 13389


def is_display_forward(host: str, port: int) -> bool:
    return (host, port) == (DISPLAY_FORWARD_HOST, DISPLAY_FORWARD_PORT)
