"""Cheap local orchestration role detection."""

import os
from pathlib import Path

ROLE_FILE = Path("/opt/jailbee/orchestration/role")
LEGACY_CHILD_FILE = Path.home() / ".jailbee/orchestration/role"


def local_role() -> str:
    """Return the broker's role, falling back to legacy child markers."""
    try:
        role = ROLE_FILE.read_text().strip()
        if role in {"plain", "child", "orchestrator"}:
            return role
    except OSError:
        pass
    if os.environ.get("JAILBEE_ROLE") == "child":
        return "child"
    try:
        if LEGACY_CHILD_FILE.read_text().strip() == "child":
            return "child"
    except OSError:
        pass
    return "plain"
