"""Console entry point.

On macOS this delegates into a Linux VM before importing the (Linux-only) CLI;
on Linux `maybe_delegate` is a no-op and the normal Typer app runs.
"""

from __future__ import annotations

import sys


def main() -> None:
    from jailbee.macos import BridgeError, maybe_delegate

    try:
        maybe_delegate(sys.argv[1:])  # on macOS this exits; on Linux it returns
    except BridgeError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e
    from jailbee.cli import app
    from jailbee.incus import IncusError

    try:
        app()
    except IncusError as e:
        # An IncusError is jailbee's own diagnosis: it names the command that
        # failed and carries what incus wrote to stderr. Typer's traceback
        # hook would print a screenful of jailbee internals above it and bury
        # the one line the user needs — a host with no `incus` binary hit
        # exactly that, on every command. Report it and exit non-zero.
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e
