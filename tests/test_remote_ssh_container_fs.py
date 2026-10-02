"""ContainerFS runs the real scripts under `sh` against a tmp_path repo."""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path

import pytest

from jailbee.incus import ExecResult, IncusError
from jailbee.remote_ssh.container_fs import ContainerFS, FSError
from tests.remote_ssh_fakes import LocalIncus


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("hello world")
    (repo / "sub").mkdir()
    (repo / "sub" / "inner.txt").write_text("inner")
    (tmp_path / "secret").write_text("SECRET")
    os.symlink("../secret", repo / "esc")
    os.symlink("/etc/passwd", repo / "abs")
    os.symlink("a.txt", repo / "ok")
    return repo


@pytest.fixture
def fs(repo: Path) -> ContainerFS:
    return ContainerFS(LocalIncus(), "app-feat", str(repo), 1000, 1000)


def kind_of(call) -> str:
    with pytest.raises(FSError) as exc:
        call()
    return exc.value.kind


def test_stat_reports_size_and_regular_file_mode(fs):
    st = fs.stat("a.txt", follow=True)
    assert st.size == 11
    assert stat_mod.S_ISREG(st.mode)
    assert stat_mod.S_ISDIR(fs.stat("sub", follow=True).mode)
    assert stat_mod.S_ISDIR(fs.stat("", follow=True).mode)


def test_stat_follows_a_link_inside_the_repo(fs):
    assert fs.stat("ok", follow=True).size == 11
    assert stat_mod.S_ISLNK(fs.stat("ok", follow=False).mode)


@pytest.mark.parametrize("name", ["esc", "abs"])
def test_links_that_leave_the_repo_are_refused_but_lstat_still_sees_them(fs, name):
    assert kind_of(lambda: fs.stat(name, follow=True)) == "denied"
    assert stat_mod.S_ISLNK(fs.stat(name, follow=False).mode)
    assert kind_of(lambda: fs.read(name, 0, 10)) == "denied"
    assert kind_of(lambda: fs.readlink(name)) == "denied"


def test_opening_an_escaping_link_for_write_never_touches_the_target(fs, repo):
    assert kind_of(lambda: fs.open("esc", create=True, truncate=True, exclusive=False)) == "denied"
    assert kind_of(lambda: fs.write("esc", 0, b"PWNED")) == "denied"
    assert (repo.parent / "secret").read_text() == "SECRET"


def test_removing_an_escaping_link_removes_only_the_link(fs, repo):
    fs.remove("esc")
    assert not (repo / "esc").is_symlink()
    assert (repo.parent / "secret").exists()


def test_a_missing_file_is_not_found(fs):
    assert kind_of(lambda: fs.stat("nope", follow=True)) == "not_found"
    assert kind_of(lambda: fs.read("nope", 0, 1)) == "not_found"


@pytest.mark.parametrize("rel", ["../x", "a/../../x", "/abs", "a//b", "./a", "a\0b"])
def test_malformed_relative_paths_are_denied_before_any_exec(fs, rel):
    assert kind_of(lambda: fs.stat(rel, follow=True)) == "denied"
    assert fs._incus.calls == []


def test_listdir_returns_entries_with_attributes(fs):
    entries = {e.name: e.stat for e in fs.listdir("")}
    assert set(entries) == {"a.txt", "sub", "esc", "abs", "ok"}
    assert stat_mod.S_ISDIR(entries["sub"].mode)
    assert stat_mod.S_ISLNK(entries["esc"].mode)
    assert entries["a.txt"].size == 11


def test_listdir_of_a_file_is_not_a_directory(fs):
    assert kind_of(lambda: fs.listdir("a.txt")) == "not_dir"


def test_read_honours_offset_and_size(fs):
    assert fs.read("a.txt", 6, 3) == b"wor"
    assert fs.read("a.txt", 100, 10) == b""


def test_binary_content_round_trips_through_offset_writes(fs, repo):
    data = bytes(range(256)) * 3 + b"\r\n\x00"
    fs.open("blob.bin", create=True, truncate=True, exclusive=False)
    fs.write("blob.bin", 0, data[:500])
    fs.write("blob.bin", 500, data[500:])
    assert (repo / "blob.bin").read_bytes() == data
    assert fs.read("blob.bin", 0, len(data)) == data


