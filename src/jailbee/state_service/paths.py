"""Where the state service's socket, locks and log live."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from jailbee.db import state_dir
from jailbee.state_service import StateServiceError


def runtime_dir() -> Path:
    """``$XDG_RUNTIME_DIR/jailbee``, or a per-user directory in the temp dir.

    The fallback is for hosts without a login session manager (macOS, a bare
    container): ``/tmp/jailbee-<uid>``, short enough that the socket path
    stays well inside the ~104-byte ``sun_path`` limit.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "jailbee"
    return Path(tempfile.gettempdir()) / f"jailbee-{os.getuid()}"


def ensure_runtime_dir() -> Path:
    """Create `runtime_dir` private to this user, refusing one that is not.

    A shared temp dir lets anyone pre-create the directory; a socket in a
    directory someone else controls could be swapped for theirs.
    """
    path = runtime_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise StateServiceError(f"{path} must be owned by you and private (mode 0700)")
    return path


def socket_path() -> Path:
    return runtime_dir() / "state.sock"


def lock_path() -> Path:
    """Held by the running server for its whole lifetime."""
    return runtime_dir() / "state.lock"


def spawn_lock_path() -> Path:
    """Held by a client while it starts a server, so two never race to."""
    return runtime_dir() / "state.spawn.lock"


def log_path() -> Path:
    return state_dir() / "state-service.log"
