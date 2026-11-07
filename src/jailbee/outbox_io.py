"""Bounded text snapshots of container outbox directories."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import math
import tarfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeGuard

from jailbee.incus import Incus, IncusError
from jailbee.tui import warn


class OutboxReadError(Exception):
    """A container outbox could not be read into a bounded text snapshot."""


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


def instance_uuid(incus: Incus, container: str) -> str:
    """The instance's Incus ``volatile.uuid``: the outbox store's key for it."""
    raw = next(
        (item for item in incus.list_containers(fast=True) if item.get("name") == container), None
    )
    config = raw.get("config") if raw is not None else None
    uuid = config.get("volatile.uuid") if isinstance(config, dict) else None
    if not isinstance(uuid, str) or not uuid:
        raise JournalError(f"{container}: instance uuid is unavailable")
    return uuid


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


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


_TAR_ALL = 'cd "$1" 2>/dev/null || exit 0; tar -cf - . | base64 -w0'
# Values arrive as positional parameters only: $1 directory, $2 per-file cap + 1
# (find's `-size -Nc` is "fewer than N bytes"), $3 archive byte cap, then
# (glob, count) pairs: the first `count` regular top-level files matching each
# glob, by name. Dot files (a writer's temp files) never count.
_TAR_BOUNDED = (
    'cd "$1" 2>/dev/null || exit 0; size="$2"; total="$3"; shift 3; '
    '{ while [ "$#" -ge 2 ]; do '
    'find . -maxdepth 1 -type f -name "$1" ! -name \'.*\' -size "-${size}c" -print0 '
    '| LC_ALL=C sort -z | head -z -n "$2"; shift 2; done; } '
    '| tar --null --no-recursion -T - -cf - | head -c "$total" | base64 -w0'
)
# Per member: header, a GNU long-name record for names up to 255 bytes, block
# padding; then the end-of-archive record (one 10 KiB tar record).
_TAR_MEMBER_OVERHEAD = 2048
_TAR_TRAILER = 10240


def read_text_outbox(
    incus: Incus,
    container: str,
    directory: str,
    *,
    uid: int | None,
    max_file_bytes: int,
    timeout: int = 15,
    warn_fn: Callable[[str], None] = warn,
    select: Sequence[tuple[str, int]] | None = None,
) -> dict[str, str]:
    """Read regular UTF-8 files from one container directory in one round trip.

    With ``select`` — ``(glob, count)`` pairs — the container side is bounded
    too: per pair, the first ``count`` regular top-level files matching
    ``glob`` (by name) of at most ``max_file_bytes`` are archived, and the
    archive is cut at a total those files can never exceed. Without it the
    whole directory is archived and filtered on the host.
    """
    # Bounded reads only: the archive member cap and the captured-stdout cap.
    max_members: int | None = None
    max_stdout: int | None = None
    if select is None:
        argv = ["bash", "-c", _TAR_ALL, "bash", directory]
    else:
        count = 0
        pairs: list[str] = []
        for glob, n in select:
            if not glob or "/" in glob or n < 1:
                raise ValueError(f"invalid outbox selection: {glob!r} x {n}")
            count += n
            pairs += [glob, str(n)]
        total = count * (max_file_bytes + _TAR_MEMBER_OVERHEAD) + _TAR_TRAILER
        argv = [
            "bash",
            "-c",
            _TAR_BOUNDED,
            "bash",
            directory,
            str(max_file_bytes + 1),
            str(total),
            *pairs,
        ]
        max_members = count
        # The container is not trusted to honour its own `head -c`: the host
        # stops reading at the base64 size of the archive cap.
        max_stdout = 4 * math.ceil(total / 3)
    try:
        if max_stdout is None:
            raw = incus.exec(container, argv, uid=uid, timeout=timeout)
        else:
            raw = incus.exec_capped(container, argv, uid=uid, timeout=timeout, max_bytes=max_stdout)
    except IncusError as e:
        raise OutboxReadError(f"could not read the outbox in {container}: {e}") from e
    if not raw.strip():
        return {}
    try:
        blob = base64.b64decode(raw.strip(), validate=True)
    except ValueError as e:  # binascii.Error (invalid base64) is a ValueError subclass
        raise OutboxReadError(f"{container} returned an unreadable outbox archive") from e

    files: dict[str, str] = {}
    skipped = 0
    # Bounded reads open plain tar only ("r:"): the container-side script
    # never compresses, and a small compressed archive can expand enormously.
    # Members are iterated lazily and counted, never collected up front.
    mode: Literal["r:*", "r:"] = "r:*" if max_members is None else "r:"
    members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode=mode) as tar:
            for member in tar:
                name = member.name
                if name == "." and member.isdir():
                    continue
                members += 1
                if max_members is not None and members > max_members:
                    raise OutboxReadError(
                        f"{container} returned an outbox archive with more than "
                        f"{max_members} members"
                    )
                if name.startswith("./"):
                    name = name[2:]
                if (
                    not member.isfile()
                    or not name
                    or name.startswith("/")
                    or "/" in name
                    or name == ".."
                    or member.size > max_file_bytes
                ):
                    skipped += 1
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    skipped += 1
                    continue
                try:
                    files[name] = extracted.read().decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
    except tarfile.TarError as e:
        raise OutboxReadError(f"{container} returned a corrupt outbox archive: {e}") from e
    if skipped:
        warn_fn(f"{container}: skipped {skipped} hostile outbox member(s)")
    return files


def _validate_outbox_name(name: str) -> None:
    """Refuse anything but a plain, contained file name at the outbox root."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"invalid outbox file name: {name!r}")
