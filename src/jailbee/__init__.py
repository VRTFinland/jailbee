"""jailbee — manage isolated dev environments using Incus.

Originally written for the GISGRO codebase; now generic — every repo
provides its own ``.jailbee/config.yaml``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jailbee")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
