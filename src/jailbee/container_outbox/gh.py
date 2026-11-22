"""Read-only GitHub metadata lookups for container outbox commands."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse


class GhError(Exception):
    """A user-facing failure while reading GitHub metadata."""


@dataclass(frozen=True)
class IssueView:
    title: str
    body: str
    labels: tuple[str, ...]
    state: Literal["open", "closed"]
    assignees: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrView:
    number: int
    head_sha: str
    repo: str


def run_text(argv: list[str], *, cwd: str | None = None) -> str:
    """Run gh once and return its standard output."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=30
        )
    except FileNotFoundError as exc:
        if cwd is not None and exc.filename == cwd:
            raise GhError(f"cannot use working directory {cwd!r}: {exc.strerror}") from exc
        raise GhError("gh not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh timed out; retry `{' '.join(argv)}`") from exc
    except OSError as exc:
        if cwd is not None and exc.filename == cwd:
            raise GhError(f"cannot use working directory {cwd!r}: {exc.strerror}") from exc
        raise GhError(f"cannot run gh: {exc}") from exc
    if result.returncode:
        raise GhError(result.stderr.strip() or "gh failed")
    return result.stdout


def run_json(argv: list[str], *, cwd: str | None = None) -> object:
    """Run gh once and decode its JSON response."""
    stdout = run_text(argv, cwd=cwd)
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GhError(f"gh returned malformed JSON; retry `{' '.join(argv)}`") from exc


def _issue_target(repo: str | None) -> tuple[str | None, str | None]:
    if repo is None or repo == ".":
        return None, None
    if repo.startswith("github:"):
        return None, repo.removeprefix("github:")
    return repo, None


def issue_view(repo: str | None, number: int) -> IssueView:
    argv = ["gh", "issue", "view", str(number), "--json", "title,body,labels,state,assignees"]
    cwd, slug = _issue_target(repo)
    if slug is not None:
        argv.extend(["-R", slug])
    try:
        value = run_json(argv, cwd=cwd)
        if not isinstance(value, dict):
            raise ValueError("response must be an object")
        labels = value["labels"]
        if not isinstance(labels, list):
            raise ValueError("labels must be a list")
        if not all(
            isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels
        ):
            raise ValueError("labels must contain names")
        names = tuple(label["name"] for label in labels)
        state_value = value["state"]
        if not isinstance(state_value, str):
            raise ValueError("state must be text")
        state = state_value.lower()
        if state == "open":
            normalized_state: Literal["open", "closed"] = "open"
        elif state == "closed":
            normalized_state = "closed"
        else:
            raise ValueError("state must be OPEN or CLOSED")
        assignees = value["assignees"]
        if not isinstance(assignees, list):
            raise ValueError("assignees must be a list")
        if not all(
            isinstance(user, dict) and isinstance(user.get("login"), str) for user in assignees
        ):
            raise ValueError("assignees must contain logins")
        logins = tuple(user["login"] for user in assignees)
        title, body = value["title"], value["body"]
        if not isinstance(title, str) or not isinstance(body, str):
            raise ValueError("invalid issue fields")
        return IssueView(title, body, names, normalized_state, logins)
    except GhError as exc:
        raise GhError(f"{exc}; retry `{' '.join(argv)}`") from exc
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise GhError(f"invalid issue response; retry `{' '.join(argv)}`: {exc}") from exc


def repo_view() -> str:
    """Resolve the repository without requiring a branch or an existing PR."""
    from jailbee.github_repo import is_github_slug

    argv = ["gh", "repo", "view", "--json", "nameWithOwner"]
    try:
        value = run_json(argv)
        repo = value.get("nameWithOwner") if isinstance(value, dict) else None
        if not isinstance(repo, str) or not is_github_slug(repo):
            raise ValueError("invalid repository name")
        return repo
    except (GhError, ValueError) as exc:
        raise GhError(f"{exc}; retry `{' '.join(argv)}` or supply --repo owner/name") from exc


def pr_body(number: int, *, repo: str | None = None) -> str:
    """Read the current replacement body for a title-only description edit."""
    argv = ["gh", "pr", "view", str(number), "--json", "body"]
    if repo is not None:
        argv.extend(["-R", repo])
    try:
        value = run_json(argv)
        if not isinstance(value, dict):
            raise ValueError("response must be an object")
        body = value.get("body")
        if not isinstance(body, str):
            raise ValueError("body must be text")
        return body
    except (GhError, ValueError) as exc:
        raise GhError(f"{exc}; retry `{' '.join(argv)}`") from exc


def pr_view(number: int | None, *, cwd: str | None = None) -> PrView:
    argv = ["gh", "pr", "view"]
    if number is not None:
        argv.append(str(number))
    argv.extend(["--json", "number,headRefOid,url"])
    try:
        value = run_json(argv, cwd=cwd)
        if not isinstance(value, dict):
            raise ValueError("response must be an object")
        pr_number = value["number"]
        head_sha = value["headRefOid"]
        url = value["url"]
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if (
            not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number < 1
            or not isinstance(head_sha, str)
            or not isinstance(url, str)
            or parsed.hostname != "github.com"
            or len(parts) < 4
            or parts[2] != "pull"
            or not parts[0]
            or not parts[1]
        ):
            raise ValueError("invalid number, head SHA, or GitHub PR URL")
        return PrView(pr_number, head_sha, f"{parts[0]}/{parts[1]}")
    except GhError as exc:
        raise GhError(f"{exc}; retry `{' '.join(argv)}`") from exc
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise GhError(f"invalid PR response; retry `{' '.join(argv)}`: {exc}") from exc


