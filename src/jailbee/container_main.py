"""Dependency-gated container entry point; imports only the standard library."""

import sys
from pathlib import Path
from typing import NoReturn

from jailbee import cli_context, container_deps

PACKAGE_DIR = Path("/opt/jailbee/app/current/jailbee")


def main() -> NoReturn:
    error = container_deps.check()
    if error:
        print(error, file=sys.stderr)
        raise SystemExit(cli_context.EXIT_DEPS)
    if not PACKAGE_DIR.exists():
        print("jailbee code is not mounted — run `jb apply` on the host", file=sys.stderr)
        raise SystemExit(cli_context.EXIT_NOT_MOUNTED)
    if len(sys.argv) > 1:
        error = cli_context.wrong_side_message(sys.argv[1], "container")
        if error:
            print(error, file=sys.stderr)
            raise SystemExit(2)
    from jailbee.container_cli import container_app

    container_app()
    raise SystemExit(0)


if __name__ == "__main__":
    main()
