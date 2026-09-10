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
import difflib
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, assert_never

from rich.markup import escape

from jailbee import git, pr
from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import Incus, IncusError
from jailbee.pr_ai import PrText
from jailbee.tui import console, error_plain, info, warn, warn_plain

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.pr import PrInfo

OUTBOX_SUBPATH = ".jailbee/pr-outbox"

MAX_MANIFEST_BYTES = 256 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_LINE_COMMENTS = 100
MAX_ACTIONS = 50
MAX_MANIFESTS = 20

# A description action's title is optional, so it can be derived from the body's
# first line — which has no length discipline of its own. The limit is borrowed
# from `pr_ai._MAX_TITLE_LEN`, but the response to exceeding it is deliberately
# not: `pr_ai` *rejects* an over-long generated title and falls back, because
# another run can generate a better one. A manifest is the only copy of text a
# human asked for, so an over-long title is truncated here rather than thrown
# away with the body it came with.
MAX_TITLE_CHARS = 120

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


# --------------------------------------------------------------------------
# Validation gates and the plan
#
# The container is untrusted input and the host's `gh` can write to any
# repository, so a manifest is resolved against the host's own view of the
# repo and the PR *before* anything is shown to a human, let alone applied.
# Gate order matters: repo lock first (a wrong repo is the costliest
# mistake), then PR ownership, then staleness — see the design's §C for why
# each gate exists.
# --------------------------------------------------------------------------

_GITHUB_SLUG_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def github_slug(url: str) -> str | None:
    """Extract an ``owner/name`` slug from a GitHub remote URL, or None.

    Matches the ssh form (``git@github.com:owner/name.git``), the https form
    (``https://github.com/owner/name[.git]``) and an explicit ``ssh://``
    form. Any non-GitHub host, or an empty/unparseable URL, returns None.
    Pure — no subprocess, no network.
    """
    if not url:
        return None
    match = _GITHUB_SLUG_RE.search(url)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


class GateError(Exception):
    """A manifest failed a host-side validation gate.

    Raised before anything from the manifest is shown to a human, let alone
    published to GitHub. Every message names the manifest file and is meant
    to be read by the user, not decoded.
    """


@dataclass(frozen=True)
class Target:
    """What one manifest resolves to, once the gates have run."""

    manifest: Manifest
    pr: PrInfo | None
    stale: bool


def resolve_target(
    cfg: Config, incus: Incus, container: str, manifest: Manifest, *, force: bool
) -> Target:
    """Run the validation gates for `manifest` and resolve what it targets.

    1. Repo lock: the host's configured remote must be a GitHub URL whose
       slug matches ``manifest.repo``. This is the gate that matters most —
       without it a container could aim the host's `gh` at an unrelated
       repository.
    2. PR lock (skipped when ``manifest.pr is None``): ``manifest.pr`` must
       be among the numbers the container itself owns — its
       ``pr_flow.PR_LABEL_PREFIX`` / ``STACKED_LABEL_PREFIX`` labels, or,
       when neither is set, the PR (if any) for the container's own branch.
    3. Staleness: computed for every manifest with a PR, but it only raises
       when the manifest carries a `ReviewAction` and `force` was not
       given — a moved head invalidates line anchors, but `reply`, `comment`
       and `description` actions don't depend on `head_sha`.
    """
    remote_url = git.get_remote_url(cfg.repo_root, cfg.upstream_remote)
    slug = github_slug(remote_url or "")
    if slug is None:
        raise GateError(
            f"manifest {manifest.name}: no GitHub remote configured for this repo "
            f"(remote {cfg.upstream_remote!r} is not a GitHub URL)"
        )
    if slug != manifest.repo:
        raise GateError(
            f"manifest {manifest.name} targets {manifest.repo}, but this repo is {slug}"
        )

    if manifest.pr is None:
        return Target(manifest=manifest, pr=None, stale=False)

    # Imported lazily: pr_flow will import this module once the CLI (Task 9)
    # and `jb pr` (Tasks 10-12) are wired up, and a module-level import here
    # would close that into an import cycle.
    from jailbee.pr_flow import PR_LABEL_PREFIX, STACKED_LABEL_PREFIX

    owned_numbers: set[int] = set()
    for prefix in (PR_LABEL_PREFIX, STACKED_LABEL_PREFIX):
        value = incus.config_get(container, prefix)
        if value is not None:
            try:
                owned_numbers.add(int(value))
            except ValueError:
                pass  # a non-numeric label value is not a PR this container owns

    if not owned_numbers:
        branch = incus.config_get(container, "user.jailbee.branch")
        if branch is not None:
            found = pr.find_pr_for_branch(cfg.repo_root, branch)
            if found is not None:
                owned_numbers.add(found.number)

    if manifest.pr not in owned_numbers:
        raise GateError(
            f"manifest {manifest.name} references PR #{manifest.pr}, which "
            f"container {container} does not own"
        )

    info = pr.resolve_pr(cfg.repo_root, manifest.pr, remote=cfg.upstream_remote)
    stale = manifest.head_sha not in (None, info.head_sha)
    if stale and not force and any(isinstance(a, ReviewAction) for a in manifest.actions):
        raise GateError(
            f"manifest {manifest.name}: PR #{info.number}'s head moved "
            f"{manifest.head_sha} → {info.head_sha}; re-anchor the comments "
            "(ask the agent to re-read the diff) or pass --force"
        )

    return Target(manifest=manifest, pr=info, stale=stale)


def _first_line(body: str, width: int = 68) -> str:
    """The first line of `body`, cut to `width` columns with an ellipsis.

    Used for every action body in the plan except the description's diff,
    which is shown in full — "rewrites the body" is not reviewable
    otherwise.
    """
    line = body.splitlines()[0] if body else ""
    if len(line) > width:
        return line[: width - 1].rstrip() + "…"
    return line


def _comment_anchor(comment: LineComment) -> str:
    """One line comment's anchor: ``src/x.py:88`` or ``src/x.py:120-134``.

    Shared by `plan_lines` and `show_lines` so "where this comment lands" has
    one spelling.
    """
    if comment.start_line is not None:
        return f"{comment.path}:{comment.start_line}-{comment.line}"
    return f"{comment.path}:{comment.line}"


