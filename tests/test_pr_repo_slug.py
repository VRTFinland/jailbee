"""The repo slug a PR link points at follows gh's own repository resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from jailbee import pr


def _repo(path: Path, remotes: dict[str, str], *, resolved: str | None = None) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "-C", str(path), "remote", "add", name, url], check=True)
    if resolved is not None:
        subprocess.run(
            ["git", "-C", str(path), "config", f"remote.{resolved}.gh-resolved", "base"],
            check=True,
        )
    return path


def test_single_remote(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r", {"origin": "https://github.com/acme/widgets.git"})
    assert pr.pr_repo_slug(root, "origin") == "acme/widgets"


def test_fork_checkout_prefers_the_upstream_remote(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "r",
        {
            "origin": "https://github.com/me/widgets.git",
            "upstream": "git@github.com:acme/widgets.git",
        },
    )
    assert pr.pr_repo_slug(root, "origin") == "acme/widgets"


def test_gh_set_default_wins_over_upstream(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "r",
        {
            "origin": "https://github.com/me/widgets.git",
            "upstream": "https://github.com/acme/widgets.git",
        },
        resolved="origin",
    )
    assert pr.pr_repo_slug(root, "origin") == "me/widgets"


def test_non_github_upstream_falls_back_to_the_given_remote(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "r",
        {
            "origin": "https://github.com/me/widgets.git",
            "upstream": "https://gitlab.com/acme/widgets.git",
        },
    )
    assert pr.pr_repo_slug(root, "origin") == "me/widgets"


def test_no_github_remote_is_none(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r", {"origin": "https://example.com/x/y.git"})
    assert pr.pr_repo_slug(root, "origin") is None
