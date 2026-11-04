"""Validated, atomic local storage for container GitHub proposals and drafts."""

from __future__ import annotations

import builtins
import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from jailbee.container_outbox import role
from jailbee.issue_manifest import IssueManifestError
from jailbee.issue_manifest import parse_manifest as parse_issue
from jailbee.outbox.cleanup import exclusive_body_names
from jailbee.pr_manifest import ManifestError
from jailbee.pr_manifest import parse_manifest as parse_pr

Kind = Literal["issue", "pr"]


class OutboxCommandError(Exception):
    """A user-facing command error with its CLI exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Entry:
    kind: Kind
    name: str
    summary: str


def _plain_name(name: str) -> None:
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\0")):
        raise OutboxCommandError("name must be a plain filename", 2)


def _draft_name(name: str) -> None:
    if re.fullmatch(r"[a-z0-9-]{1,40}", name) is None:
        raise OutboxCommandError("draft name must match [a-z0-9-]{1,40}", 2)


def _refs(value: object) -> set[str]:
    if isinstance(value, dict):
        found = {value["body_file"]} if isinstance(value.get("body_file"), str) else set()
        for child in value.values():
            found.update(_refs(child))
        return found
    if isinstance(value, list):
        return set().union(*(_refs(child) for child in value))
    return set()


def _rename_refs(value: object, names: Mapping[str, str]) -> object:
    if isinstance(value, dict):
        return {
            key: names.get(child, child) if key == "body_file" and isinstance(child, str)
            else _rename_refs(child, names)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_rename_refs(child, names) for child in value]
    return value


@contextmanager
def _directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Pin each ancestor; concurrent symlink/rename cannot redirect an operation."""
    fd = -1
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as exc:
        raise OutboxCommandError(f"{path}: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)


def _read(fd: int, name: str) -> str | None:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd, "r", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OutboxCommandError(f"{name}: not a regular file")
        try:
            return stream.read()
        except UnicodeError as exc:
            raise OutboxCommandError(f"{name}: not valid UTF-8", 2) from exc


def _atomic_write(fd: int, name: str, text: str) -> None:
    temporary = f".{uuid4().hex}.tmp"
    try:
        file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        with os.fdopen(file_fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        _unlink(fd, temporary)


def _unlink(fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=fd)
    except FileNotFoundError:
        pass


def _manifest_name(name: str) -> None:
    _plain_name(name)
    if name.startswith(".") or not name.endswith(".json") or name.endswith(".progress.json"):
        raise OutboxCommandError("name must name an outbox manifest (.json)", 2)


def _is_manifest(name: str) -> bool:
    return not name.startswith(".") and name.endswith(".json") and not name.endswith(".progress.json")


def _validate(kind: Kind, name: str, manifest: dict[str, object], bodies: Mapping[str, str]) -> str:
    try:
        text = json.dumps(manifest)
        if kind == "issue":
            parse_issue(name, text, bodies)
        else:
            parse_pr(name, text, bodies)
        for body_name, body in bodies.items():
            _plain_name(body_name)
            body.encode("utf-8")
        return text
    except (IssueManifestError, ManifestError, TypeError, ValueError, UnicodeError) as exc:
        raise OutboxCommandError(str(exc), 2) from exc


class Store:
    def __init__(self, home: Path) -> None:
        self.home = home.absolute()

    def outbox(self, kind: Kind) -> Path:
        if kind not in ("issue", "pr"):
            raise OutboxCommandError("kind must be issue or pr", 2)
        return self.home / ".jailbee" / f"{kind}-outbox"

    def drafts(self, kind: Kind) -> Path:
        self.outbox(kind)
        return self.home / ".jailbee/outbox-drafts" / kind

    def _require_role(self, env: Mapping[str, str] | None = None) -> None:
        try:
            child = role.is_child(env, self.home / ".jailbee/orchestration/role")
        except OSError as exc:
            raise OutboxCommandError(f"cannot read container role: {exc}") from exc
        if child:
            raise OutboxCommandError(role.CHILD_REFUSAL, 1)

    def require_writable(self, env: Mapping[str, str] | None = None) -> None:
        self._require_role(env)
        for kind in ("issue", "pr"):
            with _directory(self.outbox(kind), create=True):
                pass

    def write_manifest(
        self, kind: Kind, slug: str, manifest: dict[str, object], bodies: Mapping[str, str]
    ) -> str:
        self._require_role()
        if re.fullmatch(r"[a-z0-9-]{1,80}", slug) is None:
            raise OutboxCommandError("slug must match [a-z0-9-]{1,80}", 2)
        _validate(kind, f"{slug}.json", manifest, bodies)
        self.require_writable()
        with _directory(self.outbox(kind)) as fd:
            numbers = [
                int(match[1]) for name in os.listdir(fd)
                if (match := re.match(r"\.?([0-9]+)-", name))
            ]
            number = max(numbers, default=0) + 1
            while True:
                stem = f"{number:03d}-{slug}"
                reservation = f".{stem}.reserve"
                try:
                    reserved_fd = os.open(
                        reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd
                    )
                except FileExistsError:
                    number += 1
                    continue
                os.close(reserved_fd)
                if f"{stem}.json" in os.listdir(fd):
                    _unlink(fd, reservation)
                    number += 1
                    continue
                break
            written: builtins.list[str] = []
            try:
                names = {body_name: f"{stem}-{index}.md" for index, body_name in enumerate(bodies, 1)}
                rewritten = cast(dict[str, object], _rename_refs(manifest, names))
                renamed_bodies = {names[key]: body for key, body in bodies.items()}
                text = _validate(kind, f"{stem}.json", rewritten, renamed_bodies)
                for name, body in renamed_bodies.items():
                    _atomic_write(fd, name, body)
                    written.append(name)
                _atomic_write(fd, f"{stem}.json", text)
            except BaseException:
                for name in written:
                    _unlink(fd, name)
                raise
            finally:
                _unlink(fd, reservation)
            return f"{stem}.json"

    def list(self) -> builtins.list[Entry]:
        entries = []
        kinds: tuple[Kind, ...] = ("issue", "pr")
        for domain in kinds:
            path = self.outbox(domain)
            if not path.exists() and not path.is_symlink():
                continue
            with _directory(path) as fd:
                for name in sorted(filter(_is_manifest, os.listdir(fd))):
                    text = _read(fd, name)
                    if text is None:
                        continue
                    try:
                        raw = json.loads(text)
                        summary = ", ".join(str(action.get("type", "?")) for action in raw["actions"])
                    except (ValueError, KeyError, TypeError, AttributeError):
                        summary = "invalid manifest"
                    entries.append(Entry(domain, name, summary or "no actions"))
        return entries

    def show(self, kind: Kind, name: str) -> str:
        _manifest_name(name)
        with _directory(self.outbox(kind)) as fd:
            text = _read(fd, name)
            if text is None:
                raise OutboxCommandError(f"manifest {name!r} not found", 2)
            return text

    def drop(self, kind: Kind, name: str) -> None:
        self._require_role()
        _manifest_name(name)
        with _directory(self.outbox(kind)) as fd:
            text = _read(fd, name)
            if text is None:
                raise OutboxCommandError(f"manifest {name!r} not found", 2)
            try:
                candidates = _refs(json.loads(text))
            except ValueError:
                candidates = set()
            referenced: set[str] = set()
            present = os.listdir(fd)
            for other in filter(_is_manifest, present):
                if other == name:
                    continue
                try:
                    referenced.update(_refs(json.loads(_read(fd, other) or "")))
                except ValueError:
                    # Unknown references in a corrupt sibling: retain all candidate bodies.
                    referenced.update(candidates)
            _unlink(fd, name)
            for body in exclusive_body_names(candidates, present, referenced):
                if not body.startswith("."):
                    _unlink(fd, body)

    def _draft(self, kind: Kind, name: str) -> dict[str, object] | None:
        _draft_name(name)
        path = self.drafts(kind)
        if not path.exists() and not path.is_symlink():
            return None
        with _directory(path) as fd:
            text = _read(fd, f"{name}.json")
        if text is None:
            return None
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict) or not isinstance(raw.get("manifest"), dict):
                raise ValueError("manifest must be an object")
            bodies = raw.get("bodies")
            if not isinstance(bodies, dict) or not all(
                isinstance(key, str) and isinstance(body, str) for key, body in bodies.items()
            ):
                raise ValueError("bodies must map filenames to text")
            return cast(dict[str, object], raw)
        except ValueError as exc:
            raise OutboxCommandError(f"draft {name!r}: {exc}", 2) from exc

    def draft_load(self, kind: Kind, name: str) -> dict[str, object] | None:
        draft = self._draft(kind, name)
        return None if draft is None else cast(dict[str, object], draft["manifest"])

    def draft_load_state(
        self, kind: Kind, name: str
    ) -> tuple[dict[str, object], dict[str, str]] | None:
        """Load one atomic snapshot, including bodies needed by a later save."""
        draft = self._draft(kind, name)
        if draft is None:
            return None
        return cast(dict[str, object], draft["manifest"]), cast(dict[str, str], draft["bodies"])

    @contextmanager
    def _draft_lock(self, kind: Kind, name: str) -> Iterator[None]:
        _draft_name(name)
        self.outbox(kind)
        path = self.home / ".jailbee/outbox-draft-locks" / kind
        with _directory(path, create=True) as directory_fd:
            fd = os.open(
                f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600, dir_fd=directory_fd,
            )
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OutboxCommandError(f"draft {name!r}: lock is not a regular file")
                # Keep the lock inode after unlock so waiters never lock a stale inode.
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def draft_save(
        self, kind: Kind, name: str, manifest: dict[str, object], bodies: Mapping[str, str]
    ) -> None:
        self._require_role()
        _draft_name(name)
        for body_name in bodies:
            _plain_name(body_name)
        try:
            text = json.dumps({"manifest": manifest, "bodies": dict(bodies)})
        except (TypeError, ValueError) as exc:
            raise OutboxCommandError(str(exc), 2) from exc
        self.require_writable()
        with self._draft_lock(kind, name):
            with _directory(self.drafts(kind), create=True) as fd:
                _atomic_write(fd, f"{name}.json", text)

    def draft_submit(self, kind: Kind, name: str, slug: str) -> str:
        self._require_role()
        with self._draft_lock(kind, name):
            draft = self._draft(kind, name)
            if draft is None:
                raise OutboxCommandError(f"draft {name!r} not found", 2)
            result = self.write_manifest(
                kind, slug, cast(dict[str, object], draft["manifest"]),
                cast(Mapping[str, str], draft["bodies"]),
            )
            with _directory(self.drafts(kind)) as fd:
                _unlink(fd, f"{name}.json")
            return result

    def draft_discard(self, kind: Kind, name: str) -> None:
        self._require_role()
        _draft_name(name)
        with self._draft_lock(kind, name):
            path = self.drafts(kind)
            if not path.exists() and not path.is_symlink():
                return
            with _directory(path) as fd:
                _unlink(fd, f"{name}.json")
