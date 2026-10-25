"""Bounded text snapshots of container outbox directories."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import math
import os
import re
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import local
from typing import Literal, TypeGuard

from jailbee.db import state_dir
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


@dataclass(frozen=True)
class JournalKey:
    """Logical address of one manifest's host-side progress journal."""

    identity: ContainerIdentity
    manifest_name: str


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


_JOURNAL_SCHEMA = 1
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ZERO_CREATED_AT_PREFIX = "0001-01-01T00:00:00"
_RECOVERED_DETAIL = "a previous run prepared this action; its outcome is uncertain"
_MAX_DETAIL_LENGTH = 240
_DEFAULT_UNCERTAIN_DETAIL = "GitHub mutation outcome is uncertain"
_SAFE_UNCERTAIN_DETAILS = frozenset(
    {
        _DEFAULT_UNCERTAIN_DETAIL,
        "GitHub mutation was interrupted after dispatch",
        "GitHub mutation transport failed after dispatch",
        "GitHub mutation succeeded but returned an unreadable response",
        "GitHub mutation returned an unusable response",
    }
)
_SAFE_STORED_DETAILS = _SAFE_UNCERTAIN_DETAILS | {_RECOVERED_DETAIL}


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


def issue_receipt_line(manifest_name: str, action: JournalAction, timestamp: str) -> str:
    """Canonical issue receipt bytes, shared by writing and recovery proof."""
    return json.dumps(
        {
            "timestamp": timestamp,
            "manifest": manifest_name,
            "index": action.index,
            "repo": action.repo,
            "issue": action.issue,
            "url": action.url,
        },
        sort_keys=True,
    )


def issue_proposal_digest(
    key: JournalKey,
    manifest_text: str,
    body_files: Mapping[str, str],
    journal: IssueJournal | None,
) -> str:
    """Prove complete owned receipt suffixes reconstruct the original proposal.

    No input is excluded from the digest. Only exact canonical, fully settled
    journal receipt blocks may be removed, and only a matching original hash
    authorizes cleanup-only recovery. Partial or unproved suffixes stay changed.
    """
    raw_digest = proposal_digest(key.manifest_name, manifest_text, body_files)
    if (
        journal is None
        or journal.identity != key.identity
        or journal.manifest_name != key.manifest_name
        or journal.action_count <= 0
        or len(journal.actions) != journal.action_count
        or any(action.index != index for index, action in enumerate(journal.actions))
        or any(a.state != "applied" for a in journal.actions)
        or "applied.log" not in body_files
        or raw_digest == journal.digest
    ):
        return raw_digest
    text = body_files["applied.log"]
    candidate = text
    while candidate.endswith("\n"):
        end = len(candidate)
        for action in reversed(journal.actions):
            # The first appended JSON can abut original text without a newline.
            # Derive its exact length from the durable fields and fixed timestamp.
            template = issue_receipt_line(key.manifest_name, action, "2000-01-01T00:00:00Z") + "\n"
            start = end - len(template)
            if start < 0:
                return raw_digest
            record = candidate[start:end]
            try:
                value = json.loads(record)
                if not isinstance(value, dict):
                    return raw_digest
                timestamp = value["timestamp"]
                if not isinstance(timestamp, str):
                    return raw_digest
                parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
                if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != timestamp:
                    return raw_digest
            except (ValueError, TypeError, KeyError, RecursionError):
                return raw_digest
            if record != issue_receipt_line(key.manifest_name, action, timestamp) + "\n":
                return raw_digest
            end = start
        candidate = candidate[:end]
        restored = dict(body_files)
        restored["applied.log"] = candidate
        if proposal_digest(key.manifest_name, manifest_text, restored) == journal.digest:
            return journal.digest
    return raw_digest


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