def test_write_does_not_create_a_missing_file(fs, repo):
    assert kind_of(lambda: fs.write("ghost", 0, b"x")) == "not_found"
    assert not (repo / "ghost").exists()


def test_exclusive_create_fails_on_an_existing_file(fs):
    assert (
        kind_of(lambda: fs.open("a.txt", create=True, truncate=False, exclusive=True)) == "exists"
    )


def test_open_without_create_requires_the_file(fs):
    assert (
        kind_of(lambda: fs.open("ghost", create=False, truncate=False, exclusive=False))
        == "not_found"
    )


def test_names_with_shell_metacharacters_are_literal(fs, repo):
    name = "a b'; touch pwned; '$(id)\nx"
    fs.open(name, create=True, truncate=False, exclusive=False)
    assert (repo / name).exists()
    assert not (repo / "pwned").exists()
    dash = "-rf"
    fs.open(dash, create=True, truncate=False, exclusive=False)
    assert (repo / dash).exists()


def test_mkdir_and_rmdir(fs, repo):
    fs.mkdir("newdir", 0o750)
    assert (repo / "newdir").is_dir()
    assert stat_mod.S_IMODE((repo / "newdir").stat().st_mode) == 0o750
    assert kind_of(lambda: fs.mkdir("newdir", 0o755)) == "exists"
    fs.rmdir("newdir")
    assert not (repo / "newdir").exists()
    assert kind_of(lambda: fs.rmdir("a.txt")) == "not_dir"


def test_remove_refuses_directories_and_the_root(fs):
    assert kind_of(lambda: fs.remove("sub")) == "is_dir"
    assert kind_of(lambda: fs.remove("")) == "denied"
    assert kind_of(lambda: fs.rmdir("")) == "denied"
    assert kind_of(lambda: fs.mkdir("", 0o755)) == "denied"


def test_rename_refuses_an_existing_target_unless_overwrite(fs, repo):
    (repo / "b.txt").write_text("B")
    assert kind_of(lambda: fs.rename("a.txt", "b.txt", overwrite=False)) == "exists"
    fs.rename("a.txt", "b.txt", overwrite=True)
    assert (repo / "b.txt").read_text() == "hello world"
    assert not (repo / "a.txt").exists()


def test_rename_cannot_leave_the_repo_or_move_the_root(fs, repo):
    assert kind_of(lambda: fs.rename("a.txt", "../out.txt", overwrite=True)) == "denied"
    assert kind_of(lambda: fs.rename("", "x", overwrite=True)) == "denied"
    assert not (repo.parent / "out.txt").exists()


def test_rename_into_a_missing_directory_is_not_found(fs):
    assert kind_of(lambda: fs.rename("a.txt", "nodir/a.txt", overwrite=False)) == "not_found"


def test_readlink_returns_a_link_that_stays_inside(fs):
    assert fs.readlink("ok") == "a.txt"
    assert kind_of(lambda: fs.readlink("a.txt")) == "failure"


def test_setstat_changes_mode_mtime_and_size(fs, repo):
    fs.setstat("a.txt", mode=0o600, mtime=1_000_000_000, size=5)
    st = (repo / "a.txt").stat()
    assert stat_mod.S_IMODE(st.st_mode) == 0o600
    assert int(st.st_mtime) == 1_000_000_000
    assert st.st_size == 5
    assert kind_of(lambda: fs.setstat("esc", mode=0o777)) == "denied"
    assert kind_of(lambda: fs.setstat("", mode=0o777)) == "denied"


def test_setstat_through_a_link_to_the_repo_root_is_refused(fs, repo):
    os.symlink(".", repo / "self")
    before = stat_mod.S_IMODE(repo.stat().st_mode)
    assert kind_of(lambda: fs.setstat("self", mode=0o777)) == "denied"
    assert stat_mod.S_IMODE(repo.stat().st_mode) == before


