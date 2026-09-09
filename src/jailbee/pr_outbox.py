"""Read, validate and apply PR actions a container wrote into its outbox.

The container writes JSON manifests into ``~/.jailbee/pr-outbox``; this module
reads that directory over ``incus exec``, validates what it finds against the
host's own view of the repository and the PR, renders a plan for a human, and
applies it through :mod:`jailbee.pr`.

Two architecture rules meet here and both are load-bearing:
  - this module calls no ``subprocess`` — container access goes through
    :class:`jailbee.incus.Incus`;
  - it calls no ``gh`` — every GitHub mutation is a function in
    :mod:`jailbee.pr`.

The container side is untrusted input. Everything read from it is validated
before it is shown, let alone published.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import Incus, IncusError
from jailbee.tui import warn

if TYPE_CHECKING:
    from collections.abc import Mapping

OUTBOX_SUBPATH = ".jailbee/pr-outbox"

MAX_MANIFEST_BYTES = 256 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_LINE_COMMENTS = 100
MAX_ACTIONS = 50
MAX_MANIFESTS = 20

_SIDES = ("RIGHT", "LEFT")


def outbox_dir() -> str:
    """Absolute outbox path inside a container."""
    return f"/home/{CONTAINER_USERNAME}/{OUTBOX_SUBPATH}"


class ManifestError(Exception):
    """A manifest is malformed, or asks for something v1 refuses."""


@dataclass(frozen=True)
class LineComment:
    path: str
    line: int
    body: str
    start_line: int | None = None
    side: str = "RIGHT"
    start_side: str | None = None


@dataclass(frozen=True)
class ReviewAction:
    body: str
    comments: tuple[LineComment, ...]
    event: str = "COMMENT"


@dataclass(frozen=True)
class ReplyAction:
    comment_id: int
    body: str


@dataclass(frozen=True)
class CommentAction:
    body: str
    reply_to: int | None = None


@dataclass(frozen=True)
class DescriptionAction:
    body: str
    title: str | None = None
    branch: str | None = None


Action = ReviewAction | ReplyAction | CommentAction | DescriptionAction


@dataclass(frozen=True)
class Manifest:
    name: str
    repo: str
    pr: int | None
    head_sha: str | None
    actions: tuple[Action, ...] = field(default_factory=tuple)


def _require_int(name: str, index: int, value: Any, field_name: str) -> int:
    """Return ``value`` narrowed to ``int``, refusing bool (a JSON footgun)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestError(
            f"{name} action {index}: {field_name} must be an integer, got {value!r}"
        )
    return value


def _escapes_containment(path_str: str) -> bool:
    """True if ``path_str`` is absolute or has a ``..`` path component.

    Shared by the ``body_file`` containment check (which additionally
    forbids any path separator at all — a body_file is a plain name at the
    outbox root) and the ``comments[].path`` containment check (which
    allows ``/`` separators, since a path is repo-relative).
    """
    if path_str.startswith("/"):
        return True
    return ".." in path_str.split("/")


def _resolve_body(name: str, index: int, item: dict[str, Any], bodies: Mapping[str, str]) -> str:
    """Resolve the exactly-one-of ``body``/``body_file`` pair on one action or comment.

    This is the single place the outbox-containment rule (no ``/``, no ``\\``,
    no ``..``) and the body size cap are enforced, so every caller gets both
    for free.
    """
    body_val = item.get("body")
    body_file_val = item.get("body_file")
    has_body = body_val is not None
    has_body_file = body_file_val is not None

    if has_body and has_body_file:
        raise ManifestError(
            f"{name} action {index}: exactly one of body or body_file is required, not both"
        )
    if not has_body and not has_body_file:
        raise ManifestError(f"{name} action {index}: exactly one of body or body_file is required")

    if has_body_file:
        if not isinstance(body_file_val, str):
            raise ManifestError(f"{name} action {index}: body_file must be a string")
        if "/" in body_file_val or "\\" in body_file_val or _escapes_containment(body_file_val):
            raise ManifestError(
                f"{name} action {index}: body_file {body_file_val!r} points outside the outbox"
            )
        if body_file_val not in bodies:
            raise ManifestError(
                f"{name} action {index}: body_file {body_file_val!r} has no such file in the outbox"
            )
        body = bodies[body_file_val]
    else:
        if not isinstance(body_val, str):
            raise ManifestError(f"{name} action {index}: body must be a string")
        body = body_val

    if len(body.encode()) > MAX_BODY_BYTES:
        raise ManifestError(
            f"{name} action {index}: body is larger than {MAX_BODY_BYTES // 1024} KB"
        )
    return body


