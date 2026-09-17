"""Typed host-side GitHub issue operations through ``gh api``."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeGuard

_HTTP_STATUS_RE = re.compile(r"\bHTTP\s+([45]\d{2})\b", re.IGNORECASE)
_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class IssueSnapshot:
    """GitHub fields used to preflight and revalidate one issue."""

    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    state: Literal["open", "closed"]
    url: str
    is_pull_request: bool


@dataclass(frozen=True)
class MutationReceipt:
    """Durable identifiers returned by one successful GitHub mutation."""

    issue: int
    url: str


class IssueGithubReadError(RuntimeError):
    """A host-side GitHub read failed or returned an unusable response."""


class IssueGithubMutationError(RuntimeError):
    """A GitHub mutation failed, possibly after taking effect remotely."""

    def __init__(self, message: str, *, uncertain: bool) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def _run_api(
    repo_root: Path,
    endpoint: str,
    *,
    method: Literal["GET", "POST", "PATCH"] = "GET",
    payload: Mapping[str, object] | None = None,
    mutation: bool = False,
    paginate: bool = False,
) -> object:
    """Run the module's sole subprocess boundary and return decoded JSON."""
    cmd = ["gh", "api", endpoint]
    if method != "GET":
        cmd.extend(["--method", method, "--input", "-"])
    if paginate:
        cmd.extend(["--paginate", "--slurp"])

    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        if mutation:
            raise IssueGithubMutationError(
                "GitHub mutation could not start because 'gh' is unavailable",
                uncertain=False,
            ) from None
        raise IssueGithubReadError("GitHub read failed because 'gh' is unavailable") from None
    except KeyboardInterrupt:
        if mutation:
            raise IssueGithubMutationError(
                "GitHub mutation was interrupted after dispatch",
                uncertain=True,
            ) from None
        raise IssueGithubReadError("GitHub read failed") from None
    except (OSError, UnicodeError, subprocess.SubprocessError):
        if mutation:
            raise IssueGithubMutationError(
                "GitHub mutation transport failed after dispatch",
                uncertain=True,
            ) from None
        raise IssueGithubReadError("GitHub read failed") from None

    if proc.returncode != 0:
        if mutation:
            status = _http_status(proc.stderr)
            definite = proc.returncode > 0 and status is not None and 400 <= status < 500
            message = (
                "GitHub mutation was rejected"
                if definite
                else "GitHub mutation outcome is uncertain"
            )
            raise IssueGithubMutationError(message, uncertain=not definite)
        raise IssueGithubReadError("GitHub read failed")

    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        if mutation:
            raise IssueGithubMutationError(
                "GitHub mutation succeeded but returned an unreadable response",
                uncertain=True,
            ) from None
        raise IssueGithubReadError("GitHub read failed: unreadable response") from None


def _http_status(stderr: str) -> int | None:
    match = _HTTP_STATUS_RE.search(stderr)
    return int(match.group(1)) if match is not None else None


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _read_object(value: object) -> dict[str, object]:
    if not _is_object(value):
        raise IssueGithubReadError("GitHub read failed: unexpected response")
    return value


def _read_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise IssueGithubReadError("GitHub read failed: unexpected response")
    return value


def _read_number(value: object) -> int:
    if type(value) is not int:
        raise IssueGithubReadError("GitHub read failed: unexpected response")
    return value


def _mutation_receipt(value: object, *, issue: int | None = None) -> MutationReceipt:
    if not _is_object(value):
        raise IssueGithubMutationError(
            "GitHub mutation returned an unusable response", uncertain=True
        )
    url = value.get("html_url")
    response_number = value.get("number") if issue is None else issue
    if not isinstance(url, str) or not url or type(response_number) is not int:
        raise IssueGithubMutationError(
            "GitHub mutation returned an unusable response", uncertain=True
        )
    return MutationReceipt(issue=response_number, url=url)


def current_login(repo_root: Path) -> str:
    """Return the login authenticated by the host's ``gh`` configuration."""
    data = _read_object(_run_api(repo_root, "user"))
    return _read_string(data.get("login"))


