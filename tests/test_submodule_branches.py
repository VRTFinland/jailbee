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
    _git(subup, "submodule", "add", "-q", "--name", "inner-custom", str(inup), "inner")
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
    assert inner.gitdir == rig.top / ".git" / "modules" / "sub" / "modules" / "inner-custom"


def test_nested_lookup_uses_pinned_parent_commit_not_child_head(rig: Rig) -> None:
    sub = rig.top / "sub"
    pinned = _git(sub, "rev-parse", "HEAD")
    _git(sub, "checkout", "-q", "-b", "other")
    (sub / ".gitmodules").write_text('[submodule "other-custom"]\n\tpath = inner\n\turl = unused\n')
    _git(sub, "add", ".gitmodules")
    _git(sub, "commit", "-q", "-m", "different nested declaration")
    child_head = _git(sub, "rev-parse", "HEAD")
    import shutil

    shutil.rmtree(sub)
    _git(rig.top, "checkout", "-q", "main")

    inner = submodules.resolve_host_subrepo(rig.top, "sub/inner", at="dev")

    assert child_head != pinned
    assert inner is not None
    assert inner.gitdir == rig.top / ".git" / "modules" / "sub" / "modules" / "inner-custom"


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


def test_locate_raises_when_stale_worktree_repair_is_locked(rig: Rig) -> None:
    import shutil

    _git(rig.top, "checkout", "-q", "main")
    shutil.rmtree(rig.top / "sub")
    gitdir = rig.top / ".git" / "modules" / "sub"
    (gitdir / "config.lock").write_text("locked")
    with pytest.raises(submodules.SubmoduleError, match="core.worktree"):
        submodules.resolve_host_subrepo(rig.top, "sub", at="dev")


