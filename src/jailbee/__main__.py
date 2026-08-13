"""Enable `python -m jailbee` so the background worker can re-exec
the CLI identically under `uv run` and an installed `jailbee`.
"""

from jailbee.cli import app

if __name__ == "__main__":
    app()