def _parse_line_comment(
    name: str, index: int, item: dict[str, Any], bodies: Mapping[str, str]
) -> LineComment:
    path = item.get("path")
    if not isinstance(path, str) or not path:
        raise ManifestError(f"{name} action {index}: comment path must be a non-empty string")
    if _escapes_containment(path):
        raise ManifestError(f"{name} action {index}: path {path!r} points outside the repo")

    line = _require_int(name, index, item.get("line"), "line")

    start_line: int | None = None
    start_line_val = item.get("start_line")
    if start_line_val is not None:
        start_line = _require_int(name, index, start_line_val, "start_line")
        if start_line >= line:
            raise ManifestError(
                f"{name} action {index}: start_line {start_line} must be before line {line}"
            )

    side = item.get("side", "RIGHT")
    if side not in _SIDES:
        raise ManifestError(f"{name} action {index}: side {side!r} must be 'RIGHT' or 'LEFT'")

    start_side: str | None = None
    start_side_val = item.get("start_side")
    if start_side_val is not None:
        if start_side_val not in _SIDES:
            raise ManifestError(
                f"{name} action {index}: start_side {start_side_val!r} must be 'RIGHT' or 'LEFT'"
            )
        start_side = start_side_val

    body = _resolve_body(name, index, item, bodies)
    return LineComment(
        path=path, line=line, body=body, start_line=start_line, side=side, start_side=start_side
    )


def _parse_review(
    name: str, index: int, item: dict[str, Any], bodies: Mapping[str, str]
) -> ReviewAction:
    event = item.get("event", "COMMENT")
    if event != "COMMENT":
        raise ManifestError(
            f"{name} action {index}: event {event!r} is not supported "
            "(v1 posts comments only; approve or request changes by hand)"
        )

    body = _resolve_body(name, index, item, bodies)

    comments_val = item.get("comments")
    if not isinstance(comments_val, list):
        raise ManifestError(f"{name} action {index}: comments must be a list")
    if len(comments_val) > MAX_LINE_COMMENTS:
        raise ManifestError(
            f"{name} action {index}: review has {len(comments_val)} comments, "
            f"more than the cap of {MAX_LINE_COMMENTS}"
        )

    comments = []
    for comment_item in comments_val:
        if not isinstance(comment_item, dict):
            raise ManifestError(f"{name} action {index}: comment must be a JSON object")
        comments.append(_parse_line_comment(name, index, comment_item, bodies))

    return ReviewAction(body=body, comments=tuple(comments), event=event)


def _parse_reply(
    name: str, index: int, item: dict[str, Any], bodies: Mapping[str, str]
) -> ReplyAction:
    comment_id = _require_int(name, index, item.get("comment_id"), "comment_id")
    body = _resolve_body(name, index, item, bodies)
    return ReplyAction(comment_id=comment_id, body=body)


def _parse_comment(
    name: str, index: int, item: dict[str, Any], bodies: Mapping[str, str]
) -> CommentAction:
    body = _resolve_body(name, index, item, bodies)
    reply_to: int | None = None
    reply_to_val = item.get("reply_to")
    if reply_to_val is not None:
        reply_to = _require_int(name, index, reply_to_val, "reply_to")
    return CommentAction(body=body, reply_to=reply_to)


def _parse_description(
    name: str, index: int, item: dict[str, Any], bodies: Mapping[str, str]
) -> DescriptionAction:
    body = _resolve_body(name, index, item, bodies)

    title_val = item.get("title")
    if title_val is not None and not isinstance(title_val, str):
        raise ManifestError(f"{name} action {index}: title must be a string or null")

    branch_val = item.get("branch")
    if branch_val is not None and not isinstance(branch_val, str):
        raise ManifestError(f"{name} action {index}: branch must be a string or null")

    return DescriptionAction(body=body, title=title_val, branch=branch_val)


def _parse_actions(
    name: str, raw: dict[str, Any], bodies: Mapping[str, str], pr: int | None
) -> tuple[Action, ...]:
    actions_val = raw.get("actions")
    if not isinstance(actions_val, list):
        raise ManifestError(f"{name}: actions must be a list")
    if len(actions_val) == 0:
        raise ManifestError(f"{name} has no actions")
    if len(actions_val) > MAX_ACTIONS:
        raise ManifestError(
            f"{name} has {len(actions_val)} actions, more than the cap of {MAX_ACTIONS}"
        )

    parsed: list[Action] = []
    seen_review = False
    seen_description = False
    for index, item in enumerate(actions_val):
        if not isinstance(item, dict):
            raise ManifestError(f"{name} action {index}: must be a JSON object")
        action_type = item.get("type")
        if action_type == "review":
            if seen_review:
                raise ManifestError(
                    f"{name} action {index}: only one review action is allowed per manifest"
                )
            seen_review = True
            parsed.append(_parse_review(name, index, item, bodies))
        elif action_type == "reply":
            parsed.append(_parse_reply(name, index, item, bodies))
        elif action_type == "comment":
            parsed.append(_parse_comment(name, index, item, bodies))
        elif action_type == "description":
            if seen_description:
                raise ManifestError(
                    f"{name} action {index}: only one description action is allowed per manifest"
                )
            seen_description = True
            parsed.append(_parse_description(name, index, item, bodies))
        else:
            raise ManifestError(f"{name} action {index}: unknown action type {action_type!r}")

    if pr is None and (len(parsed) != 1 or not isinstance(parsed[0], DescriptionAction)):
        raise ManifestError(f"{name} has pr: null, so it may only carry a description")

    return tuple(parsed)