def test_nested_default_name_lookup_without_declaration_commit(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")

    inner = submodules.resolve_host_subrepo(rig.top, "sub/inner")

    assert inner is not None
    assert inner.gitdir == rig.top / ".git" / "modules" / "sub" / "modules" / "inner-custom"


def test_host_submodules_at_reads_the_commit_not_the_checkout(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")

    subs = submodules.host_submodules_at(rig.top, "dev")

    assert [s.path for s in subs] == ["sub", "sub/inner"]
    assert subs[0].gitlink == _git(rig.top, "rev-parse", "dev:sub")
    assert subs[0].url == str(rig.subup)
    assert subs[0].repo is not None and subs[0].repo.worktree is None
    assert submodules.host_submodules_at(rig.top, "main") == []


def test_place_branches_places_a_gitdir_only_submodule(rig: Rig) -> None:
    gitdir = rig.top / ".git" / "modules" / "sub"
    old = _git(rig.top / "sub", "rev-parse", "HEAD")
    (rig.subup / "d" / "f").write_text("2\n")
    _git(rig.subup, "commit", "-qam", "s2")
    _git(rig.top / "sub", "fetch", "-q", "origin")
    new = _git(rig.top / "sub", "rev-parse", "origin/main")
    _git(rig.top / "sub", "checkout", "-q", "-B", "dev", new)
    _git(rig.top, "add", "sub")
    _git(rig.top, "commit", "-q", "-m", "bump sub")
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    _git(gitdir, "update-ref", "refs/heads/dev", old)

    out = submodules.place_branches_from_commit(rig.top, "dev", "dev")

    by_path = {placement.path: placement for placement in out}
    assert by_path["sub"].status == "fast-forwarded"
    assert _git(gitdir, "rev-parse", "refs/heads/dev") == new
    assert subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=gitdir, check=False
    ).returncode != 0


def test_host_subrepo_exists_is_true_for_a_gitdir_only_submodule(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    assert submodules.host_subrepo_exists(rig.top, "sub")


def test_host_subrepo_root_prefers_the_located_root_and_falls_back_to_the_path(rig: Rig) -> None:
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    assert submodules.host_subrepo_root(rig.top, "sub") == rig.top / ".git" / "modules" / "sub"
    assert submodules.host_subrepo_root(rig.top, "nope") == rig.top / "nope"


def _host_run(cwd: str, args: list[str]) -> tuple[bool, str]:
    from jailbee import git

    return git.run_capture(cwd, args)


def test_vanishing_lists_submodules_absent_from_the_target(rig: Rig) -> None:
    top = str(rig.top)
    assert submodules.vanishing_submodules(_host_run, top, head="dev", target="main") == ["sub"]
    assert submodules.vanishing_submodules(_host_run, top, head="main", target="dev") == []


def test_vanishing_for_a_merge_counts_only_deletions_by_the_other_side(rig: Rig) -> None:
    top = str(rig.top)
    # base == head: the other side (main) never had sub, so nothing is deleted by it.
    assert (
        submodules.vanishing_submodules(_host_run, top, head="dev", target="main", base="main")
        == []
    )


def test_switch_dev_main_dev_leaves_a_clean_tree_and_keeps_ignored_files(rig: Rig) -> None:
    (rig.top / "sub" / ".env").write_text("SECRET\n")
    gitdir = rig.top / ".git" / "modules" / "sub"
    _git(rig.top / "sub", "remote", "add", "myfork", "https://example.invalid/f.git")
    _git(rig.top / "sub", "branch", "feature-x")
    top = str(rig.top)

    paths = submodules.vanishing_submodules(_host_run, top, head="dev", target="main")
    submodules.assert_vanishing_clean(_host_run, top, paths, target_label="main")
    _git(rig.top, "checkout", "-q", "main")
    kept = submodules.prune_vanished_worktrees(
        _host_run, top, paths, fs=submodules.HostFs(), target_label="main"
    )

    assert kept == {"sub": [str(rig.top / "sub" / ".env")]}
    assert sorted(p.name for p in (rig.top / "sub").iterdir()) == [".env"]
    assert "myfork" in _git(gitdir, "remote")
    assert "feature-x" in _git(gitdir, "branch")

    _git(rig.top, "checkout", "-q", "dev")
    _git(rig.top, "submodule", "update", "-q", "--init", "--checkout", "--recursive")
    assert _git(rig.top, "status", "--porcelain") == ""
    assert _git(rig.top / "sub", "status", "--porcelain") == ""
    assert _git(rig.top / "sub" / "inner", "status", "--porcelain") == ""
    assert (rig.top / "sub" / ".env").read_text() == "SECRET\n"
    assert (rig.top / "sub" / "inner" / "i").exists()


def test_clean_switch_removes_the_directory_entirely(rig: Rig) -> None:
    top = str(rig.top)
    paths = submodules.vanishing_submodules(_host_run, top, head="dev", target="main")
    _git(rig.top, "checkout", "-q", "main")
    submodules.prune_vanished_worktrees(
        _host_run, top, paths, fs=submodules.HostFs(), target_label="main"
    )
    assert not (rig.top / "sub").exists()
    assert _git(rig.top, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "dirty",
    [
        lambda s: (s / "d" / "f").write_text("changed\n"),
        lambda s: (s / "untracked.txt").write_text("x\n"),
        lambda s: (s / "inner" / "i").write_text("changed\n"),
    ],
    ids=["tracked", "untracked", "nested"],
)
def test_dirty_vanishing_submodule_is_refused(rig: Rig, dirty) -> None:
    dirty(rig.top / "sub")
    top = str(rig.top)
    paths = submodules.vanishing_submodules(_host_run, top, head="dev", target="main")
    with pytest.raises(submodules.SubmoduleError, match=r"'sub'"):
        submodules.assert_vanishing_clean(_host_run, top, paths, target_label="main")


def test_legacy_gitdir_is_absorbed_before_the_switch(rig: Rig) -> None:
    import shutil

    sub = rig.top / "sub"
    shutil.rmtree(sub)
    shutil.rmtree(rig.top / ".git" / "modules" / "sub")
    _git(rig.top, "clone", "-q", str(rig.subup), "sub")
    _git(sub, "checkout", "-q", _git(rig.top, "rev-parse", "dev:sub"))
    _git(sub, "branch", "keep-me")
    top = str(rig.top)

    paths = submodules.vanishing_submodules(_host_run, top, head="dev", target="main")
    submodules.assert_vanishing_clean(_host_run, top, paths, target_label="main")

    assert (sub / ".git").is_file()
    assert "keep-me" in _git(rig.top / ".git" / "modules" / "sub", "branch")


@pytest.mark.parametrize("legacy", ["sub", "sub/inner"])
def test_prune_preserves_legacy_git_directory(rig: Rig, legacy: str) -> None:
    import shutil

    sub = rig.top / legacy
    gitdir = Path(_git(sub, "rev-parse", "--absolute-git-dir"))
    _git(sub, "config", "--unset", "core.worktree")
    (sub / ".git").unlink()
    shutil.move(str(gitdir), str(sub / ".git"))
    if legacy == "sub":
        (sub / "inner" / ".git").write_text("gitdir: ../.git/modules/inner-custom\n")
    _git(rig.top, "checkout", "-q", "main")
    with pytest.raises(submodules.SubmoduleError, match="git directory"):
        submodules.prune_vanished_worktrees(
            _host_run, str(rig.top), ["sub"], fs=submodules.HostFs(), target_label="main"
        )
    assert (sub / ".git" / "HEAD").exists()
    assert (rig.top / "sub" / "d" / "f").exists()


def test_precheck_absorbs_nested_legacy_git_directory(rig: Rig) -> None:
    import shutil

    inner = rig.top / "sub" / "inner"
    gitdir = Path(_git(inner, "rev-parse", "--absolute-git-dir"))
    _git(inner, "config", "--unset", "core.worktree")
    (inner / ".git").unlink()
    shutil.move(str(gitdir), str(inner / ".git"))
    _git(inner, "branch", "keep-nested")
    submodules.assert_vanishing_clean(_host_run, str(rig.top), ["sub"], target_label="main")
    assert (inner / ".git").is_file()
    assert "keep-nested" in _git(inner, "branch")


@pytest.mark.parametrize("operation", ["ls-tree", "status", "read-tree", "config"])
def test_prune_errors_stop_parent_cleanup(rig: Rig, operation: str) -> None:
    _git(rig.top, "checkout", "-q", "main")
    inner = str(rig.top / "sub" / "inner")

    def run(cwd: str, args: list[str]) -> tuple[bool, str]:
        if cwd == inner and args[0] == operation:
            return False, "permission denied"
        return _host_run(cwd, args)

    with pytest.raises(submodules.SubmoduleError):
        submodules.prune_vanished_worktrees(
            run, str(rig.top), ["sub"], fs=submodules.HostFs(), target_label="main"
        )
    assert (rig.top / "sub" / "d" / "f").exists()
    assert (rig.top / "sub" / ".git").exists()


def test_unreadable_target_tree_is_not_empty(rig: Rig) -> None:
    with pytest.raises(submodules.SubmoduleError):
        submodules.vanishing_submodules(_host_run, str(rig.top), head="dev", target="invalid")


def test_precheck_refuses_unreadable_status(rig: Rig) -> None:
    def run(cwd: str, args: list[str]) -> tuple[bool, str]:
        if args[0] == "status":
            return False, "permission denied"
        return _host_run(cwd, args)

    with pytest.raises(submodules.SubmoduleError, match="status"):
        submodules.assert_vanishing_clean(run, str(rig.top), ["sub"], target_label="main")


@pytest.mark.parametrize("foreign", [False, True])
def test_prune_missing_or_foreign_worktree_warns_and_preserves(rig: Rig, foreign: bool, mocker) -> None:
    sub = rig.top / "sub"
    if foreign:
        (sub / ".git").unlink()
    else:
        import shutil

        shutil.rmtree(sub)
    _git(rig.top, "checkout", "-q", "main")
    warn = mocker.patch("jailbee.submodules._warn")
    submodules.prune_vanished_worktrees(
        _host_run, str(rig.top), ["sub"], fs=submodules.HostFs(), target_label="main"
    )
    warn.assert_called_once()
    if foreign:
        assert (sub / "d" / "f").exists()


def test_host_fs_does_not_follow_directory_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "empty").mkdir()
    (outside / "secret").write_text("keep")
    (root / "link").symlink_to(outside, target_is_directory=True)
    fs = submodules.HostFs()
    fs.remove_empty_dirs(str(root))
    assert fs.list_files(str(root)) == [str(root / "link")]
    assert (outside / "empty").exists()
    fs.remove_empty_dirs(str(root / "link"))
    assert fs.list_files(str(root / "link")) == [str(root / "link")]
    assert (outside / "empty").exists()


def test_switch_keeps_ignored_symlink(rig: Rig) -> None:
    link = rig.top / "sub" / ".env"
    link.symlink_to(rig.inup, target_is_directory=True)
    submodules.assert_vanishing_clean(_host_run, str(rig.top), ["sub"], target_label="main")
    _git(rig.top, "checkout", "-q", "main")
    kept = submodules.prune_vanished_worktrees(
        _host_run, str(rig.top), ["sub"], fs=submodules.HostFs(), target_label="main"
    )
    assert kept == {"sub": [str(link)]}
    assert link.is_symlink()
    assert (rig.inup / "i").exists()


def test_prune_skips_conflicted_superproject(rig: Rig, mocker) -> None:
    _git(rig.top, "checkout", "-q", "main")

    def run(cwd: str, args: list[str]) -> tuple[bool, str]:
        if cwd == str(rig.top) and args == ["ls-files", "-u"]:
            return True, "160000 abc 1\tsub\n"
        return _host_run(cwd, args)

    warn = mocker.patch("jailbee.submodules._warn")
    assert submodules.prune_vanished_worktrees(
        run, str(rig.top), ["sub"], fs=submodules.HostFs(), target_label="main"
    ) == {}
    assert (rig.top / "sub" / "d" / "f").exists()
    warn.assert_called_once()


def test_prune_filesystem_failure_stops_parent_cleanup(rig: Rig, mocker) -> None:
    _git(rig.top, "checkout", "-q", "main")
    fs = submodules.HostFs()
    mocker.patch.object(fs, "remove_file", side_effect=PermissionError("denied"))
    with pytest.raises(PermissionError):
        submodules.prune_vanished_worktrees(
            _host_run, str(rig.top), ["sub"], fs=fs, target_label="main"
        )
    assert (rig.top / "sub" / "d" / "f").exists()
    assert (rig.top / "sub" / ".git").exists()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("precheck", [False, True])
def test_foreign_linked_worktree_is_preserved(rig: Rig, nested: bool, precheck: bool, mocker) -> None:
    import shutil

    candidate = rig.top / "sub" / "inner" if nested else rig.top / "sub"
    shutil.rmtree(candidate)
    foreign = rig.top.parent / "foreign"
    _git(rig.top.parent, "init", "-q", "-b", "main", str(foreign))
    (foreign / "valuable").write_text("keep\n")
    _git(foreign, "add", ".")
    _git(foreign, "commit", "-q", "-m", "foreign")
    _git(foreign, "worktree", "add", "-q", "-b", "linked", str(candidate))
    index = _git(candidate, "ls-files", "--stage")
    dot_git = (candidate / ".git").read_bytes()
    warn = mocker.patch("jailbee.submodules._warn")
    if precheck:
        # Nested foreign worktrees can make the parent look dirty; refusal is safe.
        try:
            submodules.assert_vanishing_clean(_host_run, str(rig.top), ["sub"], target_label="main")
        except submodules.SubmoduleError:
            pass
    else:
        _git(rig.top, "checkout", "-q", "main")
        submodules.prune_vanished_worktrees(
            _host_run, str(rig.top), ["sub"], fs=submodules.HostFs(), target_label="main"
        )
    assert (candidate / "valuable").read_text() == "keep\n"
    assert _git(candidate, "ls-files", "--stage") == index
    assert (candidate / ".git").read_bytes() == dot_git
    assert warn.called
    if nested:
        assert (rig.top / "sub" / "d" / "f").exists()


def test_custom_named_submodule_prunes(rig: Rig) -> None:
    _git(rig.top, "submodule", "add", "-q", "--name", "lib-core", str(rig.inup), "libs/core")
    _git(rig.top, "commit", "-q", "-m", "custom")
    paths = submodules.vanishing_submodules(_host_run, str(rig.top), head="dev", target="main")
    submodules.assert_vanishing_clean(_host_run, str(rig.top), paths, target_label="main")
    _git(rig.top, "checkout", "-q", "main")
    submodules.prune_vanished_worktrees(
        _host_run, str(rig.top), paths, fs=submodules.HostFs(), target_label="main"
    )
    assert not (rig.top / "libs/core").exists()
    assert (rig.top / ".git/modules/lib-core/HEAD").exists()


def _cfg(rig: Rig):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.repo_root = rig.top
    return cfg


def test_jb_branch_round_trip_leaves_no_debris(rig: Rig) -> None:
    from jailbee import sync

    sync.checkout_submodules_on_host(_cfg(rig), branch="main", switch_superproject=True)
    assert not (rig.top / "sub").exists()
    assert _git(rig.top, "status", "--porcelain") == ""
    sync.checkout_submodules_on_host(_cfg(rig), branch="dev", switch_superproject=True)
    assert (rig.top / "sub" / "inner" / "i").exists()
    assert _git(rig.top, "status", "--porcelain") == ""


def test_jb_branch_refuses_dirty_submodule(rig: Rig) -> None:
    from jailbee import sync

    (rig.top / "sub" / "untracked.txt").write_text("x\n")
    with pytest.raises(sync.SyncError, match="local changes"):
        sync.checkout_submodules_on_host(_cfg(rig), branch="main", switch_superproject=True)
    assert _git(rig.top, "branch", "--show-current") == "dev"
    assert (rig.top / "sub" / "untracked.txt").read_text() == "x\n"


def test_a_failed_operation_prunes_nothing(rig: Rig) -> None:
    from jailbee import sync

    def op() -> None:
        raise sync.SyncError("conflict")

    with pytest.raises(sync.SyncError, match="conflict"):
        sync._move_host_head(_cfg(rig), target="main", op=op, label="main")
    assert (rig.top / "sub" / "d" / "f").exists()
    assert (rig.top / "sub" / ".git").is_file()


@pytest.mark.parametrize("conflict", [False, True])
def test_move_host_head_real_merge_deletion_and_conflict(rig: Rig, conflict: bool) -> None:
    from jailbee import git, sync

    (rig.top / "shared").write_text("base\n")
    _git(rig.top, "add", "shared")
    _git(rig.top, "commit", "-qm", "base")
    base = _git(rig.top, "rev-parse", "HEAD")
    _git(rig.top, "checkout", "-qb", "incoming")
    _git(rig.top, "update-index", "--force-remove", "sub")
    _git(rig.top, "rm", ".gitmodules")
    (rig.top / "shared").write_text("incoming\n")
    _git(rig.top, "add", "shared")
    _git(rig.top, "commit", "-qm", "remove sub")
    _git(rig.top, "checkout", "-q", "dev")
    if conflict:
        (rig.top / "shared").write_text("local\n")
        _git(rig.top, "add", "shared")
        _git(rig.top, "commit", "-qm", "local edit")

    def op() -> None:
        try:
            git.merge_ref(rig.top, "incoming", message="merge", no_ff=True, ff_only=False)
        except git.GitError:
            sync._resolve_gitlinks_and_commit_host(_cfg(rig), branch="dev", location=str(rig.top))

    if conflict:
        with pytest.raises(sync.SyncError):
            sync._move_host_head(_cfg(rig), target="incoming", base=base, label="dev", op=op)
        assert _git(rig.top, "ls-files", "-u")
        assert (rig.top / ".git" / "MERGE_HEAD").exists()
        assert (rig.top / "sub" / "inner" / "i").exists()
        assert (rig.top / "sub" / ".git").is_file()
    else:
        sync._move_host_head(_cfg(rig), target="incoming", base=base, label="dev", op=op)
        assert not (rig.top / "sub").exists()
        assert _git(rig.top, "status", "--porcelain") == ""
        assert len(_git(rig.top, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3


class LocalIncus:
    """Run container commands in a temporary local clone."""

    def exec(self, _container, cmd, *, uid=None, gid=None, env=None):
        from jailbee.incus import IncusError

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode:
            raise IncusError(result.stderr)
        return result.stdout

    def config_get(self, _container, _key):
        return None


@pytest.fixture
def local_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(submodules, "_sub_upload_pack_url", lambda cfg, c, repo_dir, sub: f"{repo_dir}/{sub}")


def _transport_cfg(rig: Rig):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.repo_root = rig.top
    cfg.container_user.uid = None
    cfg.container_user.gid = None
    return cfg


@pytest.mark.parametrize("name", ["extra", "extra-custom"])
def test_pull_container_born_submodule_preserves_host_tree(rig, tmp_path, local_urls, name):
    ctr = tmp_path / "ctr"
    _git(tmp_path, "clone", "-q", "-b", "dev", "--recurse-submodules", str(rig.top), str(ctr))
    _git(ctr, "submodule", "add", "-q", "--name", name, str(rig.inup), "libs/extra")
    _git(ctr, "commit", "-q", "-m", "add extra")
    # An uncommitted rename must not change the transport gitdir name.
    _git(ctr, "config", "-f", ".gitmodules", "--rename-section", f"submodule.{name}", "submodule.wrong")
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    submodules.transport_submodules_to_host(_transport_cfg(rig), LocalIncus(), "ctr", "c1", repo_dir=str(ctr))
    assert _git(rig.top, "status", "--porcelain") == ""
    assert not (rig.top / "libs").exists()
    gitdir = rig.top / ".git/modules" / name
    assert _git(gitdir, "rev-parse", "refs/jailbee-sub/c1/libs/extra/HEAD") == _git(ctr / "libs/extra", "rev-parse", "HEAD")
    assert _git(gitdir, "remote", "get-url", "origin") == str(rig.inup)
    _git(rig.top, "fetch", "-q", str(ctr), "dev:dev", "--update-head-ok")
    from jailbee import sync

    sync.checkout_submodules_on_host(_cfg(rig), branch="dev", switch_superproject=True)
    assert (rig.top / "libs/extra/i").exists()
    assert _git(rig.top, "status", "--porcelain") == ""


def test_pull_fetches_existing_gitdir_without_rewriting_origin(rig, tmp_path, local_urls):
    ctr = tmp_path / "ctr"
    _git(tmp_path, "clone", "-q", "-b", "dev", "--recurse-submodules", str(rig.top), str(ctr))
    (ctr / "sub/d/f").write_text("ctr\n")
    _git(ctr / "sub", "commit", "-qam", "ctr work")
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    gitdir = rig.top / ".git/modules/sub"
    _git(gitdir, "remote", "set-url", "origin", "preserved")
    submodules.transport_submodules_to_host(_transport_cfg(rig), LocalIncus(), "ctr", "c1", repo_dir=str(ctr))
    assert _git(gitdir, "rev-parse", "refs/jailbee-sub/c1/sub/HEAD") == _git(ctr / "sub", "rev-parse", "HEAD")
    assert _git(gitdir, "remote", "get-url", "origin") == "preserved"
    assert not (rig.top / "sub").exists()
    assert not (gitdir / "modules/sub").exists()


def test_pull_only_nested_container_born_leaf_fetches_parents_first(rig, tmp_path, local_urls):
    ctr = tmp_path / "ctr"
    _git(tmp_path, "clone", "-q", "-b", "dev", "--recurse-submodules", str(rig.top), str(ctr))
    _git(ctr, "submodule", "add", "-q", "--name", "parent-custom", str(rig.subup), "new-parent")
    _git(ctr, "submodule", "update", "-q", "--init", "--recursive")
    _git(ctr, "submodule", "add", "-q", str(rig.inup), "unrelated")
    _git(ctr, "commit", "-q", "-m", "new modules")
    _git(rig.top, "checkout", "-q", "--recurse-submodules", "main")
    submodules.transport_submodules_to_host(_transport_cfg(rig), LocalIncus(), "ctr", "c1", repo_dir=str(ctr), only="new-parent/inner")
    parent = rig.top / ".git/modules/parent-custom"
    child = parent / "modules/inner-custom"
    assert _git(parent, "rev-parse", "HEAD") == _git(ctr / "new-parent", "rev-parse", "HEAD")
    assert _git(child, "rev-parse", "refs/jailbee-sub/c1/new-parent/inner/HEAD") == _git(ctr / "new-parent/inner", "rev-parse", "HEAD")
    assert not (rig.top / ".git/modules/unrelated").exists()
    assert not (rig.top / "new-parent").exists()
    assert _git(rig.top, "status", "--porcelain") == ""
