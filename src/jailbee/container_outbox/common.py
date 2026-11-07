"""What every in-container outbox command shares: its error type and the role guard.

Proposals and drafts live in the host outbox store, reached over the broker
socket (`container_outbox.broker`); nothing here touches the filesystem but
the child-role marker.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from jailbee.container_outbox import role

Kind = Literal["issue", "pr"]


class OutboxCommandError(Exception):
    """A user-facing command error with its CLI exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def require_role(env: Mapping[str, str] | None = None, *, home: Path | None = None) -> None:
    """Refuse a child container: it never stages GitHub writes of its own."""
    base = home if home is not None else Path.home()
    try:
        child = role.is_child(env, base / ".jailbee/orchestration/role")
    except OSError as exc:
        raise OutboxCommandError(f"cannot read container role: {exc}") from exc
    if child:
        raise OutboxCommandError(role.CHILD_REFUSAL, 1)
