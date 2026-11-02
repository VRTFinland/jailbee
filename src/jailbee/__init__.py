"""jailbee — manage isolated dev environments using Incus.

Originally written for the GISGRO codebase; now generic — every repo
provides its own ``.jailbee/config.yaml``.
"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version_from(package_dir: Path) -> str:
    try:
        tokens = (package_dir.parent / "VERSION").read_text().split()
        return tokens[0] if tokens else "0.0.0+unknown"
    except OSError:
        return "0.0.0+unknown"


try:
    __version__ = version("jailbee")
except PackageNotFoundError:
    __version__ = _version_from(Path(__file__).parent)