def plan_lines(target: Target, current_body: str | None) -> list[str]:
    """Render the plan for `target` as plain text lines. Pure — no printing.

    One line per action (plus a sub-line per line comment, and per general
    comment reply), bodies truncated to their first line via `_first_line`.
    The exception is a `DescriptionAction`, whose body is rendered in full
    as a unified diff against `current_body`. Rich markup is the caller's
    job (Task 9), not this function's.
    """
    lines: list[str] = []
    for action in target.manifest.actions:
        if isinstance(action, ReviewAction):
            lines.append(f"REVIEW ({action.event}): {_first_line(action.body)}")
            for comment in action.comments:
                lines.append(f"  {_comment_anchor(comment)}: {_first_line(comment.body)}")
        elif isinstance(action, ReplyAction):
            lines.append(
                f"REPLY to review comment #{action.comment_id}: {_first_line(action.body)}"
            )
        elif isinstance(action, CommentAction):
            lines.append(f"COMMENT (general): {_first_line(action.body)}")
            if action.reply_to is not None:
                lines.append(f"  reply to general comment #{action.reply_to}")
        elif isinstance(action, DescriptionAction):
            lines.append("DESCRIPTION")
            if action.title is not None:
                lines.append(f"  title: {action.title}")
            lines.extend(
                difflib.unified_diff(
                    (current_body or "").splitlines(),
                    action.body.splitlines(),
                    fromfile="current description",
                    tofile="proposed",
                    lineterm="",
                )
            )
        else:
            # Action is a closed union (Task 2); this both narrows the type
            # for mypy and guards against a future member added to it
            # without updating this renderer.
            assert_never(action)
    return lines


def show_lines(manifest: Manifest) -> list[str]:
    """Every action of `manifest`, bodies in full. Pure — no printing.

    The untruncated counterpart of `plan_lines`, and the same contract: plain
    text lines, no Rich markup, the caller decides how they are rendered. It
    lives here for the same reason `plan_lines` does — `Action` is a closed
    union defined in this module, and the one command whose whole purpose is
    showing *everything* must not be the place a new variant silently goes
    missing.

    A body is split into its own lines rather than emitted as one embedded
    block, so a caller printing line by line reproduces it exactly. An empty
    body contributes nothing, which is what it is.
    """
    lines: list[str] = [
        f"{manifest.name}  {manifest.repo}  "
        + (f"PR #{manifest.pr}" if manifest.pr is not None else "no PR yet")
    ]
    for index, action in enumerate(manifest.actions):
        lines.append("")
        if isinstance(action, ReviewAction):
            lines.append(f"action {index} · REVIEW ({action.event})")
            lines.extend(action.body.splitlines())
            for comment in action.comments:
                lines.append("")
                lines.append(f"  {_comment_anchor(comment)}")
                lines.extend(comment.body.splitlines())
        elif isinstance(action, ReplyAction):
            lines.append(f"action {index} · REPLY to review comment #{action.comment_id}")
            lines.extend(action.body.splitlines())
        elif isinstance(action, CommentAction):
            reply = (
                f", replying to general comment #{action.reply_to}"
                if action.reply_to is not None
                else ""
            )
            lines.append(f"action {index} · COMMENT (general){reply}")
            lines.extend(action.body.splitlines())
        elif isinstance(action, DescriptionAction):
            lines.append(f"action {index} · DESCRIPTION")
            if action.title is not None:
                lines.append(f"  title: {action.title}")
            if action.branch is not None:
                lines.append(f"  branch: {action.branch}")
            lines.extend(action.body.splitlines())
        else:
            assert_never(action)
    return lines


_ACTION_LABELS: tuple[tuple[str, type[Action]], ...] = (
    ("review", ReviewAction),
    ("reply", ReplyAction),
    ("comment", CommentAction),
    ("description", DescriptionAction),
)


def action_summary(manifest: Manifest) -> str:
    """One manifest's action counts by type: ``review:1 reply:2``. Pure.

    The one-cell counterpart of `plan_lines`, for `jb review ls`. It lives
    here rather than in `cli.py` because `Action` is a closed union defined
    in this module: a fifth variant needs a label here, and adding one to
    `_ACTION_LABELS` is a visible edit next to the union itself, where the
    same summary spelled out in the CLI would silently under-count.
    """
    parts = [
        f"{label}:{count}"
        for label, cls in _ACTION_LABELS
        if (count := sum(isinstance(a, cls) for a in manifest.actions))
    ]
    return " ".join(parts)


# --------------------------------------------------------------------------
# Applying a manifest
#
# GitHub calls are not transactional, so every write below goes through
# ``incus.exec`` (never a shell string — argv only) and progress is recorded
# *in the container*, not just returned to the caller: a review comment
# posted twice is public noise nothing can take back. See design §C.
# --------------------------------------------------------------------------

_NO_URL = "(no url)"

# Matches a `"body_file": "name"` field in a manifest's *raw* JSON text.
# Deliberately a text scan rather than a second `parse_manifest` call:
# `parse_manifest` resolves `body_file` into an inline `body` string and
# throws the filename away, so once a `Manifest` exists there is nowhere
# left to ask "which file did this come from" except the raw source.
# Naive on purpose: it cannot tell a real `body_file` field from the same
# text appearing inside some other string value, so it can only ever
# over-count references. That failure mode is safe — an extra file kept
# around costs nothing, where deleting one still in use would not.
_BODY_FILE_RE = re.compile(r'"body_file"\s*:\s*"([^"]*)"')


