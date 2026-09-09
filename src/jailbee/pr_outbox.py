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

from jailbee import git, pr
from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import Incus, IncusError
from jailbee.tui import warn

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.pr import PrInfo

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


def comment_anchor(comment: LineComment) -> str:
    """One line comment's anchor: ``src/x.py:88`` or ``src/x.py:120-134``.

    Public because `jb review show` prints the same anchor above the body it
    shows in full, and two spellings of "where this comment lands" would be
    two things to keep in step.
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
                lines.append(f"  {comment_anchor(comment)}: {_first_line(comment.body)}")
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
        to_delete = [manifest_path, sidecar_path]
        to_delete.extend(
            f"{outbox_dir()}/{name}" for name in _orphaned_body_files(outbox, manifest.name)
        )
        try:
            incus.exec(container, ["rm", "-f", *to_delete], uid=uid)
        except IncusError as e:
            raise FinalizeError(
                f"manifest {manifest.name} is fully applied but could not be "
                f"deleted ({e}); it will be cleaned up on a later run"
            ) from e


def drop_manifest(
    incus: Incus, container: str, outbox: Outbox, name: str, *, uid: int | None
) -> list[str]:
    """Delete manifest `name` from the container's outbox without applying it.

    The discard counterpart of `finalize`'s cleanup, and deliberately the
    same deletion: one `rm -f` over the manifest, its progress sidecar (when
    one exists) and any `body_file` no *other* manifest in `outbox` still
    references — see `_orphaned_body_files`. Nothing is posted to GitHub and
    no `applied.log` line is written: dropping is the user saying this
    proposal will never be published, so there is nothing to record.

    Returns the outbox-relative names that were deleted, so a caller
    dropping several manifests in a row can shrink its own `outbox`
    snapshot between calls — without that, a body file shared by two
    manifests looks referenced while each of them is dropped and would
    survive them both.

    Raises `FinalizeError` if the deletion fails; the manifest is then still
    pending, exactly as it was.
    """
    sidecar_name = f"{name}.progress.json"

    names = [name]
    if sidecar_name in outbox.files:
        names.append(sidecar_name)
    names.extend(_orphaned_body_files(outbox, name))

    try:
        incus.exec(container, ["rm", "-f", *(f"{outbox_dir()}/{n}" for n in names)], uid=uid)
    except IncusError as e:
        raise FinalizeError(
            f"manifest {name} could not be deleted ({e}); it is still pending"
        ) from e
    return names


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