def journal_key(identity: ContainerIdentity, manifest_name: str) -> JournalKey:
    """Build the logical key for one manifest journal."""
    if not identity.full_name or not identity.created_at or not manifest_name:
        raise JournalError("journal identity and manifest name must be non-empty")
    return JournalKey(identity=identity, manifest_name=manifest_name)


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _exact_fields(value: dict[str, object], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise JournalError(f"invalid journal {context} schema")


def safe_mutation_detail(detail: str) -> str:
    """Map a mutation error message to one of a known-safe, fixed set.

    ``IssueGithubMutationError`` messages are, in the real adapter, always one
    of a small closed set of fixed literals (see ``issue_github.py``) that
    never embed ``gh``'s stderr, a payload, or anything else that could carry
    a secret. This function is the one place that trust boundary is
    enforced: an unrecognized message — which can only happen if a caller
    passes something else, e.g. a raw exception string in a test — is mapped
    to a generic default rather than stored or shown verbatim. Used both by
    `JournalStore.mark_uncertain` (the persisted detail) and by
    `issue_outbox` (the in-memory `ApplyFailure.detail`), so both surfaces
    share one definition of "safe".
    """
    return detail if detail in _SAFE_UNCERTAIN_DETAILS else _DEFAULT_UNCERTAIN_DETAIL


def journal_has_uncertainty(journal: IssueJournal) -> bool:
    """Whether a recovered journal (one already returned by `.load()`) has any
    action whose GitHub outcome is unknown.

    Callers that read a journal through `.load()` never see a raw
    ``"prepared"`` state — `.load()` already recovers those to
    ``"uncertain"`` — so checking for ``"uncertain"`` alone is exhaustive
    here. `JournalStore.archive` is the one place that instead reads a
    journal's *raw* state directly (via `_required_raw`, bypassing that
    recovery) and so keeps its own, broader check for both ``"prepared"``
    and ``"uncertain"``.
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


class JournalStore:
    """Crash-safe host storage for issue mutation progress."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else state_dir() / "issue-outbox"
        self._lock_state = local()

    @staticmethod
    def _container_directory(identity: ContainerIdentity) -> str:
        raw = f"{identity.full_name}\0{identity.created_at}".encode()
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _manifest_filename(manifest_name: str) -> str:
        encoded = base64.urlsafe_b64encode(manifest_name.encode()).decode().rstrip("=")
        return f"{encoded}.json"

    def _path(self, key: JournalKey) -> Path:
        return (
            self.root
            / self._container_directory(key.identity)
            / self._manifest_filename(key.manifest_name)
        )

    def _lock_path(self, key: JournalKey) -> Path:
        journal_path = self._path(key)
        return journal_path.with_name(f"{journal_path.name}.lock")

    def _held_lock_paths(self) -> set[Path]:
        held = getattr(self._lock_state, "paths", None)
        if held is None:
            held = set()
            self._lock_state.paths = held
        return held

    @contextmanager
    def lock(self, key: JournalKey, *, timeout: float | None = None) -> Iterator[None]:
        """Serialize one journal; None waits indefinitely, finite seconds bound contention."""
        path = self._lock_path(key)
        held = self._held_lock_paths()
        if path in held:
            yield
            return
        descriptor: int | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.chmod(path, 0o600)
            acquire_outbox_lock(descriptor, timeout)
        except (OSError, ValueError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            if isinstance(exc, TimeoutError):
                raise JournalError(
                    f"Timed out waiting for journal lock for {key.manifest_name}; "
                    "refresh and retry after the current operation finishes"
                ) from exc
            if isinstance(exc, ValueError):
                raise JournalError(str(exc)) from exc
            raise JournalError(f"could not lock journal for {key.manifest_name}") from exc
        assert descriptor is not None
        held.add(path)
        try:
            yield
        finally:
            held.remove(path)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write(self, key: JournalKey, journal: IssueJournal) -> None:
        path = self._path(key)
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            document = {
                "schema": _JOURNAL_SCHEMA,
                "identity": {
                    "full_name": journal.identity.full_name,
                    "created_at": journal.identity.created_at,
                },
                "manifest_name": journal.manifest_name,
                "digest": journal.digest,
                "action_count": journal.action_count,
                "actions": [
                    {
                        "index": action.index,
                        "state": action.state,
                        "repo": action.repo,
                        "url": action.url,
                        "issue": action.issue,
                        "detail": action.detail,
                    }
                    for action in journal.actions
                ],
            }
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump(document, output, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            temporary = None
            self._fsync_directory(path.parent)
        except (OSError, TypeError, ValueError) as exc:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise JournalError(f"could not write journal for {key.manifest_name}") from exc

    @staticmethod
    def _validate_digest(digest: object) -> str:
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise JournalError("invalid journal digest")
        return digest

    @staticmethod
    def _parse_action(value: object, *, action_count: int) -> JournalAction:
        if not _is_object(value):
            raise JournalError("invalid journal action schema")
        _exact_fields(value, {"index", "state", "repo", "url", "issue", "detail"}, "action")
        index = value["index"]
        state = value["state"]
        repo = value["repo"]
        url = value["url"]
        issue = value["issue"]
        detail = value["detail"]
        if type(index) is not int or not 0 <= index < action_count:
            raise JournalError("journal action index is out of bounds")
        if state not in ("prepared", "applied", "uncertain"):
            raise JournalError("invalid journal action state")
        if not isinstance(repo, str) or not repo:
            raise JournalError("invalid journal action repo")
        if url is not None and (not isinstance(url, str) or not url):
            raise JournalError("invalid journal action URL")
        if issue is not None and (type(issue) is not int or issue <= 0):
            raise JournalError("invalid journal action issue number")
        if detail is not None and (
            not isinstance(detail, str)
            or not detail
            or "\n" in detail
            or len(detail) > _MAX_DETAIL_LENGTH
        ):
            raise JournalError("invalid journal action detail")
        if state == "prepared" and (url is not None or issue is not None or detail is not None):
            raise JournalError("invalid prepared journal action")
        if state == "applied" and (url is None or detail is not None):
            raise JournalError("invalid applied journal action")
        if state == "uncertain" and (
            url is not None or issue is not None or detail not in _SAFE_STORED_DETAILS
        ):
            raise JournalError("invalid uncertain journal action detail")
        return JournalAction(
            index=index,
            state=state,
            repo=repo,
            url=url,
            issue=issue,
            detail=detail,
        )

    def _load_raw(self, key: JournalKey) -> IssueJournal | None:
        path = self._path(key)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise JournalError(f"could not read journal for {key.manifest_name}") from exc
        try:
            value: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JournalError(f"invalid journal JSON for {key.manifest_name}") from exc
        if not _is_object(value):
            raise JournalError("invalid journal schema")
        _exact_fields(
            value,
            {"schema", "identity", "manifest_name", "digest", "action_count", "actions"},
            "document",
        )
        if type(value["schema"]) is not int or value["schema"] != _JOURNAL_SCHEMA:
            raise JournalError("unsupported journal schema")
        raw_identity = value["identity"]
        if not _is_object(raw_identity):
            raise JournalError("invalid journal identity schema")
        _exact_fields(raw_identity, {"full_name", "created_at"}, "identity")
        full_name = raw_identity["full_name"]
        created_at = raw_identity["created_at"]
        if not isinstance(full_name, str) or not isinstance(created_at, str):
            raise JournalError("invalid journal identity")
        identity = ContainerIdentity(full_name=full_name, created_at=created_at)
        if identity != key.identity:
            raise JournalError("journal identity does not match its key")
        manifest_name = value["manifest_name"]
        if not isinstance(manifest_name, str) or manifest_name != key.manifest_name:
            raise JournalError("journal manifest name does not match its key")
        digest = self._validate_digest(value["digest"])
        action_count = value["action_count"]
        if type(action_count) is not int or action_count < 0:
            raise JournalError("invalid journal action_count")
        raw_actions = value["actions"]
        if not isinstance(raw_actions, list):
            raise JournalError("invalid journal actions schema")
        actions = tuple(
            self._parse_action(action, action_count=action_count) for action in raw_actions
        )
        indices = [action.index for action in actions]
        if indices != sorted(set(indices)):
            raise JournalError("journal action indices must be unique and ordered")
        return IssueJournal(
            identity=identity,
            manifest_name=manifest_name,
            digest=digest,
            action_count=action_count,
            actions=actions,
        )

    @staticmethod
    def _recover_prepared(journal: IssueJournal) -> IssueJournal:
        actions = tuple(
            replace(action, state="uncertain", detail=_RECOVERED_DETAIL)
            if action.state == "prepared"
            else action
            for action in journal.actions
        )
        return replace(journal, actions=actions)

    def load(self, key: JournalKey) -> IssueJournal | None:
        """Load validated progress, conservatively viewing prepared as uncertain."""
        journal = self._load_raw(key)
        return None if journal is None else self._recover_prepared(journal)

    def create(self, key: JournalKey, digest: str, action_count: int) -> IssueJournal:
        """Create an empty journal or return the identical existing one."""
        with self.lock(key):
            digest = self._validate_digest(digest)
            if type(action_count) is not int or action_count < 0:
                raise JournalError("invalid journal action_count")
            existing = self._load_raw(key)
            if existing is not None:
                if existing.digest != digest:
                    raise JournalError("existing journal digest does not match this proposal")
                if existing.action_count != action_count:
                    raise JournalError("existing journal action_count does not match this proposal")
                return self._recover_prepared(existing)
            journal = IssueJournal(
                identity=key.identity,
                manifest_name=key.manifest_name,
                digest=digest,
                action_count=action_count,
                actions=(),
            )
            self._write(key, journal)
            return journal

    def _required_raw(self, key: JournalKey) -> IssueJournal:
        journal = self._load_raw(key)
        if journal is None:
            raise JournalError(f"journal for {key.manifest_name} does not exist")
        return journal

    @staticmethod
    def _checked_index(journal: IssueJournal, index: int) -> None:
        if type(index) is not int or not 0 <= index < journal.action_count:
            raise JournalError("journal action index is out of bounds")

    @staticmethod
    def _action_at(journal: IssueJournal, index: int) -> JournalAction | None:
        return next((action for action in journal.actions if action.index == index), None)

    @staticmethod
    def _with_action(
        journal: IssueJournal, action: JournalAction | None, index: int
    ) -> IssueJournal:
        actions = [existing for existing in journal.actions if existing.index != index]
        if action is not None:
            actions.append(action)
        return replace(journal, actions=tuple(sorted(actions, key=lambda item: item.index)))

    def mark_prepared(self, key: JournalKey, index: int, *, repo: str) -> IssueJournal:
        with self.lock(key):
            journal = self._required_raw(key)
            self._checked_index(journal, index)
            if not repo:
                raise JournalError("journal action repo must be non-empty")
            if self._action_at(journal, index) is not None:
                raise JournalError("illegal journal transition to prepared")
            updated = self._with_action(
                journal,
                JournalAction(index=index, state="prepared", repo=repo),
                index,
            )
            self._write(key, updated)
            return updated

    def mark_applied(
        self,
        key: JournalKey,
        index: int,
        *,
        repo: str,
        url: str,
        issue: int | None,
    ) -> IssueJournal:
        with self.lock(key):
            journal = self._required_raw(key)
            self._checked_index(journal, index)
            current = self._action_at(journal, index)
            if current is None or current.state != "prepared":
                raise JournalError("illegal journal transition to applied")
            if current.repo != repo:
                raise JournalError("journal action repo does not match prepared state")
            action = JournalAction(index=index, state="applied", repo=repo, url=url, issue=issue)
            self._parse_action(
                {
                    "index": action.index,
                    "state": action.state,
                    "repo": action.repo,
                    "url": action.url,
                    "issue": action.issue,
                    "detail": action.detail,
                },
                action_count=journal.action_count,
            )
            updated = self._with_action(journal, action, index)
            self._write(key, updated)
            return updated

    def mark_uncertain(
        self,
        key: JournalKey,
        index: int,
        *,
        repo: str,
        detail: str,
    ) -> IssueJournal:
        with self.lock(key):
            journal = self._required_raw(key)
            self._checked_index(journal, index)
            current = self._action_at(journal, index)
            if current is None or current.state != "prepared":
                raise JournalError("illegal journal transition to uncertain")
            if current.repo != repo:
                raise JournalError("journal action repo does not match prepared state")
            updated = self._with_action(
                journal,
                JournalAction(
                    index=index,
                    state="uncertain",
                    repo=repo,
                    detail=safe_mutation_detail(detail),
                ),
                index,
            )
            self._write(key, updated)
            return updated

    def clear_prepared(self, key: JournalKey, index: int) -> IssueJournal:
        with self.lock(key):
            journal = self._required_raw(key)
            self._checked_index(journal, index)
            current = self._action_at(journal, index)
            if current is None or current.state != "prepared":
                raise JournalError("illegal journal transition while clearing prepared action")
            updated = self._with_action(journal, None, index)
            self._write(key, updated)
            return updated

    def _required_uncertain(self, key: JournalKey, index: int) -> IssueJournal:
        raw = self._required_raw(key)
        journal = self._recover_prepared(raw)
        self._checked_index(journal, index)
        current = self._action_at(journal, index)
        if current is None or current.state != "uncertain":
            raise JournalError("illegal journal transition from non-uncertain action")
        return journal

    def resolve_applied(
        self,
        key: JournalKey,
        index: int,
        *,
        url: str,
        issue: int | None,
    ) -> IssueJournal:
        with self.lock(key):
            journal = self._required_uncertain(key, index)
            current = self._action_at(journal, index)
            assert current is not None
            action = JournalAction(
                index=index,
                state="applied",
                repo=current.repo,
                url=url,
                issue=issue,
            )
            self._parse_action(
                {
                    "index": action.index,
                    "state": action.state,
                    "repo": action.repo,
                    "url": action.url,
                    "issue": action.issue,
                    "detail": action.detail,
                },
                action_count=journal.action_count,
            )
            updated = self._with_action(journal, action, index)
            self._write(key, updated)
            return updated

    def resolve_retry(self, key: JournalKey, index: int) -> IssueJournal:
        with self.lock(key):
            journal = self._required_uncertain(key, index)
            updated = self._with_action(journal, None, index)
            self._write(key, updated)
            return updated

    def archive(self, key: JournalKey) -> Path:
        """Move a journal with no unknown outcomes into immutable history."""
        with self.lock(key):
            journal = self._required_raw(key)
            if any(action.state in ("prepared", "uncertain") for action in journal.actions):
                raise JournalError("cannot archive a journal containing uncertain progress")
            source = self._path(key)
            archive_dir = source.parent / "archive"
            encoded_name = self._manifest_filename(key.manifest_name).removesuffix(".json")
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            stem = f"{encoded_name}.{journal.digest}.{timestamp}"
            destination: Path | None = None
            moved = False
            try:
                archive_dir.mkdir(parents=True, exist_ok=True)
                descriptor, reserved = tempfile.mkstemp(
                    dir=archive_dir,
                    prefix=f"{stem}.",
                    suffix=".json",
                )
                os.close(descriptor)
                destination = Path(reserved)
                os.replace(source, destination)
                moved = True
                self._fsync_directory(archive_dir)
                self._fsync_directory(source.parent)
            except OSError as exc:
                if destination is not None and not moved:
                    try:
                        destination.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise JournalError(f"could not archive journal for {key.manifest_name}") from exc
            assert destination is not None
            return destination


_TAR_ALL = 'cd "$1" 2>/dev/null || exit 0; tar -cf - . | base64 -w0'
# Values arrive as positional parameters only: $1 directory, $2 per-file cap + 1
# (find's `-size -Nc` is "fewer than N bytes"), $3 archive byte cap, then
# (glob, count) pairs: the first `count` regular top-level files matching each
# glob, by name.
_TAR_BOUNDED = (
    'cd "$1" 2>/dev/null || exit 0; size="$2"; total="$3"; shift 3; '
    '{ while [ "$#" -ge 2 ]; do '
    'find . -maxdepth 1 -type f -name "$1" -size "-${size}c" -print0 '
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
            raw = incus.exec_capped(
                container, argv, uid=uid, timeout=timeout, max_bytes=max_stdout
            )
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


def append_applied_log(
    incus: Incus, container: str, directory: str, lines: Sequence[str], *, uid: int | None
) -> None:
    """Append `lines` to `directory`/applied.log inside the container.

    Every line is untrusted-ish content (it is jailbee's own construction, but
    built from journal fields that ultimately trace back to a container-
    supplied manifest), so it is never interpolated into a shell string:
    the joined text is sent base64-encoded as one argv value, and the
    ``bash -c`` script that decodes it never mentions `directory` or the
    payload itself, only the positional parameters `$1`/`$2`.
    """
    if not lines:
        return
    payload = base64.b64encode(("\n".join(lines) + "\n").encode("utf-8")).decode("ascii")
    incus.exec(
        container,
        ["bash", "-c", 'base64 -d <<<"$2" >> "$1/applied.log"', "bash", directory, payload],
        uid=uid,
    )


def delete_outbox_files(
    incus: Incus, container: str, directory: str, names: Sequence[str], *, uid: int | None
) -> None:
    """Delete `names` from `directory` inside the container.

    Every name is validated *before* anything is deleted: no empty name, no
    ``.``/``..``, no path separator, no NUL — a plain file at the outbox
    root, never a traversal or an absolute path. Names are then passed as
    plain argv elements after an ``--`` option terminator (never a shell
    string), so a name that happens to start with ``-`` cannot be mistaken
    for an `rm` flag.
    """
    for name in names:
        _validate_outbox_name(name)
    if not names:
        return
    incus.exec(container, ["rm", "-f", "--", *(f"{directory}/{name}" for name in names)], uid=uid)
