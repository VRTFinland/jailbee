"""Fail-closed retention checks for a child's staged GitHub writes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee import outbox_io
from jailbee.incus import IncusError
from jailbee.outbox.io import store_directory

if TYPE_CHECKING:
    from jailbee.incus import Incus
    from jailbee.outbox.models import Kind

_MAX_MANIFESTS = 128
_MAX_FILE_BYTES = 65536
_MAX_NAMES_BYTES = 524288

# Independent of the bounded tar snapshot: large/unreadable manifests still
# count. Walk directory descriptors so symlinked ancestors cannot mean empty.
_NAMES_SCRIPT = r"""
import json
import os
import sys


def names(path, cap):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in path.strip('/').split('/'):
            if not os.access('/proc/self/fd/%d' % fd, os.X_OK):
                raise OSError('unsearchable ancestor')
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                return []
            os.close(fd)
            fd = nxt
        if not os.access('/proc/self/fd/%d' % fd, os.R_OK | os.X_OK):
            raise OSError('unreadable outbox')
        result = []
        with os.scandir(fd) as entries:
            for entry in entries:
                name = entry.name
                if (name.startswith('.') or not name.endswith('.json')
                        or name.endswith('.progress.json')):
                    continue
                if entry.is_file(follow_symlinks=False):
                    result.append(name)
                    if len(result) > cap:
                        raise OSError('too many outbox manifests')
        return sorted(result)
    finally:
        os.close(fd)


try:
    cap = int(sys.argv[3])
    result = {'issue': names(sys.argv[1], cap), 'pr': names(sys.argv[2], cap)}
except OSError as exc:
    result = {'error': str(exc)}
print(json.dumps(result, ensure_ascii=True))
"""


class ChildOutboxError(Exception):
    """The child outbox could not be established safely."""


@dataclass(frozen=True)
class PendingGithub:
    issue: tuple[str, ...]
    pr: tuple[str, ...]
    summaries: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.issue) + len(self.pr)


def _names(incus: Incus, full: str, *, uid: int | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from jailbee.orchestration.actions import CHILD_EXEC_TIMEOUT_S

    try:
        raw = incus.exec_capped(
            full,
            ["python3", "-I", "-c", _NAMES_SCRIPT, store_directory("issue"),
             store_directory("pr"), str(_MAX_MANIFESTS)],
            uid=uid,
            timeout=CHILD_EXEC_TIMEOUT_S,
            max_bytes=_MAX_NAMES_BYTES,
        )
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"issue", "pr"}:
            raise ValueError("unknown outbox state")
        result = []
        for kind in ("issue", "pr"):
            names = data[kind]
            if not isinstance(names, list) or len(names) > _MAX_MANIFESTS:
                raise ValueError("invalid outbox names")
            if any(
                not isinstance(name, str) or not name.endswith(".json")
                or name.startswith(".") or name.endswith(".progress.json")
                or "/" in name or "\0" in name or len(name) > 255
                for name in names
            ):
                raise ValueError("invalid outbox name")
            if len(set(names)) != len(names):
                raise ValueError("duplicate outbox name")
            result.append(tuple(sorted(names)))
        return result[0], result[1]
    except (IncusError, ValueError, TypeError, RecursionError) as exc:
        raise ChildOutboxError(f"outbox could not be checked: {exc}") from exc


def _summary(kind: str, name: str, text: str | None) -> str:
    prefix = f"{kind}/{name}"
    if text is None:
        return prefix
    try:
        data = json.loads(text)
        actions = data["actions"]
        if not isinstance(actions, list) or not actions:
            return prefix
        action = actions[0]
        verb = action["type"]
        if not isinstance(verb, str):
            return prefix
        detail = verb[:40]
        if verb == "create" and isinstance(action.get("title"), str):
            detail += " " + json.dumps(action["title"][:200], ensure_ascii=True)
        elif verb == "review" and isinstance(action.get("comments"), list):
            detail += f" ({len(action['comments'])} comments)"
        if len(actions) > 1:
            detail += f" (+{len(actions) - 1} actions)"
        return f"{prefix}: {detail}"
    except (ValueError, KeyError, TypeError, IndexError, RecursionError):
        return prefix


def probe(incus: Incus, full: str, *, uid: int | None) -> PendingGithub:
    """Count names independently; snapshots provide best-effort summaries only.

    The caller supplies an Incus wrapper bounded by CHILD_EXEC_TIMEOUT_S.
    Recheck names after reading to reject a changing snapshot rather than
    declaring a newly populated store empty.
    """
    from jailbee.orchestration.actions import CHILD_EXEC_TIMEOUT_S

    issue, pr = _names(incus, full, uid=uid)
    summaries: list[str] = []
    stores: tuple[tuple[Kind, tuple[str, ...]], ...] = (("issue", issue), ("pr", pr))
    try:
        for kind, names in stores:
            files = outbox_io.read_text_outbox(
                incus, full, store_directory(kind), uid=uid,
                max_file_bytes=_MAX_FILE_BYTES, timeout=CHILD_EXEC_TIMEOUT_S,
                select=[("*.json", _MAX_MANIFESTS)],
            )
            summaries.extend(_summary(kind, name, files.get(name)) for name in names)
    except outbox_io.OutboxReadError as exc:
        raise ChildOutboxError(f"outbox could not be checked: {exc}") from exc
    if (issue, pr) != _names(incus, full, uid=uid):
        raise ChildOutboxError("outbox could not be checked: manifests changed during inspection")
    return PendingGithub(issue, pr, tuple(summaries))


def describe(child_short: str, p: PendingGithub) -> str:
    groups = []
    if p.issue:
        groups.append("issue: " + ", ".join(p.issue))
    if p.pr:
        groups.append("pr: " + ", ".join(p.pr))
    return describe_count(child_short, p.count, "; ".join(groups))


def describe_count(child_short: str, count: int, names: str = "") -> str:
    listing = f" ({names})" if names else ""
    return (
        f"child `{child_short}` has {count} staged GitHub action(s) in its outbox{listing}; "
        "they are never published from a child — ask it to send the content, "
        "then destroy with force: true"
    )


def unchecked(child_short: str) -> str:
    return (
        f"child `{child_short}` outbox could not be checked; restart it so its outbox "
        "can be checked (or use force: true to destroy anyway)"
    )