def parse_manifest(name: str, text: str, bodies: Mapping[str, str]) -> Manifest:
    """Parse one manifest, resolving every ``body_file`` from ``bodies``.

    ``bodies`` maps outbox-relative file names to their text. Resolving here
    means every action downstream carries a plain ``body`` string, and the
    containment rule (no ``..``, no absolute path, no nesting) is enforced in
    exactly one place.
    """
    if len(text.encode()) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"{name} is larger than {MAX_MANIFEST_BYTES // 1024} KB")

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ManifestError(f"{name} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError(f"{name} must be a JSON object")

    version = raw.get("version")
    if version != 1:
        raise ManifestError(
            f"{name} declares version {version!r}; this JailBee understands version 1"
        )

    repo_val = raw.get("repo")
    if not isinstance(repo_val, str) or not repo_val:
        raise ManifestError(f"{name}: repo must be a non-empty string")

    pr: int | None = None
    pr_val = raw.get("pr")
    if pr_val is not None:
        if not isinstance(pr_val, int) or isinstance(pr_val, bool):
            raise ManifestError(f"{name}: pr must be an integer or null, got {pr_val!r}")
        pr = pr_val

    head_sha_val = raw.get("head_sha")
    if head_sha_val is not None and not isinstance(head_sha_val, str):
        raise ManifestError(f"{name}: head_sha must be a string or null")

    actions = _parse_actions(name, raw, bodies, pr)

    return Manifest(name=name, repo=repo_val, pr=pr, head_sha=head_sha_val, actions=actions)


_READ_SCRIPT = 'cd "$1" 2>/dev/null || exit 0; tar -cf - . | base64 -w0'


class OutboxReadError(Exception):
    """The container's outbox could not be read."""


@dataclass(frozen=True)
class Outbox:
    files: dict[str, str]

    @property
    def manifest_names(self) -> list[str]:
        """Manifest file names, sorted. Progress sidecars are not manifests."""
        return sorted(
            n for n in self.files if n.endswith(".json") and not n.endswith(".progress.json")
        )


def _members(blob: bytes, container: str) -> dict[str, str]:
    """Extract text files from a hostile tar archive, entirely in memory.

    A member survives only if it is a regular file, its name (after
    stripping a leading ``./``) is a single path segment with no ``..`` and
    no leading ``/``, its size is within :data:`MAX_MANIFEST_BYTES`, and its
    bytes decode as UTF-8. Everything else is skipped silently; the count of
    skipped members is reported once via :func:`warn`, never per member.
    """
    files: dict[str, str] = {}
    skipped = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            for member in tar.getmembers():
                name = member.name
                if name.startswith("./"):
                    name = name[2:]
                if (
                    not member.isfile()
                    or not name
                    or "/" in name  # single path segment only, no nesting
                    or _escapes_containment(name)
                    or member.size > MAX_MANIFEST_BYTES
                ):
                    skipped += 1
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    skipped += 1
                    continue
                data = extracted.read()
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
                    continue
                files[name] = text
    except tarfile.TarError as e:
        raise OutboxReadError(f"{container} returned a corrupt outbox archive: {e}") from e
    if skipped:
        warn(f"{container}: skipped {skipped} hostile outbox member(s)")
    return files


def read_outbox(incus: Incus, container: str, *, uid: int | None) -> Outbox:
    """Read the whole outbox in one round-trip; never extracts to disk."""
    try:
        raw = incus.exec(
            container,
            ["bash", "-c", _READ_SCRIPT, "bash", outbox_dir()],
            uid=uid,
            timeout=15,  # tar+base64 of a <=256KB*20 outbox is near-instant
        )
    except IncusError as e:
        raise OutboxReadError(f"could not read the outbox in {container}: {e}") from e
    if not raw.strip():
        return Outbox(files={})
    try:
        blob = base64.b64decode(raw.strip(), validate=True)
    except ValueError as e:  # binascii.Error (invalid base64) is a ValueError subclass
        raise OutboxReadError(f"{container} returned an unreadable outbox archive") from e
    return Outbox(files=_members(blob, container))
