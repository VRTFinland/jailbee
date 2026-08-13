"""Seed `<shared_dir>/ssh/` from host `~/.ssh/` on first `jailbee init`.

Strict allowlist — only `config`, `known_hosts`, and `config.d/` are
copied. Everything else in host_ssh is ignored, including private keys
(delivered via SSH_AUTH_SOCK from host gpg-agent), authorized_keys
(containers don't accept inbound SSH), ControlMaster sockets, and any
user-added files.

Idempotent: returns 0 without mutating target when host is missing or
target is non-empty.

Pure stdlib (pathlib, shutil). No `subprocess`, no `incus`. Tests
inject both paths as parameters; no `Path.home()` mocking required.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SEED_FILES: tuple[str, ...] = (
    "config",
    "known_hosts",
)

SEED_DIRS: tuple[str, ...] = ("config.d",)


def seed_ssh_dir(target: Path, host_ssh: Path) -> int:
    """Copy allowlisted items from host_ssh into target.

    Returns the count of top-level items copied. Returns 0 (no-op) if
    host_ssh does not exist, is not a directory, or target is
    non-empty. Errors from shutil propagate.
    """
    if not host_ssh.is_dir():
        return 0
    if not target.exists() or any(target.iterdir()):
        return 0

    copied = 0
    for name in SEED_FILES:
        src = host_ssh / name
        if src.is_file():
            shutil.copy2(src, target / name)
            copied += 1
    for name in SEED_DIRS:
        src = host_ssh / name
        if src.is_dir():
            shutil.copytree(src, target / name, symlinks=False)
            copied += 1
    return copied
