"""Detect child containers without host-side dependencies."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ROLE_FILE = Path.home() / ".jailbee/orchestration/role"
CHILD_REFUSAL = (
    "A child container cannot stage GitHub actions. Send the orchestrator a message or question "
    "with the exact repo, issue or PR, title, body, labels, file and line instead: "
    'jailbee inbox send message --text "<exact GitHub action>"'
)


def is_child(env: Mapping[str, str] | None = None, role_file: Path | None = None) -> bool:
    if (os.environ if env is None else env).get("JAILBEE_ROLE") == "child":
        return True
    try:
        return (ROLE_FILE if role_file is None else role_file).read_text().strip() == "child"
    except FileNotFoundError:
        return False
