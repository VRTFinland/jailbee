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

    app()