def _now_iso() -> str:
    """Current UTC time as `2026-09-09T12:34:56Z`, for `applied.log` lines."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _display_url(url: str) -> str:
    """`url`, or a placeholder when `pr.py` returned "".

    `submit_review`/`reply_to_review_comment`/`add_issue_comment` return ""
    when a 2xx GitHub response ever lacks `html_url` (Task 4). Writing that
    empty string into a human-facing log line would read as "the link is
    missing" (a jailbee bug) rather than "GitHub didn't send one" — so the
    log always gets an explicit placeholder instead. The progress sidecar's
    `urls` map keeps the raw value (including ""): it's machine-read state
    for a future run, not something shown to a person.
    """
    return url if url else _NO_URL


def _write_sidecar(
    incus: Incus, container: str, path: str, payload: dict[str, Any], *, uid: int | None
) -> None:
    """Overwrite `path` in the container with `payload` as one JSON document.

    Argv only, never a shell string: the payload is passed as `$1` to a
    `bash -c` script that never interpolates it. `>` (not `>>`) because the
    sidecar always holds the *complete* current state, not an appended log.
    """
    incus.exec(
        container,
        ["bash", "-c", 'printf %s "$1" > "$2"', "bash", json.dumps(payload), path],
        uid=uid,
    )


def _append_log_line(incus: Incus, container: str, line: str, *, uid: int | None) -> None:
    """Append one line to `applied.log`. Argv only; `>>` to accumulate history."""
    incus.exec(
        container,
        ["bash", "-c", 'printf "%s\\n" "$1" >> "$2"', "bash", line, f"{outbox_dir()}/applied.log"],
        uid=uid,
    )


@dataclass(frozen=True)
class Progress:
    """What a manifest's `<name>.progress.json` sidecar records.

    `urls` maps a stringified action index to the URL that landed for it
    (or "" — see `_display_url`).
    """

    applied: frozenset[int]
    urls: dict[str, str]


def _parse_progress_json(text: str) -> Progress:
    """Parse one sidecar's text; any shape problem is "nothing applied yet".

    A sidecar is jailbee's own output, but a half-written file (a crash
    mid-`printf`) or a container-side accident is still possible, and the
    safe response to unreadable progress is to treat it as no progress —
    the caller then re-attempts, and re-attempting an already-landed action
    is exactly what this sidecar exists to prevent when it *can* be read.
    """
    empty = Progress(applied=frozenset(), urls={})
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return empty
    if not isinstance(data, dict):
        return empty
    applied_val = data.get("applied")
    if not isinstance(applied_val, list):
        return empty
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in applied_val):
        return empty
    urls_val = data.get("urls")
    urls = {str(k): str(v) for k, v in urls_val.items()} if isinstance(urls_val, dict) else {}
    return Progress(applied=frozenset(applied_val), urls=urls)


def read_progress(outbox: Outbox, manifest_name: str) -> Progress:
    """Read `<manifest_name>.progress.json` from `outbox`; tolerant of absence.

    A missing sidecar (first run) and a broken one (see `_parse_progress_json`)
    both resolve to "nothing applied yet" rather than raising.
    """
    text = outbox.files.get(f"{manifest_name}.progress.json")
    if text is None:
        return Progress(applied=frozenset(), urls={})
    return _parse_progress_json(text)


def pending_indices(manifest: Manifest, progress: Progress) -> list[int]:
    """Action indices of `manifest` that `apply_manifest` would still post.

    The same rule `apply_manifest` applies when it skips an index already in
    `progress.applied`, stated once so a caller can *count* the pending
    actions — the plan's "N actions will be published" line — without
    restating it. Ascending manifest order, not application order: this
    answers "how many, and which", never "in what sequence".
    """
    return [i for i in range(len(manifest.actions)) if i not in progress.applied]


@dataclass(frozen=True)
class ApplyOutcome:
    """What one `apply_manifest` call did.

    `applied`/`urls` are positionally paired and cover only indices newly
    applied *by this call* — indices `apply_manifest` skipped because they
    were already in `progress.applied` are not repeated here. `finalize`
    merges this against the manifest's own recorded progress before
    deciding what the container now looks like, so nothing already landed
    is ever lost.
    """

    applied: tuple[int, ...]
    urls: tuple[str, ...]
    failure: str | None


def _apply_order(actions: tuple[Action, ...]) -> list[int]:
    """Indices of `actions` in application order: review, then the rest, then description.

    `parse_manifest` already enforces at most one `ReviewAction` and at most
    one `DescriptionAction`, so both lists below have length 0 or 1.
    """
    review = [i for i, a in enumerate(actions) if isinstance(a, ReviewAction)]
    description = [i for i, a in enumerate(actions) if isinstance(a, DescriptionAction)]
    rest = [i for i, a in enumerate(actions) if isinstance(a, (ReplyAction, CommentAction))]
    order = review + rest + description
    # Guards against a future fifth `Action` variant silently falling through
    # every isinstance check above: it would never be posted *and* never be
    # marked applied, rather than failing loudly here.
    assert len(order) == len(actions), (
        "_apply_order does not account for every action — a new Action variant needs a branch here"
    )
    return order


def _comment_payload(comment: LineComment) -> dict[str, Any]:
    """One `LineComment` as the `comments[]` entry `gh api .../reviews` expects."""
    payload: dict[str, Any] = {
        "path": comment.path,
        "line": comment.line,
        "body": comment.body,
        "side": comment.side,
    }
    if comment.start_line is not None:
        payload["start_line"] = comment.start_line
        payload["start_side"] = comment.start_side or comment.side
    return payload


def _reply_permalink(slug: str, number: int, comment_id: int) -> str:
    """Prefix for a general `CommentAction` that replies to another comment.

    GitHub's issue-comments endpoint has no threading, unlike review
    comments (`reply_to_review_comment`), so a reply is simulated with a
    permalink to the comment it answers.
    """
    return f"> [Replying to this comment](https://github.com/{slug}/pull/{number}#issuecomment-{comment_id})\n\n"


def _apply_one(repo_root: Path, target: Target, action: Action) -> str:
    """Post one action to GitHub via `pr.py`; return its receipt URL (or "")."""
    assert target.pr is not None  # apply_manifest's own precondition
    number = target.pr.number
    if isinstance(action, ReviewAction):
        commit_id = target.manifest.head_sha or target.pr.head_sha
        comments = [_comment_payload(c) for c in action.comments]
        return pr.submit_review(
            repo_root, number, commit_id=commit_id, body=action.body, comments=comments
        )
    elif isinstance(action, ReplyAction):
        return pr.reply_to_review_comment(repo_root, number, action.comment_id, action.body)
    elif isinstance(action, CommentAction):
        body = action.body
        if action.reply_to is not None:
            body = _reply_permalink(target.manifest.repo, number, action.reply_to) + body
        return pr.add_issue_comment(repo_root, number, body)
    elif isinstance(action, DescriptionAction):
        pr.edit_pr(repo_root, number, title=action.title, body=action.body)
        return ""  # edit_pr updates an existing object; there is no new receipt
    else:
        assert_never(action)


def apply_manifest(
    cfg: Config,
    incus: Incus,
    container: str,
    target: Target,
    progress: Progress,
    *,
    uid: int | None,
) -> ApplyOutcome:
    """Apply `target.manifest`'s pending actions to GitHub, in the fixed order.

    Order: the review (if any) first — one atomic call carrying every line
    comment — then replies and general comments in manifest order, then the
    description last, so a failed comment never leaves a rewritten
    description as the only visible change (design §C). Indices already in
    `progress.applied` are skipped outright: a retry after a failure must
    never repost what already landed.

    After each action that succeeds, the running total (`progress.applied`
    plus everything applied so far in *this* call) is written to the
    container's `<manifest>.progress.json` sidecar. This is why `incus`,
    `container` and `uid` are parameters here and not only on `finalize`: a
    crash between two actions — or between the last action and `finalize` —
    must still leave the container's own record accurate.

    Stops at the first `PrError`; remaining actions are never attempted.

    A `PrError` is not the only way an action can fail to be *safely*
    applied: the action can succeed on GitHub and then the sidecar write
    that was supposed to record it can itself fail (`IncusError` — the
    container stopped, its disk is full, a permission changed). That index
    is *not* dropped: it is already in `applied`/`urls` (the post landed),
    but `failure` is set to say plainly that it landed and could not be
    recorded, so a re-run may repost it — and, exactly as with a `PrError`,
    no further action is attempted once the run can no longer record what
    it just did.
    """
    if target.pr is None:
        raise ValueError(
            f"apply_manifest requires a resolved PR (manifest {target.manifest.name!r} "
            "has pr: null); a pr: null manifest carries only a description and must "
            "be routed to `jb pr` (record_consumed), never to apply_manifest"
        )

    manifest = target.manifest
    sidecar_path = f"{outbox_dir()}/{manifest.name}.progress.json"

    applied: list[int] = []
    urls: list[str] = []
    failure: str | None = None
    running_applied = set(progress.applied)
    running_urls = dict(progress.urls)

    for index in _apply_order(manifest.actions):
        if index in progress.applied:
            continue
        try:
            url = _apply_one(cfg.repo_root, target, manifest.actions[index])
        except pr.PrError as e:
            failure = str(e)
            break
        applied.append(index)
        urls.append(url)
        running_applied.add(index)
        running_urls[str(index)] = url
        try:
            _write_sidecar(
                incus,
                container,
                sidecar_path,
                {"applied": sorted(running_applied), "urls": running_urls},
                uid=uid,
            )
        except IncusError as e:
            failure = (
                f"action {index} was published to GitHub but could not be recorded "
                f"in the container ({e}); a re-run may repost it"
            )
            break

    return ApplyOutcome(applied=tuple(applied), urls=tuple(urls), failure=failure)


def _orphaned_body_files(outbox: Outbox, exclude_name: str) -> list[str]:
    """Non-manifest files in `outbox` that no manifest other than `exclude_name` references.

    Scans every *other* manifest's raw JSON text for `body_file` mentions
    (see `_BODY_FILE_RE`) rather than parsing them, since parsing loses the
    filename. A file referenced by nothing still standing — including one
    orphaned by some earlier, unrelated cleanup — is safe to delete.
    """
    referenced_elsewhere: set[str] = set()
    for name in outbox.manifest_names:
        if name == exclude_name:
            continue
        referenced_elsewhere.update(_BODY_FILE_RE.findall(outbox.files.get(name, "")))
    return sorted(
        name
        for name in outbox.files
        if not name.endswith(".json") and name not in referenced_elsewhere
    )


def _delete_from_outbox(
    incus: Incus,
    container: str,
    outbox: Outbox,
    name: str,
    *,
    uid: int | None,
    with_sidecar: bool,
) -> list[str]:
    """Remove manifest `name` and everything only it still needs, in one `rm`.

    The single deletion path in this module: `finalize` uses it for a manifest
    that is fully applied, `drop_manifest` for one the user discarded, and the
    outbox layout, the ``-f`` and the orphan rule (`_orphaned_body_files`)
    therefore live in exactly one place. `with_sidecar` is True whenever a
    progress sidecar exists to remove — always, for `finalize`, which has just
    written one.

    Returns the outbox-relative names removed, and lets `IncusError` out: the
    two callers have different things to say about a failed deletion.
    """
    names = [name]
    if with_sidecar:
        names.append(f"{name}.progress.json")
    names.extend(_orphaned_body_files(outbox, name))
    incus.exec(container, ["rm", "-f", *(f"{outbox_dir()}/{n}" for n in names)], uid=uid)
    return names


class FinalizeError(Exception):
    """`finalize`/`record_consumed`/`drop_manifest` could not write to the container.

    Raised when the *container* write itself fails (`IncusError`) after the
    GitHub side of the work is already settled — e.g. the sidecar can't be
    written, or a fully-applied (or deliberately dropped) manifest can't be
    deleted. Unlike `apply_manifest`, none of them returns an `ApplyOutcome`
    to carry a `failure` string in, so this is the equivalent for them: a named,
    documented exception whose message says which step failed and what
    that implies, rather than a bare `IncusError` from deep inside a
    `bash -c printf` call. Each function stops at the first such failure —
    it does not go on to a later step (appending the log, or deleting the
    manifest) once an earlier one could not be recorded.
    """


def finalize(
    incus: Incus,
    container: str,
    outbox: Outbox,
    target: Target,
    outcome: ApplyOutcome,
    *,
    uid: int | None,
) -> None:
    """Record `outcome`, then delete `target.manifest` if it is now fully applied.

    `outbox` is the snapshot read *before* `apply_manifest` ran, so
    `read_progress(outbox, ...)` reproduces the progress that was already
    passed into `apply_manifest` as `progress`. Merging that against
    `outcome.applied` (this call's newly-applied indices) reconstructs the
    full picture without a separate `progress` parameter — and without ever
    overwriting the sidecar with less than what has actually landed.

    Always writes the sidecar when anything has ever been applied (whether
    or not that completes the manifest), even on a re-invocation that
    applies nothing new this time — the write is idempotent, so this is
    harmless. The `applied.log` line, however, is only appended when *this*
    call actually applied something (`outcome.applied`); gating it on the
    merged total instead would append a stale `actions=0` line on every
    no-op re-finalize. Only when every action index is now applied does it
    delete the manifest, its sidecar, and any `body_file` no other manifest
    in the outbox still references — a shared `.md` is kept. Writing the
    sidecar before deleting (rather than skipping the write when about to
    delete anyway) means a crash between the two `incus.exec` calls still
    leaves an accurate, resumable record.

    Raises `FinalizeError` — stopping before any later step — if a
    container write here fails; see that class for why.
    """
    manifest = target.manifest
    manifest_path = f"{outbox_dir()}/{manifest.name}"
    sidecar_path = f"{manifest_path}.progress.json"

    baseline = read_progress(outbox, manifest.name)
    merged_applied = baseline.applied | set(outcome.applied)
    merged_urls = dict(baseline.urls)
    merged_urls.update(dict(zip((str(i) for i in outcome.applied), outcome.urls, strict=True)))

    if merged_applied:
        try:
            _write_sidecar(
                incus,
                container,
                sidecar_path,
                {"applied": sorted(merged_applied), "urls": merged_urls},
                uid=uid,
            )
        except IncusError as e:
            raise FinalizeError(
                f"manifest {manifest.name}: applied {sorted(outcome.applied)} to "
                f"GitHub, but the progress sidecar could not be written ({e}); a "
                "re-run may repost them"
            ) from e

    if outcome.applied:
        pr_field = manifest.pr if manifest.pr is not None else "none"
        display_urls = ",".join(_display_url(u) for u in outcome.urls) or _NO_URL
        line = (
            f"{_now_iso()} {manifest.name} pr={pr_field} "
            f"actions={len(outcome.applied)} urls={display_urls}"
        )
        try:
            _append_log_line(incus, container, line, uid=uid)
        except IncusError as e:
            raise FinalizeError(
                f"manifest {manifest.name}: progress was recorded, but the "
                f"applied.log entry could not be written ({e})"
            ) from e

    if merged_applied == set(range(len(manifest.actions))):
        try:
            _delete_from_outbox(incus, container, outbox, manifest.name, uid=uid, with_sidecar=True)
        except IncusError as e:
            raise FinalizeError(
                f"manifest {manifest.name} is fully applied but could not be "
                f"deleted ({e}); it will be cleaned up on a later run"
            ) from e


def drop_manifest(
    incus: Incus, container: str, outbox: Outbox, name: str, *, uid: int | None
) -> list[str]:
    """Delete manifest `name` from the container's outbox without applying it.

    The discard counterpart of `finalize`'s cleanup, and literally the same
    deletion — both go through `_delete_from_outbox`, so the manifest, its
    progress sidecar (when one exists) and any `body_file` no *other*
    manifest in `outbox` still references go in one `rm -f`. Nothing is
    posted to GitHub and no `applied.log` line is written: dropping is the
    user saying this proposal will never be published, so there is nothing
    to record.

    Returns the outbox-relative names that were deleted, so a caller
    dropping several manifests in a row can shrink its own `outbox`
    snapshot between calls — without that, a body file shared by two
    manifests looks referenced while each of them is dropped and would
    survive them both.

    Raises `FinalizeError` if the deletion fails; the manifest is then still
    pending, exactly as it was.
    """
    try:
        return _delete_from_outbox(
            incus,
            container,
            outbox,
            name,
            uid=uid,
            with_sidecar=f"{name}.progress.json" in outbox.files,
        )
    except IncusError as e:
        raise FinalizeError(
            f"manifest {name} could not be deleted ({e}); it is still pending"
        ) from e


def _read_optional(incus: Incus, container: str, path: str, *, uid: int | None) -> str | None:
    """Read one file's content from the container; None if it can't be read.

    The single-file counterpart of `_READ_SCRIPT`'s whole-outbox tar+base64,
    for `record_consumed`, which has no `Outbox` in hand. Like that reader,
    any failure (missing file, permission, the instance not running) is
    treated as absence rather than surfaced — a caller here always has a
    safe default for "the file isn't there".
    """
    try:
        return incus.exec(container, ["cat", path], uid=uid)
    except IncusError:
        return None


def _manifest_pr_and_action_count(text: str | None) -> tuple[str, int | None]:
    """Best-effort `(pr= field, action count)` from a manifest's raw JSON text.

    Used only for `record_consumed`'s log line and its own deletion check;
    any parse failure yields `("none", None)`, which logs politely and
    never deletes a manifest whose action count couldn't be confirmed.
    """
    if text is None:
        return "none", None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "none", None
    if not isinstance(data, dict):
        return "none", None
    pr_val = data.get("pr")
    pr_field = str(pr_val) if isinstance(pr_val, int) and not isinstance(pr_val, bool) else "none"
    actions_val = data.get("actions")
    total = len(actions_val) if isinstance(actions_val, list) else None
    return pr_field, total


def record_consumed(
    incus: Incus,
    container: str,
    manifest_name: str,
    index: int,
    url: str,
    *,
    uid: int | None,
) -> None:
    """Record that action `index` of `manifest_name` was applied outside `apply_manifest`.

    The single-index form `jb pr` (Task 11) uses when it turns a pending
    null-PR `description` action into the PR it just created or updated:
    there is no `Outbox`/`Target` in hand at that call site, only the one
    manifest name and the one index just consumed. Reads the manifest's
    current sidecar and its own action count directly off the container,
    merges `index` into what's applied, writes the sidecar and one
    `applied.log` line, and deletes the manifest (with its sidecar) once
    that leaves nothing pending.

    Raises `FinalizeError` — stopping before any later step — if a
    container write here fails; see that class for why.
    """
    manifest_path = f"{outbox_dir()}/{manifest_name}"
    sidecar_path = f"{manifest_path}.progress.json"

    sidecar_text = _read_optional(incus, container, sidecar_path, uid=uid)
    progress = (
        _parse_progress_json(sidecar_text)
        if sidecar_text is not None
        else Progress(applied=frozenset(), urls={})
    )
    applied = progress.applied | {index}
    urls = dict(progress.urls)
    urls[str(index)] = url

    try:
        _write_sidecar(
            incus, container, sidecar_path, {"applied": sorted(applied), "urls": urls}, uid=uid
        )
    except IncusError as e:
        raise FinalizeError(
            f"manifest {manifest_name}: action {index} was published but the "
            f"progress sidecar could not be written ({e}); a re-run may repost it"
        ) from e

    manifest_text = _read_optional(incus, container, manifest_path, uid=uid)
    pr_field, total_actions = _manifest_pr_and_action_count(manifest_text)

    line = f"{_now_iso()} {manifest_name} pr={pr_field} actions=1 urls={_display_url(url)}"
    try:
        _append_log_line(incus, container, line, uid=uid)
    except IncusError as e:
        raise FinalizeError(
            f"manifest {manifest_name}: action {index}'s progress was recorded, "
            f"but the applied.log entry could not be written ({e})"
        ) from e

    if total_actions is not None and applied == set(range(total_actions)):
        try:
            incus.exec(container, ["rm", "-f", manifest_path, sidecar_path], uid=uid)
        except IncusError as e:
            raise FinalizeError(
                f"manifest {manifest_name} is fully applied but could not be "
                f"deleted ({e}); it will be cleaned up on a later run"
            ) from e


# --------------------------------------------------------------------------
# The `jailbee pr` half of the outbox
#
# A `description` action is the one action `jb review apply` never publishes on
# its own: it is the text of a PR that may not exist yet, so `jailbee pr` picks
# it up instead of running Claude, and records it with `record_consumed` above.
# Everything below is best-effort by contract — `jailbee pr` must fall back to
# its normal path rather than fail because of what a container wrote.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxPrText:
    """A container-written PR description `jailbee pr` can use as-is.

    `manifest` and `index` are what `record_consumed` needs once the text has
    actually landed in a PR; without them the description would be published
    and then offered again on the next run.
    """

    text: PrText
    manifest: str
    index: int


def _description_title(action: DescriptionAction, fallback: str) -> str:
    """The title for `action`: its own, else the body's first non-blank line.

    `title` is optional in the manifest schema but `PrText.title` is not, and
    an empty PR title is rejected by GitHub rather than by us. A leading
    Markdown heading marker is dropped, since a body that opens with
    `## Summary` means the heading text, not the hashes.
    """
    if action.title and action.title.strip():
        candidate = action.title.strip()
    else:
        candidate = next(
            (
                stripped
                for stripped in (
                    line.strip().lstrip("#").strip() for line in action.body.splitlines()
                )
                if stripped
            ),
            "",
        )
    if not candidate:
        return fallback
    if len(candidate) > MAX_TITLE_CHARS:
        return candidate[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return candidate


def _eligible_for(manifest: Manifest, for_pr: int | None) -> bool:
    """True if `manifest`'s description belongs to the PR this run is about.

    `pr: null` means "the PR `jailbee pr` is about to open from this
    container", so the create path (`for_pr=None`) accepts only those. A
    numbered manifest describes a PR that already exists: publishing its
    proposed body as a brand-new PR's would put the text somewhere it was never
    meant to go *and* burn the action index, so `jb review apply` could never
    post it where it belongs. The update path passes its own number and accepts
    both — `pr: null` because the container may have written the description
    before the PR existed, `for_pr` because that is the PR being updated.
    """
    if for_pr is None:
        return manifest.pr is None
    return manifest.pr in (None, for_pr)


def _outbox_branch(action: DescriptionAction, container_branch: str, name: str) -> str:
    """The head branch name to propose: `action.branch` if it is a valid ref.

    The container is untrusted input and this value reaches `git push`, the
    local-branch rename and `gh pr create --head`. The other two sources of a
    head name are already checked (`--as` exits 2, `pr_ai` falls back), so this
    one is too — a rejected name falls back to the container's own branch
    rather than failing the run after the push, which the design forbids.
    """
    if not action.branch:
        return container_branch
    if git.check_ref_format(action.branch):
        return action.branch
    instead = (
        f"publishing under {container_branch!r} instead" if container_branch else "ignoring it"
    )
    warn(
        f"{name}: the proposed branch name {action.branch!r} is not a valid git "
        f"branch name; {instead}."
    )
    return container_branch


def pending_pr_text(
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    uid: int | None,
    for_pr: int | None = None,
    pick: Callable[[list[str]], str | None] | None = None,
) -> OutboxPrText | None:
    """The pending `description` action, as a `PrText`, or None.

    Best-effort: an unreadable outbox, a malformed manifest, or an ambiguity
    with no `pick` never fails `jailbee pr` — it warns and returns None, and the
    caller falls back to its normal path. `pick` is supplied only on a TTY; it
    is handed every candidate manifest name and returns the one to use, or None
    to use none of them. Off a TTY an ambiguity is refused with a warning
    naming each candidate: `jailbee pr` must neither guess between two
    descriptions nor die mid-flow after it has already pushed.

    Two gates decide which manifests may contribute, and they are the two
    `resolve_target` runs first, in the same order:

    1. Repo lock — `manifest.repo` must be the GitHub slug of the host's own
       upstream remote. This is the gate the design calls the costliest to
       skip: without it a container could hand `jailbee pr` text written for
       an unrelated repository. Failing it warns and skips.
    2. PR ownership — `for_pr`; see `_eligible_for`. A manifest for some other
       PR is skipped *silently*: a review container legitimately carries such
       manifests for `jailbee review apply`, and warning about them on every
       `jailbee pr` run would be noise, not news.
    """
    try:
        outbox = read_outbox(incus, container, uid=uid)
    except OutboxReadError as e:
        warn(f"{e}; falling back to the usual PR text.")
        return None
    if not outbox.manifest_names:
        # Before the git round-trip below: the empty outbox is the common case.
        return None

    slug = github_slug(git.get_remote_url(cfg.repo_root, cfg.upstream_remote) or "")
    if slug is None:
        warn(
            f"{container} has outbox manifests, but this repo has no GitHub remote "
            f"(remote {cfg.upstream_remote!r} is not a GitHub URL) to check them "
            "against; falling back to the usual PR text."
        )
        return None

    candidates: list[tuple[str, int, DescriptionAction]] = []
    for name in outbox.manifest_names:
        try:
            manifest = parse_manifest(name, outbox.files[name], outbox.files)
        except ManifestError as e:
            warn(f"Ignoring outbox manifest {name}: {e}")
            continue
        if manifest.repo != slug:
            warn(f"Ignoring outbox manifest {name}: it targets {manifest.repo}, not {slug}.")
            continue
        if not _eligible_for(manifest, for_pr):
            continue
        candidates.extend(
            (name, index, action)
            for index in pending_indices(manifest, read_progress(outbox, name))
            if isinstance(action := manifest.actions[index], DescriptionAction)
        )

    if not candidates:
        return None

    if len(candidates) > 1:
        names = [name for name, _, _ in candidates]
        if pick is None:
            warn(
                f"{container} has more than one pending PR description "
                f"({', '.join(names)}); using none of them. Read them with "
                f"`jailbee review show`, drop the stale one with `jailbee review "
                f"drop`, or re-run on a terminal to choose."
            )
            return None
        chosen = pick(names)
        if chosen is None:
            return None
        candidates = [c for c in candidates if c[0] == chosen]
        if not candidates:  # a picker that answered something it was not offered
            warn(f"{chosen!r} is not one of {container}'s pending manifests; using none of them.")
            return None

    name, index, action = candidates[0]
    # A description need not propose a branch name. The container's own branch
    # is then the honest answer — it is what `jailbee pr` would publish under
    # anyway, and `confirm_pr_branch_name` skips its prompt when the proposal
    # and the source branch agree.
    container_branch = incus.config_get(container, "user.jailbee.branch") or ""
    return OutboxPrText(
        text=PrText(
            title=_description_title(action, container_branch or container),
            body=action.body,
            branch=_outbox_branch(action, container_branch, name),
        ),
        manifest=name,
        index=index,
    )


# --------------------------------------------------------------------------
# The shared offer: gate everything, show the plan, ask once, publish
#
# `jailbee review apply` and the offer `jailbee pr` makes once a PR is up are
# the same loop behind two different questions, so the loop lives here once
# and each caller injects its own `confirm`. That keeps `cli.py` thin and, more
# to the point, keeps the two from drifting: the rule that a failed
# container-side write *stops* the run rather than publishing the next manifest
# into a container that can no longer record it must hold on both paths.
#
# This is the one part of the module that prints. Everything above it renders
# to strings and lets a caller decide; here the printing *is* the behaviour
# being shared, and splitting it back out would leave each caller with its own
# copy of the loop again.
# --------------------------------------------------------------------------


def _current_pr_body(cfg: Config, target: Target) -> str | None:
    """The PR's current description, when the manifest proposes rewriting it.

    Fetched only for a manifest carrying a ``description`` action — that diff
    is the one part of the plan `plan_lines` renders in full — and a failure
    to read it degrades the diff instead of blocking the plan.
    """
    if target.pr is None:
        return None
    if not any(isinstance(a, DescriptionAction) for a in target.manifest.actions):
        return None
    try:
        return pr.pr_body(cfg.repo_root, target.pr.number)
    except pr.PrError as e:
        warn_plain(
            f"could not read PR #{target.pr.number}'s current description ({e}); "
            "the proposed body is shown as an addition"
        )
        return None


def _print_plan(cfg: Config, short: str, target: Target, progress: Progress) -> None:
    """Print one manifest's header and its plan.

    Every plan line is printed with markup off and wrapping soft: the bodies
    were written inside the container, and Rich would read a ``[note]`` in
    one of them as a style tag and *silently delete it* — in exactly the text
    the user is being asked to vouch for.
    """
    # `_gate_manifests` held back every `pr: null` manifest, so a target
    # reaching the plan always has a resolved PR to head it.
    assert target.pr is not None
    manifest = target.manifest
    console.print()
    console.print(
        f"[bold]PR #{target.pr.number}[/bold]  {escape(manifest.repo)}  "
        f"head {escape(target.pr.head_sha)}"
    )
    console.print(f"container {escape(short)} · manifest {escape(manifest.name)}")
    if target.stale:
        console.print("[yellow]the PR head has moved since this was written[/yellow]")
    already = [i for i in sorted(progress.applied) if i < len(manifest.actions)]
    if already:
        console.print(
            f"{len(already)} of {len(manifest.actions)} actions already published — skipped"
        )
    for line in plan_lines(target, _current_pr_body(cfg, target)):
        console.print(f"  {line}", markup=False, highlight=False, soft_wrap=True)


def _print_identity(cfg: Config, total: int) -> None:
    """The plan's closing line: how much is about to be published, and as whom.

    Not decoration. Every comment below will carry the *host* user's GitHub
    identity, and this is the moment that becomes obvious. `pr.gh_login`
    swallows every failure, so an unknown login drops the clause rather than
    standing between the user and a publish.
    """
    login = pr.gh_login(cfg.repo_root)
    plural = "" if total == 1 else "s"
    tail = f" as [bold]{escape(login)}[/bold]" if login else ""
    console.print()
    console.print(f"{total} action{plural} will be published to GitHub{tail}.")


def _print_receipts(target: Target, outcome: ApplyOutcome) -> None:
    """Print what actually landed — one line per published action, with its URL."""
    for index, url in zip(outcome.applied, outcome.urls, strict=True):
        console.print(
            f"  ✓ {target.manifest.name} action {index}: {url or _NO_URL}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )


def _has_pending_comment(manifest: Manifest, progress: Progress) -> bool:
    """True if `manifest` still has an unapplied action other than a description.

    The offer's selection rule. A description is excluded because `jailbee pr`
    has just decided this PR's description itself — offering to publish another
    one in the same breath would ask the user to overrule the run they are
    still reading the output of.
    """
    return any(
        not isinstance(manifest.actions[i], DescriptionAction)
        for i in pending_indices(manifest, progress)
    )


def _held_back(reason: str, manifest: Manifest, short: str, *, forceable: bool) -> str:
    """A gate failure on the offer path, phrased as something to act on.

    The interesting case is staleness: `jailbee pr` has just pushed, so on the
    adopted path the PR's head moved and a `review` action's line anchors no
    longer point where they were written. `--force` is named only for a
    `GateError` (`forceable`) on a manifest that carries such an action,
    because that is the only refusal `resolve_target` relaxes under it —
    advertising it for a wrong-repo manifest, or for a `gh` that would not
    answer, would be advice that cannot work.
    """
    if forceable and any(isinstance(a, ReviewAction) for a in manifest.actions):
        remedy = f"`jailbee review apply --force {short}` posts them as outdated comments."
    else:
        remedy = f"`jailbee review apply {short}` deals with it separately."
    return f"held back: {reason}\n  {remedy}"


def _gate_manifests(
    cfg: Config,
    incus: Incus,
    container: str,
    outbox: Outbox,
    short: str,
    *,
    force: bool,
    comments_only: bool,
) -> tuple[list[Target], list[str], list[str]]:
    """Gate every pending manifest: (publishable targets, refusals, notes).

    Nothing is printed here. All three lists go back to the caller so it can
    report every problem *before* the first line of the plan — a plan
    interrupted halfway by a refusal is worse than a refusal on its own.

    `comments_only` is the offer's rule and it changes two things. It narrows
    the candidates to manifests that still hold an unapplied non-`description`
    action (see `_has_pending_comment`), and it turns every refusal into a
    warning: `jailbee pr` has already created or updated the PR, so nothing a
    container wrote may turn that run into a failure. A malformed manifest, a
    moved head, a `gh` that would not answer — each is reported as held back,
    with the command that deals with it, and the run goes on.

    Without it — `jailbee review apply`'s own mode — a manifest that cannot be
    published is a refusal the caller exits non-zero on, and a ``pr: null``
    manifest is a *deferral*, not a refusal: it carries the description of a PR
    that does not exist yet, which `jailbee pr` will consume. It must never
    reach `apply_manifest`, whose own precondition rejects it as a caller
    routing bug rather than as anything the user did.
    """
    targets: list[Target] = []
    refusals: list[str] = []
    notes: list[str] = []
    for name in outbox.manifest_names:
        try:
            manifest = parse_manifest(name, outbox.files[name], outbox.files)
        except ManifestError as e:
            if comments_only:
                notes.append(f"Ignoring outbox manifest {name}: {e}")
            else:
                refusals.append(str(e))
            continue
        if comments_only and not _has_pending_comment(manifest, read_progress(outbox, name)):
            continue
        try:
            target = resolve_target(cfg, incus, container, manifest, force=force)
        except GateError as e:
            if comments_only:
                notes.append(_held_back(str(e), manifest, short, forceable=True))
            else:
                refusals.append(str(e))
            continue
        except (pr.PrError, IncusError) as e:
            # Only on the offer path: `resolve_target` reaches `gh` and the
            # container to answer "which PR is this, and where is its head",
            # and neither is guaranteed to still answer once the push is done.
            # `jailbee review apply` lets these out, where a traceback is at
            # least about the command the user actually ran.
            if not comments_only:
                raise
            notes.append(_held_back(str(e), manifest, short, forceable=False))
            continue
        if target.pr is None:
            if not comments_only:
                # Two lines on purpose: the command must not be split across a
                # wrap, which is exactly what a single long line does at 80
                # columns.
                notes.append(
                    f"manifest {name} describes a PR that does not exist yet.\n"
                    f"  Run `jailbee pr {short}` to create it."
                )
            continue
        targets.append(target)
    return targets, refusals, notes


def offer_pending_comments(
    cfg: Config,
    incus: Incus,
    container: str,
    short: str,
    *,
    pr_number: int | None,
    confirm: Callable[[int], bool],
    outbox: Outbox | None = None,
    force: bool = False,
    dry_run: bool = False,
    can_prompt: bool = True,
) -> int:
    """Show what `container` wants to publish, ask once, publish it.

    Returns the number of failures, so the caller can set its own exit code.
    Both callers of this function set it to 1 on anything above zero; nothing
    here raises `typer.Exit` of its own, and the injected `confirm` is the only
    thing that may.

    `pr_number` chooses the mode.

    ``None`` is `jailbee review apply`: every pending manifest, descriptions
    included, and anything that cannot be published is a refusal the caller
    exits non-zero on.

    An integer is the offer `jailbee pr` makes once that PR is up: only
    manifests still holding an unapplied non-`description` action, and a
    manifest that cannot be published is held back with a reason rather than
    failing the run. Note what it does *not* do: it does not filter manifests
    by the number. On the `--stacked` path the run just opened a *different*
    PR from the one the container was reviewing, and the reviewed PR's comments
    are exactly what there is to offer — gate 2 in `resolve_target` already
    restricts every manifest to a PR this container owns, which is the check
    that matters. The number names the PR this run touched; the mode is what it
    decides.

    `confirm` is injected so the CLI owns prompting and this stays unit
    testable; it is handed the number of actions and answers once, for all of
    them — the plan *is* the question, so there is never a second one.
    `can_prompt=False` degrades the offer to a single line naming the count and
    the command that publishes it: no plan is rendered and `confirm` is never
    called. `jailbee review apply` leaves it True, because its own `confirm`
    already decides what a non-interactive run means (`-y`, or a refusal).

    `outbox` is a snapshot the caller has already read; without one the outbox
    is read here, and a container that will not answer is reported and treated
    as nothing pending rather than raised — the offer's caller has a PR on
    screen that must not be retracted by this.
    """
    for_offer = pr_number is not None
    uid = cfg.container_user.uid
    if outbox is None:
        try:
            outbox = read_outbox(incus, container, uid=uid)
        except OutboxReadError as e:
            warn(f"{e}; nothing was offered.")
            return 0
    if not outbox.manifest_names:
        return 0

    targets, refusals, notes = _gate_manifests(
        cfg, incus, container, outbox, short, force=force, comments_only=for_offer
    )
    for message in refusals:
        error_plain(message)
    for message in notes:
        warn_plain(message)
    if not targets:
        # Refusals are failures; a deferral or a held-back manifest only means
        # the work belongs to another command, which is not a reason to fail.
        return len(refusals)

    plans = [(t, read_progress(outbox, t.manifest.name)) for t in targets]
    total = sum(len(pending_indices(t.manifest, p)) for t, p in plans)
    if not can_prompt:
        plural = "" if total == 1 else "s"
        info(
            f"{total} pending PR comment{plural} in {short}: "
            f"`jailbee review apply {short}` publishes {'it' if total == 1 else 'them'}."
        )
        return len(refusals)

    for target, progress in plans:
        _print_plan(cfg, short, target, progress)
    _print_identity(cfg, total)

    if dry_run:
        info("Dry run: nothing was published.")
        # A refusal sets the exit code on every path: something the container
        # wrote will not be published, and a script has to be able to see that
        # whether or not this run was going to publish anything anyway.
        return len(refusals)
    if total == 0:
        # Everything here landed on an earlier run that then failed to record
        # it. There is nothing to publish and so nothing to confirm — only the
        # bookkeeping below, which deletes the spent manifests.
        info("Every action here has already been published; finishing the bookkeeping.")
    elif not confirm(total):
        info(
            f"Nothing published. `jailbee review apply {short}` posts them later."
            if for_offer
            else "Nothing published."
        )
        return len(refusals)

    failures = len(refusals)
    for position, (target, progress) in enumerate(plans):
        outcome = apply_manifest(cfg, incus, container, target, progress, uid=uid)
        _print_receipts(target, outcome)
        stop = False
        try:
            finalize(incus, container, outbox, target, outcome, uid=uid)
        except FinalizeError as e:
            # The GitHub side is settled but the container could not be told.
            # Publishing the next manifest would post more that nothing can
            # record — the very thing the sidecar exists to prevent.
            error_plain(str(e))
            stop = True
        if outcome.failure is not None:
            error_plain(f"{target.manifest.name}: {outcome.failure}")
            warn_plain(f"{target.manifest.name} is still pending; re-running skips what landed.")
            stop = True
        # `finalize` deletes a spent manifest together with the body files no
        # *other* manifest in this snapshot references. Drop the spent one from
        # the snapshot so a `.md` shared by two completed manifests doesn't
        # look referenced by each of them in turn and outlive them both — the
        # same hazard `jailbee review drop` guards against.
        if not pending_indices(
            target.manifest,
            Progress(applied=progress.applied | set(outcome.applied), urls={}),
        ):
            outbox = Outbox(
                files={k: v for k, v in outbox.files.items() if k != target.manifest.name}
            )
        if stop:
            failures += 1
            left = [t.manifest.name for t, _ in plans[position + 1 :]]
            if left:
                warn_plain(f"Stopped here; still pending: {', '.join(left)}")
            break
    return failures
