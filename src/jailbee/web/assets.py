"""Where the built browser app lives. Standard library only.

`make web` builds `frontend/` into `static/` next to this file. The
directory is gitignored: it exists in a release wheel and in a checkout
after `make web`, never in git.
"""

from __future__ import annotations

from pathlib import Path


def default_static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def frontend_built(directory: Path) -> bool:
    return (directory / "index.html").is_file()
