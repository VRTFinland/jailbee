"""Host-side GitHub image uploads through the authenticated ``gh`` CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from jailbee import pr
from jailbee.github_repo import is_github_slug

_TIMEOUT_SECONDS = 120
_IGNORED_GH_ENV = {"GH_DEBUG", "GH_HOST", "GH_REPO", "GH_API_HOST"}
_ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_CANONICAL_URL_RE = re.compile(
    r"/user-attachments/assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.ASCII | re.IGNORECASE,
)


@dataclass(frozen=True)
class UploadTarget:
    """Verified GitHub repository identity bound to one upload."""

    host: str
    repo: str
    repository_id: int


class UploadError(RuntimeError):
    """Base class for safe, user-facing GitHub attachment errors."""


class UploadRejectedError(UploadError):
    """GitHub returned a definite client-side rejection for the upload."""


class UploadUncertainError(UploadError):
    """The upload may have succeeded; inspect remote state and do not retry blindly."""


class UploadTargetError(UploadError):
    """The repository could not be resolved as the requested GitHub target."""


def _run(
    repo_root: Path,
    args: list[str],
    *,
    input_path: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = pr.attachment_request(
            repo_root,
            args,
            timeout=_TIMEOUT_SECONDS,
            env={
                **{key: value for key, value in os.environ.items() if key not in _IGNORED_GH_ENV},
                "GH_HOST": "github.com",
                "GH_PROMPT_DISABLED": "1",
            },
        )
        return proc
    except FileNotFoundError:
        raise UploadError("GitHub request failed because 'gh' is unavailable") from None
    except subprocess.TimeoutExpired:
        if input_path is not None:
            raise UploadUncertainError(
                "GitHub upload timed out after dispatch; inspect remote state before retrying"
            ) from None
        raise UploadTargetError("GitHub repository lookup timed out") from None
    except (OSError, subprocess.SubprocessError):
        if input_path is not None:
            raise UploadUncertainError(
                "GitHub upload transport failed after dispatch; inspect remote state"
                " before retrying"
            ) from None
        raise UploadTargetError("GitHub repository lookup failed") from None


def _parse_http_response(raw: str) -> tuple[int | None, str]:
    text = raw
    while text.startswith("HTTP/"):
        separator = "\r\n\r\n" if "\r\n\r\n" in text else "\n\n"
        if separator not in text:
            return None, ""
        headers, text = text.split(separator, 1)
        match = re.match(r"HTTP/\S+\s+(\d{3})", headers)
        if match is None:
            return None, text
        status = int(match.group(1))
        if not text.startswith("HTTP/"):
            return status, text
    return None, text


def resolve_target(repo_root: Path, repo: str) -> UploadTarget:
    """Resolve and verify an explicit GitHub.com owner/repository slug."""
    if repo.startswith("github.com/"):
        host, separator, slug = repo.partition("/")
        if not separator or not slug:
            raise ValueError("Repository must be [github.com/]OWNER/REPO")
        repo = slug
    else:
        host = "github.com"
    if host.casefold() != "github.com" or not is_github_slug(repo):
        raise ValueError("Repository must be [github.com/]OWNER/REPO on github.com")

    result = _run(
        repo_root,
        [f"repos/{repo}", "--include", "-H", "Accept: application/vnd.github+json"],
    )
    if result.returncode != 0 or not isinstance(result.stdout, bytes):
        raise UploadTargetError(
            "GitHub repository lookup failed; verify access and repository name"
        )
    _status, body = _parse_http_response(result.stdout.decode("iso-8859-1"))
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise UploadTargetError("GitHub repository lookup returned an unusable response") from None
    if not isinstance(data, dict):
        raise UploadTargetError("GitHub repository lookup returned an unusable response")
    full_name = data.get("full_name")
    repository_id = data.get("id")
    if (
        not isinstance(full_name, str)
        or full_name.casefold() != repo.casefold()
        or type(repository_id) is not int
        or repository_id <= 0
    ):
        raise UploadTargetError("GitHub repository identity does not match the requested target")
    return UploadTarget(host="github.com", repo=full_name, repository_id=repository_id)


def _canonical_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or _CANONICAL_URL_RE.fullmatch(parsed.path) is None
    ):
        return None
    return value


def _validate_upload(path: Path, display_name: str, content_type: str) -> None:
    if (
        not isinstance(display_name, str)
        or not display_name
        or display_name in {".", ".."}
        or len(display_name.encode("utf-8")) > 255
        or any(char in display_name for char in "/\\")
        or any(ord(char) < 32 or ord(char) == 127 for char in display_name)
    ):
        raise ValueError("Image display name must be a safe filename")
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ValueError("Unsupported image content type")
    if not path.is_file():
        raise ValueError("Image path must name a regular file")


def upload_image(
    repo_root: Path,
    repo: str,
    path: Path,
    display_name: str,
    content_type: str,
) -> str:
    """Resolve a repository and upload an image bound to its verified identity."""
    target = resolve_target(repo_root, repo)
    return upload_to_target(repo_root, target, path, display_name, content_type)


def upload_to_target(
    repo_root: Path,
    target: UploadTarget,
    path: Path,
    display_name: str,
    content_type: str,
) -> str:
    """Upload an image using a previously verified repository identity."""
    _validate_upload(path, display_name, content_type)
    if target.host != "github.com" or not is_github_slug(target.repo) or target.repository_id <= 0:
        raise ValueError("Invalid verified GitHub upload target")
    query = (
        f"name={quote(display_name, safe='')}"
        f"&content_type={quote(content_type, safe='')}"
        f"&repository_id={target.repository_id}"
    )
    result = _run(
        repo_root,
        [
            f"https://uploads.github.com/user-attachments/assets?{query}",
            "--method",
            "POST",
            "--include",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "Content-Type: application/octet-stream",
            "--input",
            str(path),
        ],
        input_path=path,
    )
    if not isinstance(result.stdout, bytes):
        raise UploadUncertainError(
            "GitHub upload returned an unusable response; inspect remote state before retrying"
        )
    raw = result.stdout.decode("iso-8859-1")
    status, body = _parse_http_response(raw)
    if result.returncode != 0:
        if result.returncode > 0 and status is not None and 400 <= status < 500:
            raise UploadRejectedError(f"GitHub rejected the image upload (HTTP {status})")
        raise UploadUncertainError(
            "GitHub upload outcome is uncertain; inspect remote state before retrying"
        )
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise UploadUncertainError(
            "GitHub upload returned an unreadable response; inspect remote state before retrying"
        ) from None
    url = _canonical_url(data.get("href") or data.get("url") if isinstance(data, dict) else None)
    if url is None:
        raise UploadUncertainError(
            "GitHub upload returned no canonical asset URL; inspect remote state before retrying"
        )
    return url