def list_labels(repo_root: Path, repo: str) -> Mapping[str, str]:
    """Return canonical repository label names keyed by case-folded spelling."""
    data = _run_api(repo_root, f"repos/{repo}/labels", paginate=True)
    if not _is_array(data):
        raise IssueGithubReadError("GitHub read failed: unexpected response")

    labels: dict[str, str] = {}
    for page in data:
        if not _is_array(page):
            raise IssueGithubReadError("GitHub read failed: unexpected response")
        for raw_label in page:
            label = _read_object(raw_label)
            name = _read_string(label.get("name"))
            labels[name.casefold()] = name
    return labels


def get_issue(repo_root: Path, repo: str, number: int) -> IssueSnapshot:
    """Return the current host-visible snapshot of one issue-shaped object."""
    data = _read_object(_run_api(repo_root, f"repos/{repo}/issues/{number}"))
    raw_labels = data.get("labels")
    if not _is_array(raw_labels):
        raise IssueGithubReadError("GitHub read failed: unexpected response")
    labels = tuple(_read_string(_read_object(label).get("name")) for label in raw_labels)

    raw_body = data.get("body")
    if raw_body is not None and not isinstance(raw_body, str):
        raise IssueGithubReadError("GitHub read failed: unexpected response")
    raw_state = data.get("state")
    if raw_state not in ("open", "closed"):
        raise IssueGithubReadError("GitHub read failed: unexpected response")

    return IssueSnapshot(
        number=_read_number(data.get("number")),
        title=_read_string(data.get("title")),
        body=raw_body or "",
        labels=labels,
        state=raw_state,
        url=_read_string(data.get("html_url")),
        is_pull_request="pull_request" in data,
    )


def create_issue(
    repo_root: Path,
    repo: str,
    *,
    title: str,
    body: str,
    labels: tuple[str, ...],
) -> MutationReceipt:
    """Create an issue in the explicit host-resolved repository."""
    data = _run_api(
        repo_root,
        f"repos/{repo}/issues",
        method="POST",
        payload={"title": title, "body": body, "labels": list(labels)},
        mutation=True,
    )
    return _mutation_receipt(data)


def edit_issue(
    repo_root: Path,
    repo: str,
    number: int,
    *,
    title: str | None,
    body: str | None,
) -> MutationReceipt:
    """Replace the requested title and/or body fields in one issue."""
    payload: dict[str, object] = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    data = _run_api(
        repo_root,
        f"repos/{repo}/issues/{number}",
        method="PATCH",
        payload=payload,
        mutation=True,
    )
    return _mutation_receipt(data)


def replace_labels(
    repo_root: Path,
    repo: str,
    number: int,
    *,
    labels: tuple[str, ...],
) -> MutationReceipt:
    """Atomically replace an issue's labels with the complete supplied tuple."""
    data = _run_api(
        repo_root,
        f"repos/{repo}/issues/{number}",
        method="PATCH",
        payload={"labels": list(labels)},
        mutation=True,
    )
    return _mutation_receipt(data)


def add_comment(
    repo_root: Path,
    repo: str,
    number: int,
    *,
    body: str,
) -> MutationReceipt:
    """Add one top-level comment to an issue."""
    data = _run_api(
        repo_root,
        f"repos/{repo}/issues/{number}/comments",
        method="POST",
        payload={"body": body},
        mutation=True,
    )
    return _mutation_receipt(data, issue=number)


def set_state(
    repo_root: Path,
    repo: str,
    number: int,
    *,
    state: Literal["open", "closed"],
    reason: Literal["completed", "not_planned"] | None,
) -> MutationReceipt:
    """Close an issue with a reason or reopen it."""
    state_reason = "reopened" if state == "open" else reason
    data = _run_api(
        repo_root,
        f"repos/{repo}/issues/{number}",
        method="PATCH",
        payload={"state": state, "state_reason": state_reason},
        mutation=True,
    )
    return _mutation_receipt(data)
