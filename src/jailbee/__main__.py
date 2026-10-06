"""Enable `python -m jailbee` so the background worker can re-exec
the CLI identically under `uv run` and an installed `jailbee`.
"""

import sys

from jailbee.cli import app
from jailbee.repo_option import RepoOptionError, lift_repo, with_repo_first

if __name__ == "__main__":
    try:
        sys.argv[1:] = with_repo_first(*lift_repo(sys.argv[1:]))
    except RepoOptionError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from e
    app()
