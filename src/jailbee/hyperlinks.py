"""The one switch for OSC 8 terminal hyperlinks."""

from __future__ import annotations

import os


def hyperlinks_enabled() -> bool:
    """Whether OSC 8 links may be emitted: not disabled, not a dumb terminal."""
    if os.environ.get("JAILBEE_NO_HYPERLINKS"):
        return False
    return os.environ.get("TERM", "") != "dumb"
