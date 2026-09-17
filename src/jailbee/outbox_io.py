"""Bounded text snapshots of container outbox directories."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
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
_SECRET_RE = re.compile(
    r"(?i)(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+|"
    r"\b(?:token|authorization|password|secret)\s*(?:=|:)\s*\S+"
)
_DIAGNOSTIC_TAIL_RE = re.compile(r"(?i)\b(?:request\s+body|payload|stderr)\b")


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


def _safe_detail(detail: str) -> str:
    first_line = detail.splitlines()[0].strip() if detail.splitlines() else ""
    first_line = _DIAGNOSTIC_TAIL_RE.split(first_line, maxsplit=1)[0].rstrip(" :-")
    first_line = _SECRET_RE.sub("[redacted]", first_line)
    if not first_line:
        return "GitHub mutation outcome is uncertain"
    return first_line[:_MAX_DETAIL_LENGTH]


class JournalStore:
    """Crash-safe host storage for issue mutation progress."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else state_dir() / "issue-outbox"

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
        if state == "uncertain" and (url is not None or issue is not None or detail is None):
            raise JournalError("invalid uncertain journal action")
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
    def _with_action(journal: IssueJournal, action: JournalAction | None, index: int) -> IssueJournal:
        actions = [existing for existing in journal.actions if existing.index != index]
        if action is not None:
            actions.append(action)
        return replace(journal, actions=tuple(sorted(actions, key=lambda item: item.index)))

    def mark_prepared(self, key: JournalKey, index: int, *, repo: str) -> IssueJournal:
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
                detail=_safe_detail(detail),
            ),
            index,
        )
        self._write(key, updated)
        return updated

    def clear_prepared(self, key: JournalKey, index: int) -> IssueJournal:
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
        journal = self._required_uncertain(key, index)
        updated = self._with_action(journal, None, index)
        self._write(key, updated)
        return updated

    def archive(self, key: JournalKey) -> Path:
        """Move a journal with no unknown outcomes into immutable history."""
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


def read_text_outbox(
    incus: Incus,
    container: str,
    directory: str,
    *,
    uid: int | None,
    max_file_bytes: int,
    timeout: int = 15,
    warn_fn: Callable[[str], None] = warn,
) -> dict[str, str]:
    """Read regular UTF-8 files from one container directory in one round trip."""
    try:
        raw = incus.exec(
            container,
            [
                "bash",
                "-c",
                'cd "$1" 2>/dev/null || exit 0; tar -cf - . | base64 -w0',
                "bash",
                directory,
            ],
            uid=uid,
            timeout=timeout,
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
    try:
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            for member in tar.getmembers():
                name = member.name
                if name == "." and member.isdir():
                    continue
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
