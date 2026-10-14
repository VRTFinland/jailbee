"""Real-git tests: submodules that exist on some branches only.

These run the real `git` binary in tmp_path — never `incus` — because the
behaviour under test is git's own (gitdir layout, core.worktree, read-tree).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from jailbee import submodules


@pytest.fixture(autouse=True)
def git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "t")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "t@t")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Rig:
    top: Path
    subup: Path
    inup: Path


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    tmp_path = tmp_path.resolve()
    inup = tmp_path / "inup"
    _git(tmp_path, "init", "-q", "-b", "main", str(inup))
    (inup / "i").write_text("i\n")
    _git(inup, "add", "i")
    _git(inup, "commit", "-q", "-m", "i1")

    subup = tmp_path / "subup"
    _git(tmp_path, "init", "-q", "-b", "main", str(subup))
    (subup / ".gitignore").write_text(".env\n")
    (subup / "d").mkdir()
    (subup / "d" / "f").write_text("1\n")
    _git(subup, "add", ".")
    _git(subup, "commit", "-q", "-m", "s1")
    _git(subup, "submodule", "add", "-q", str(inup), "inner")
    _git(subup, "commit", "-q", "-m", "add inner")

    top = tmp_path / "top"
    _git(tmp_path, "init", "-q", "-b", "main", str(top))
    _git(top, "commit", "-q", "--allow-empty", "-m", "t1")
    _git(top, "checkout", "-q", "-b", "dev")
    _git(top, "submodule", "add", "-q", str(subup), "sub")
    _git(top, "submodule", "update", "-q", "--init", "--recursive")
    _git(top, "commit", "-q", "-m", "add sub")
    return Rig(top=top, subup=subup, inup=inup)


def test_locate_finds_a_checked_out_submodule(rig: Rig) -> None:
    sub = submodules.resolve_host_subrepo(rig.top, "sub")
    assert sub is not None
    assert sub.worktree == rig.top / "sub"
    assert sub.gitdir == rig.top / ".git" / "modules" / "sub"
    assert sub.root == rig.top / "sub"


def test_locate_finds_a_gitdir_only_submodule_by_commit(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    assert not (rig.top / "sub").exists()
    sub = submodules.resolve_host_subrepo(rig.top, "sub", at="dev")
    assert sub is not None
    assert sub.worktree is None
    assert sub.root == rig.top / ".git" / "modules" / "sub"
    assert _git(sub.root, "rev-parse", "HEAD")


def test_locate_falls_back_to_name_equals_path_without_gitmodules(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    sub = submodules.resolve_host_subrepo(rig.top, "sub")
    assert sub is not None and sub.worktree is None


def test_locate_finds_a_nested_gitdir(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    inner = submodules.resolve_host_subrepo(rig.top, "sub/inner", at="dev")
    assert inner is not None
    assert inner.gitdir == rig.top / ".git" / "modules" / "sub" / "modules" / "inner"


def test_locate_heals_a_stale_core_worktree(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "main")
    import shutil

    shutil.rmtree(rig.top / "sub")
    sub = submodules.resolve_host_subrepo(rig.top, "sub", at="dev")
    assert sub is not None and sub.worktree is None
    assert _git(sub.root, "rev-parse", "HEAD")


def test_locate_uses_the_declared_name_not_the_path(tmp_path: Path, rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "dev")
    _git(rig.top, "submodule", "add", "-q", "--name", "lib-core", str(rig.inup), "libs/core")
    _git(rig.top, "commit", "-q", "-m", "named")
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    sub = submodules.resolve_host_subrepo(rig.top, "libs/core", at="dev")
    assert sub is not None
    assert sub.gitdir == rig.top / ".git" / "modules" / "lib-core"


def test_locate_returns_none_for_an_unknown_submodule(rig: Rig) -> None:
    assert submodules.resolve_host_subrepo(rig.top, "nope") is None


def test_host_subrepo_exists_is_true_for_a_gitdir_only_submodule(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    assert submodules.host_subrepo_exists(rig.top, "sub")


def test_host_subrepo_root_prefers_the_located_root_and_falls_back_to_the_path(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    assert submodules.host_subrepo_root(rig.top, "sub") == rig.top / ".git" / "modules" / "sub"
    assert submodules.host_subrepo_root(rig.top, "nope") == rig.top / "nope"