@dataclass(frozen=True)
class CommentView:
    """One issue or PR conversation comment as the container's gh reads it."""

    id: int
    author: str
    body: str
    created_at: str
    url: str
    issue: int | None
    review_id: int | None = None


_ISSUE_API_URL_RE = re.compile(r"https://api\.github\.com/repos/[^/]+/[^/]+/issues/([1-9][0-9]*)")


def _api_repo(repo: str | None) -> tuple[str | None, str]:
    """Working directory and ``repos/...`` prefix for one manifest repo value.

    gh fills ``{owner}/{repo}`` from the checkout it runs in.
    """
    cwd, slug = _issue_target(repo)
    return cwd, f"repos/{slug}" if slug is not None else "repos/{owner}/{repo}"


_PULL_API_URL_RE = re.compile(r"https://api\.github\.com/repos/[^/]+/[^/]+/pulls/([1-9][0-9]*)")


def _comment_view(
    value: object, *, parent_key: str = "issue_url", parent_re: re.Pattern[str] = _ISSUE_API_URL_RE
) -> CommentView:
    if not isinstance(value, dict):
        raise ValueError("comment must be an object")
    user = value.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    cid, body = value.get("id"), value.get("body")
    created, url, issue_url = value.get("created_at"), value.get("html_url"), value.get(parent_key)
    if type(cid) is not int or not isinstance(author, str):
        raise ValueError("comment needs an id and an author")
    if body is not None and not isinstance(body, str):
        raise ValueError("comment body must be text")
    if not isinstance(url, str) or not isinstance(created, str):
        raise ValueError("comment needs a URL and a creation time")
    match = parent_re.fullmatch(issue_url) if isinstance(issue_url, str) else None
    review = value.get("pull_request_review_id")
    return CommentView(
        cid,
        author,
        body or "",
        created,
        url,
        int(match[1]) if match else None,
        review if type(review) is int else None,
    )


def issue_comment(repo: str | None, comment_id: int) -> CommentView:
    """Read one issue or PR conversation comment by its numeric id."""
    cwd, prefix = _api_repo(repo)
    argv = ["gh", "api", f"{prefix}/issues/comments/{comment_id}"]
    try:
        return _comment_view(run_json(argv, cwd=cwd))
    except GhError as exc:
        raise GhError(f"{exc}; retry `{' '.join(argv)}`") from exc
    except ValueError as exc:
        raise GhError(f"invalid comment response; retry `{' '.join(argv)}`: {exc}") from exc


def review_comment(repo: str | None, comment_id: int) -> CommentView:
    """Read one pull-request review (line) comment; ``issue`` is its PR number."""
    cwd, prefix = _api_repo(repo)
    argv = ["gh", "api", f"{prefix}/pulls/comments/{comment_id}"]
    try:
        return _comment_view(
            run_json(argv, cwd=cwd), parent_key="pull_request_url", parent_re=_PULL_API_URL_RE
        )
    except GhError as exc:
        raise GhError(f"{exc}; retry `{' '.join(argv)}`") from exc
    except ValueError as exc:
        raise GhError(f"invalid comment response; retry `{' '.join(argv)}`: {exc}") from exc


def review_state(repo: str | None, pr: int, review_id: int) -> str:
    """The state (``PENDING``, ``COMMENTED``, ...) of one review on PR #`pr`."""
    cwd, prefix = _api_repo(repo)
    argv = ["gh", "api", f"{prefix}/pulls/{pr}/reviews/{review_id}"]
    try:
        value = run_json(argv, cwd=cwd)
    except GhError as exc:
        raise GhError(f"{exc}; retry `{' '.join(argv)}`") from exc
    state = value.get("state") if isinstance(value, dict) else None
    if not isinstance(state, str):
        raise GhError(f"invalid review response; retry `{' '.join(argv)}`")
    return state


def _decode_pages(text: str) -> list[list[object]]:
    """The JSON arrays ``gh api --paginate`` prints back to back, one per page."""
    decoder = json.JSONDecoder()
    pages: list[list[object]] = []
    position = 0
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            return pages
        page, position = decoder.raw_decode(text, position)
        if not isinstance(page, list):
            raise ValueError("page must be a list")
        pages.append(page)


def issue_comments(repo: str | None, number: int) -> tuple[CommentView, ...]:
    """Every conversation comment on an issue or pull request, oldest first."""
    cwd, prefix = _api_repo(repo)
    argv = ["gh", "api", f"{prefix}/issues/{number}/comments", "--paginate"]
    try:
        pages = _decode_pages(run_text(argv, cwd=cwd))
        return tuple(_comment_view(item) for page in pages for item in page)
    except GhError as exc:
        raise GhError(f"{exc}; retry `{' '.join(argv)}`") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise GhError(f"invalid comments response; retry `{' '.join(argv)}`: {exc}") from exc