def test_a_non_running_container_is_a_failure_not_a_crash(repo):
    class Down(LocalIncus):
        def exec_bytes(self, *a, **k):
            raise IncusError("instance is not running")

    fs = ContainerFS(Down(), "app-feat", str(repo), 1000, 1000)
    assert kind_of(lambda: fs.stat("a.txt", follow=True)) == "failure"


def test_helper_stderr_is_trimmed_to_one_short_line(repo):
    class Noisy(LocalIncus):
        def exec_bytes(self, *a, **k):
            return ExecResult(1, b"", b"boom\n" + b"x" * 500)

    fs = ContainerFS(Noisy(), "app-feat", str(repo), 1000, 1000)
    with pytest.raises(FSError) as exc:
        fs.stat("a.txt", follow=True)
    assert str(exc.value) == "boom"


def test_scripts_are_constants_and_receive_values_only_as_arguments(fs, repo):
    (repo / "marker-xyz").write_text("")
    fs.stat("marker-xyz", follow=True)
    _, cmd, uid, gid = fs._incus.calls[-1]
    assert cmd[:2] == ["sh", "-c"] and cmd[3] == "sh"
    assert "marker-xyz" not in cmd[2]  # the script text never contains client input
    assert cmd[-1] == "marker-xyz"  # ...it arrives as an argument
    assert (uid, gid) == (1000, 1000)


@pytest.fixture
def repo_with_linkdir(tmp_path: Path) -> Path:
    """Repo with a symlinked parent directory pointing outside."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("hello world")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("SECRET")
    os.symlink("../outside", repo / "linkdir")
    return repo


@pytest.fixture
def fs_with_linkdir(repo_with_linkdir: Path) -> ContainerFS:
    return ContainerFS(LocalIncus(), "app-feat", str(repo_with_linkdir), 1000, 1000)


def test_open_through_symlinked_parent_is_denied(fs_with_linkdir, repo_with_linkdir):
    outside = repo_with_linkdir.parent / "outside"
    assert (
        kind_of(
            lambda: fs_with_linkdir.open(
                "linkdir/new", create=True, truncate=False, exclusive=False
            )
        )
        == "denied"
    )
    assert not (outside / "new").exists()


def test_mkdir_through_symlinked_parent_is_denied(fs_with_linkdir, repo_with_linkdir):
    outside = repo_with_linkdir.parent / "outside"
    assert kind_of(lambda: fs_with_linkdir.mkdir("linkdir/d", 0o755)) == "denied"
    assert not (outside / "d").exists()


def test_remove_through_symlinked_parent_is_denied(fs_with_linkdir, repo_with_linkdir):
    outside = repo_with_linkdir.parent / "outside"
    assert kind_of(lambda: fs_with_linkdir.remove("linkdir/secret")) == "denied"
    assert (outside / "secret").read_text() == "SECRET"


def test_stat_of_file_through_symlinked_parent_is_denied_even_lstat(
    fs_with_linkdir, repo_with_linkdir
):
    assert kind_of(lambda: fs_with_linkdir.stat("linkdir/secret", follow=False)) == "denied"


def test_listdir_of_symlinked_parent_is_denied(fs_with_linkdir):
    assert kind_of(lambda: fs_with_linkdir.listdir("linkdir")) == "denied"


def test_rename_to_symlinked_parent_is_denied(fs_with_linkdir, repo_with_linkdir):
    outside = repo_with_linkdir.parent / "outside"
    assert kind_of(lambda: fs_with_linkdir.rename("a.txt", "linkdir/x", overwrite=True)) == "denied"
    assert (repo_with_linkdir / "a.txt").exists()
    assert not (outside / "x").exists()


def test_rename_from_symlinked_parent_is_denied(fs_with_linkdir, repo_with_linkdir):
    outside = repo_with_linkdir.parent / "outside"
    assert (
        kind_of(lambda: fs_with_linkdir.rename("linkdir/secret", "x", overwrite=True)) == "denied"
    )
    assert (outside / "secret").read_text() == "SECRET"
    assert not (repo_with_linkdir / "x").exists()
