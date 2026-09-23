"""Shared helpers for installing systemd user units."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def systemd_user_dir() -> Path:
    """Return the fixed user-unit directory used by the systemd user manager."""
    return Path.home() / ".config" / "systemd" / "user"


def write_if_changed(path: Path, content: str) -> bool:
    """Atomically replace ``path`` when ``content`` differs."""
    if path.exists() and path.read_text() == content:
        return False

    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True
