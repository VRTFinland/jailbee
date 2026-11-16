from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import pytest_mock

from jailbee.attachment_github import (
    UploadRejectedError,
    UploadTarget,
    UploadTargetError,
    UploadUncertainError,
    resolve_target,
    upload_image,
    upload_to_target,
)

ROOT = Path("/repo")


def completed(stdout: str | bytes, returncode: int = 0, stderr: str = "") -> Mock:
    output = stdout.encode() if isinstance(stdout, str) else stdout
    return Mock(returncode=returncode, stdout=output, stderr=stderr.encode())


def test_resolve_target_verifies_private_repository_identity(
    mocker: pytest_mock.MockerFixture,
) -> None:
    run = mocker.patch(
        "jailbee.attachment_github.subprocess.run",
        return_value=completed(json.dumps({"id": 812, "full_name": "Org/Docs", "private": True})),
    )

    target = resolve_target(ROOT, "github.com/Org/Docs")

    assert target == UploadTarget(host="github.com", repo="Org/Docs", repository_id=812)
    assert run.call_args.args[0] == [
        "gh",
        "api",
        "repos/Org/Docs",
        "--include",
        "-H",
        "Accept: application/vnd.github+json",
    ]
    assert run.call_args.kwargs["cwd"] == ROOT
    assert run.call_args.kwargs["timeout"] == 120
    assert run.call_args.kwargs["env"]["GH_HOST"] == "github.com"
    assert "GH_REPO" not in run.call_args.kwargs["env"]
    assert "GH_DEBUG" not in run.call_args.kwargs["env"]


def test_resolve_target_rejects_github_enterprise_and_identity_mismatch(
    mocker: pytest_mock.MockerFixture,
) -> None:
    run = mocker.patch("jailbee.attachment_github.subprocess.run")
    with pytest.raises(ValueError, match=r"github\.com"):
        resolve_target(ROOT, "github.example.com/Org/Docs")
    run.assert_not_called()

    run.return_value = completed(
        json.dumps({"id": 812, "full_name": "Elsewhere/Docs", "private": True})
    )
    with pytest.raises(UploadTargetError, match="identity"):
        resolve_target(ROOT, "Org/Docs")


def test_upload_image_uses_resolved_repository_and_returns_only_canonical_url(
    mocker: pytest_mock.MockerFixture, tmp_path: Path
) -> None:
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image")
    asset_id = "01234567-89ab-cdef-0123-456789abcdef"
    response = (
        b"HTTP/2 201 Created\r\nContent-Type: application/json\r\n\r\n"
        + json.dumps({"href": f"https://github.com/user-attachments/assets/{asset_id}"}).encode()
    )
    run = mocker.patch(
        "jailbee.attachment_github.subprocess.run",
        side_effect=[
            completed(json.dumps({"id": 812, "full_name": "Org/Docs", "private": True})),
            completed(response),
        ],
    )

    url = upload_image(ROOT, "Org/Docs", image, "diagram.png", "image/png")

    assert url == f"https://github.com/user-attachments/assets/{asset_id}"
    args = run.call_args.args[0]
    assert args[:2] == ["gh", "api"]
    endpoint = args[2]
    parsed = urlsplit(endpoint)
    assert parsed.scheme == "https" and parsed.netloc == "uploads.github.com"
    assert parsed.path == "/user-attachments/assets"
    assert "repository_id=812" in parsed.query
    assert "name=diagram.png" in parsed.query and "content_type=image%2Fpng" in parsed.query
    assert args[args.index("--input") + 1] == str(image)
    assert "Content-Type: application/octet-stream" in args
    assert run.call_args.kwargs["cwd"] == ROOT
    assert run.call_args.kwargs["timeout"] == 120


def test_upload_rejection_is_definite_but_transport_failure_is_uncertain(
    mocker: pytest_mock.MockerFixture, tmp_path: Path
) -> None:
    image = tmp_path / "diagram.png"
    image.touch()
    target = UploadTarget("github.com", "Org/Docs", 812)
    mocker.patch("jailbee.attachment_github.resolve_target", return_value=target)
    run = mocker.patch(
        "jailbee.attachment_github.subprocess.run",
        return_value=completed(b"HTTP/2 415 Unsupported Media Type\r\n\r\n{}", 1),
    )
    with pytest.raises(UploadRejectedError):
        upload_image(ROOT, "Org/Docs", image, "diagram.png", "image/png")

    run.side_effect = subprocess.TimeoutExpired(["gh"], 120)
    with pytest.raises(UploadUncertainError):
        upload_image(ROOT, "Org/Docs", image, "diagram.png", "image/png")


def test_upload_refuses_unsafe_names_types_and_noncanonical_urls(
    mocker: pytest_mock.MockerFixture, tmp_path: Path
) -> None:
    image = tmp_path / "diagram.png"
    image.touch()
    target = UploadTarget("github.com", "Org/Docs", 812)
    mocker.patch("jailbee.attachment_github.resolve_target", return_value=target)
    run = mocker.patch("jailbee.attachment_github.subprocess.run")
    with pytest.raises(ValueError):
        upload_image(ROOT, "Org/Docs", image, "../diagram.png", "image/png")
    with pytest.raises(ValueError):
        upload_image(ROOT, "Org/Docs", image, "diagram.png", "image/svg+xml")
    run.assert_not_called()

    run.return_value = completed(
        json.dumps(
            {
                "url": "https://github.com/user-attachments/assets/"
                "01234567-89ab-cdef-0123-456789abcdef?token=secret"
            }
        )
    )
    with pytest.raises(UploadUncertainError):
        upload_to_target(ROOT, target, image, "diagram.png", "image/png")


def test_upload_does_not_expose_gh_output_or_stderr(
    mocker: pytest_mock.MockerFixture, tmp_path: Path
) -> None:
    image = tmp_path / "diagram.png"
    image.touch()
    mocker.patch(
        "jailbee.attachment_github.resolve_target",
        return_value=UploadTarget("github.com", "Org/Docs", 812),
    )
    run = mocker.patch(
        "jailbee.attachment_github.subprocess.run",
        return_value=completed(b"token=secret", 1, "credential=private"),
    )
    with pytest.raises(UploadUncertainError) as caught:
        upload_to_target(
            ROOT,
            UploadTarget("github.com", "Org/Docs", 812),
            image,
            "diagram.png",
            "image/png",
        )
    assert "secret" not in str(caught.value)
    assert "private" not in str(caught.value)
    assert "token=secret" not in repr(run.call_args)
