"""Shared, pure checks for editing an existing GitHub comment from an outbox.

An outbox may only rewrite a comment the publishing host account wrote, and
only if it still reads what the agent saw when it staged the edit. Both checks
and the before/after rendering live here so every comment kind (issue and PR
conversation comments now, review comments later) refuses and shows an edit
the same way. No I/O: callers fetch the `CommentSnapshot` themselves.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class CommentSnapshot:
    """The fields of one GitHub comment an edit is checked against."""

    id: int
    author: str
    body: str
    url: str
    # The issue or pull request number the comment belongs to, when known.
    parent: int | None = None


def edit_refusals(
    snapshot: CommentSnapshot,
    *,
    login: str,
    expected_body: str,
    parent: int | None = None,
    context: str,
) -> list[str]:
    """Why ``snapshot`` may not be replaced by the publisher ``login``; empty when it may.

    ``parent``, when given, is the issue or PR the edit claims the comment is on.
    """
    refusals = []
    if parent is not None and snapshot.parent != parent:
        where = f"#{snapshot.parent}" if snapshot.parent is not None else "another thread"
        refusals.append(f"{context}: comment {snapshot.id} belongs to {where}, not #{parent}")
    if not login or snapshot.author.casefold() != login.casefold():
        refusals.append(
            f"{context}: comment {snapshot.id} is authored by {snapshot.author}, "
            f"not {login or 'the unresolved publishing account'}"
        )
    if snapshot.body != expected_body:
        refusals.append(f"{context}: expected.body differs from the current comment")
    return refusals


def diff_lines(before: str, after: str, *, indent: str = "    ") -> list[str]:
    """A unified, header-less line diff of a comment body, each line prefixed by ``indent``."""
    diff = difflib.unified_diff(before.split("\n"), after.split("\n"), lineterm="", n=3)
    return [f"{indent}{line}" for line in diff if not line.startswith(("---", "+++"))]
