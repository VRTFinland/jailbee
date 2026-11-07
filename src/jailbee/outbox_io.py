"""Shared outbox identity, journal types and digests for the host outbox store."""

from __future__ import annotations

import fcntl
import hashlib
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from jailbee.incus import Incus


class OutboxReadError(Exception):
    """A container's pending proposals could not be read from the host outbox store."""


class JournalError(Exception):
    """Host-side issue progress is missing, corrupt, or inconsistent."""


@dataclass(frozen=True)
class ContainerIdentity:
    """Stable Incus identity that does not survive instance replacement."""

    full_name: str
    created_at: str


JournalState = Literal["prepared", "applied", "uncertain"]


@dataclass(frozen=True)
class JournalAction:
    """Durable state recorded for one manifest action."""

    index: int
    state: JournalState
    repo: str
    url: str | None = None
    issue: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class IssueJournal:
    """Validated progress for one exact proposal."""

    identity: ContainerIdentity
    manifest_name: str
    digest: str
    action_count: int
    actions: tuple[JournalAction, ...]


_ZERO_CREATED_AT_PREFIX = "0001-01-01T00:00:00"
_DEFAULT_UNCERTAIN_DETAIL = "GitHub mutation outcome is uncertain"
ASSIGN_NOT_APPLIED_DETAIL = (
    "GitHub did not assign every requested assignee (not assignable by the publisher)"
)
UNASSIGN_NOT_APPLIED_DETAIL = "GitHub did not unassign every requested assignee"
_SAFE_UNCERTAIN_DETAILS = frozenset(
    {
        _DEFAULT_UNCERTAIN_DETAIL,
        ASSIGN_NOT_APPLIED_DETAIL,
        UNASSIGN_NOT_APPLIED_DETAIL,
        "GitHub mutation was interrupted after dispatch",
        "GitHub mutation transport failed after dispatch",
        "GitHub mutation succeeded but returned an unreadable response",
        "GitHub mutation returned an unusable response",
    }
)


def _hash_parts(parts: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def proposal_digest(
    manifest_name: str,
    manifest_text: str,
    body_files: Mapping[str, str],
) -> str:
    """Hash exact proposal inputs without ambiguous string concatenation."""
    parts = [manifest_name, manifest_text]
    for name in sorted(body_files):
        parts.extend((name, body_files[name]))
    return _hash_parts(tuple(parts))


def container_identity(incus: Incus, container: str) -> ContainerIdentity:
    """Return the full Incus name and raw, nonzero creation timestamp."""
    raw = next((item for item in incus.list_containers() if item.get("name") == container), None)
    if raw is None:
        raise JournalError(f"{container}: instance not found; cannot establish journal identity")
    created_at = raw.get("created_at")
    if (
        not isinstance(created_at, str)
        or not created_at
        or created_at.startswith(_ZERO_CREATED_AT_PREFIX)
    ):
        raise JournalError(f"{container}: instance creation time is unavailable")
    return ContainerIdentity(full_name=container, created_at=created_at)


def safe_mutation_detail(detail: str) -> str:
    """Map a mutation error message to one of a known-safe, fixed set.

    ``IssueGithubMutationError`` messages are, in the real adapter, always one
    of a small closed set of fixed literals (see ``issue_github.py``) that
    never embed ``gh``'s stderr, a payload, or anything else that could carry
    a secret. This function is the one place that trust boundary is
    enforced: an unrecognized message — which can only happen if a caller
    passes something else, e.g. a raw exception string in a test — is mapped
    to a generic default rather than stored or shown verbatim. Used by
    `issue_outbox` for the persisted and in-memory failure detail, so both
    surfaces share one definition of "safe".
    """
    return detail if detail in _SAFE_UNCERTAIN_DETAILS else _DEFAULT_UNCERTAIN_DETAIL


def journal_has_uncertainty(journal: IssueJournal) -> bool:
    """Whether a store-read journal has any action whose GitHub outcome is unknown.

    The store reads a leftover ``"prepared"`` action as ``"uncertain"``, so
    checking for ``"uncertain"`` alone is exhaustive.
    """
    return any(action.state == "uncertain" for action in journal.actions)


def acquire_outbox_lock(descriptor: int, timeout: float | None = None) -> None:
    """Acquire an exclusive flock; opt-in deadlines bound only lock contention.

    Callers own the descriptor and translate OSError/TimeoutError into their
    domain errors. None preserves publication's existing blocking behavior.
    """
    if timeout is None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("lock timeout must be finite non-negative seconds")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("outbox lock deadline expired") from None
            time.sleep(min(0.05, remaining))


def _validate_outbox_name(name: str) -> None:
    """Refuse anything but a plain, contained file name at the outbox root."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"invalid outbox file name: {name!r}")
