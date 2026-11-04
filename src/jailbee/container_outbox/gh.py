"""Read-only GitHub metadata lookups for container outbox commands."""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class PrView:
    number: int
    head_sha: str
    repo: str


def run_json(argv: list[str], *, cwd: str | None = None) -> object:
    """Run gh once and decode its JSON response."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=30
        )
    except FileNotFoundError as exc:
        raise GhError("gh not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh timed out; retry `{ ' '.join(argv) }`") from exc
    if result.returncode:
        detail = result.stderr.strip() or "gh failed"
        raise GhError(f"{detail}; retry `{ ' '.join(argv) }` from the same directory")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GhError(f"gh returned malformed JSON; retry `{ ' '.join(argv) }`") from exc


def _issue_target(repo: str | None) -> tuple[str | None, str | None]:
    if repo is None or repo == ".":
        return None, None
    if repo.startswith("github:"):
        return None, repo.removeprefix("github:")
    return repo, None


def issue_view(repo: str | None, number: int) -> IssueView:
    argv = ["gh", "issue", "view", str(number), "--json", "title,body,labels,state"]
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
        if not all(isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels):
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
        title, body = value["title"], value["body"]
        if not isinstance(title, str) or not isinstance(body, str):
            raise ValueError("invalid issue fields")
        return IssueView(title, body, names, normalized_state)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise GhError(f"invalid issue response; retry `{ ' '.join(argv) }`: {exc}") from exc


def pr_view(number: int | None) -> PrView:
    argv = ["gh", "pr", "view"]
    if number is not None:
        argv.append(str(number))
    argv.extend(["--json", "number,headRefOid,url"])
    try:
        value = run_json(argv)
        if not isinstance(value, dict):
            raise ValueError("response must be an object")
        pr_number = value["number"]
        head_sha = value["headRefOid"]
        url = value["url"]
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if (
            not isinstance(pr_number, int) or not isinstance(head_sha, str)
            or parsed.hostname != "github.com" or len(parts) < 4 or parts[2] != "pull"
        ):
            raise ValueError("invalid number, head SHA, or GitHub PR URL")
        return PrView(pr_number, head_sha, f"{parts[0]}/{parts[1]}")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise GhError(f"invalid PR response; retry `{ ' '.join(argv) }`: {exc}") from exc
