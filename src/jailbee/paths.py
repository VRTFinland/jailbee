"""Path utilities — XDG-compliant config location, environment expansion."""

import functools
import os
from pathlib import Path

REPO_CONFIG_DIRS: tuple[str, ...] = (".jailbee", ".gie")
"""Repo config directories, most preferred first.

``.gie`` is the pre-1.0 location. It is accepted with a deprecation warning
because the file is committed to shared application repos, so renaming it
there cannot be synchronised with each user's tool upgrade. Removed in 2.0.0.
"""


def expand_path(path: str | Path) -> Path:
    """Expand ~, $VARS, and resolve to an absolute path."""
    p = Path(os.path.expandvars(str(path))).expanduser()
    return p.resolve() if not p.is_absolute() else p


def xdg_data_home() -> Path:
    """Return $XDG_DATA_HOME if set, else ~/.local/share (XDG Base Dir spec)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


@functools.cache
def _warn_legacy_config_dir(path: Path) -> None:
    """Warn once per process (per path) that the pre-1.0 config dir is in use.

    Cached rather than flag-guarded so repeated loads inside one dashboard
    refresh do not repeat the line.
    """
    from jailbee.tui import warn

    warn(
        f"{path.parent.name}/config.yaml is deprecated and stops working in 2.0.0 — "
        f"run `git mv {path.parent.name} .jailbee` in this repo."
    )


def repo_config_path(repo_root: Path) -> Path | None:
    """Return `repo_root`'s existing config file, or None if it has none.

    `.jailbee/config.yaml` wins over the deprecated `.gie/config.yaml`.
    """
    for name in REPO_CONFIG_DIRS:
        candidate = repo_root / name / "config.yaml"
        if candidate.is_file():
            return candidate
    return None


def repo_config_dir_name(repo_root: Path) -> str:
    """Return the config directory name `repo_root` uses.

    An existing directory wins, so writers keep a legacy repo consistent
    instead of scattering config across both names; a repo with neither gets
    the current name.
    """
    for name in REPO_CONFIG_DIRS:
        if (repo_root / name / "config.yaml").is_file():
            return name
    return REPO_CONFIG_DIRS[0]


def find_repo_config() -> Path:
    """Return the repo config path in CWD, or raise ConfigNotFoundError.

    Prefers ``.jailbee/config.yaml``, accepts ``.gie/config.yaml`` with a
    deprecation warning. No walk-up: the file must live directly under the
    current directory.
    """
    # Local import avoids circular import (config.py imports from paths).
    from jailbee.config import ConfigNotFoundError

    cwd = Path.cwd()
    candidate = repo_config_path(cwd)
    if candidate is not None:
        if candidate.parent.name != REPO_CONFIG_DIRS[0]:
            _warn_legacy_config_dir(candidate)
        return candidate
    raise ConfigNotFoundError(
        "No .jailbee/config.yaml found in current directory.\n"
        "jailbee must be run from a repo root containing .jailbee/config.yaml.\n"
        "Run `jailbee config init` to create a template."
    )
