"""JailbeeSFTPServer over a LocalIncus-backed tmp_path repo."""

from __future__ import annotations

import asyncio
import logging
import os
import stat as stat_mod
from pathlib import Path
from unittest.mock import Mock

import asyncssh
import pytest
from asyncssh import (
    FXF_APPEND,
    FXF_CREAT,
    FXF_EXCL,
    FXF_READ,
    FXF_TRUNC,
    FXF_WRITE,
    SFTPAttrs,
    SFTPServer,
)
from asyncssh.constants import (
    FILEXFER_TYPE_DIRECTORY,
    FILEXFER_TYPE_REGULAR,
    FILEXFER_TYPE_SYMLINK,
)
from asyncssh.sftp import SFTPServerFS

from jailbee.remote_ssh import sftp
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.remote_ssh.sftp import JailbeeSFTPServer, SFTPService, split_path
from tests.remote_ssh_fakes import LocalIncus, raw_container

FINGERPRINT = "SHA256:qLBHzrI/tje39Belv8gH7aaz1iprjQMjKh4sbnQnFT4"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("hello world")
    (repo / "sub").mkdir()
    (tmp_path / "secret").write_text("SECRET")
    os.symlink("../secret", repo / "esc")
    os.symlink("a.txt", repo / "ok")
    return repo


def make_server(incus: LocalIncus, excluded: tuple[str, ...] = ()) -> JailbeeSFTPServer:
    chan = Mock()
    chan.get_extra_info.side_effect = {
        "peername": ("192.0.2.10", 43123),
        "jailbee_key_fingerprint": FINGERPRINT,
    }.get
    scope = RemoteRepoScope(frozenset(excluded))
    service = SFTPService(incus, lambda: scope, asyncio.Semaphore(8))
    return JailbeeSFTPServer(chan, service)


@pytest.fixture
def server(repo: Path) -> JailbeeSFTPServer:
    return make_server(LocalIncus([raw_container("app-feat", repo_dir=str(repo))]))


async def names(srv: JailbeeSFTPServer, path: bytes) -> list[str]:
    return [n.filename.decode() async for n in srv.scandir(path) if n.filename not in (b".", b"..")]


# ---- path handling ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "parts"),
    [
        (b"/", ()),
        (b"", ()),
        (b".", ()),
        (b"/a/b", ("a", "b")),
        (b"//a///b/", ("a", "b")),
        (b"/a/../b", ("b",)),
        (b"/../../a", ("a",)),
        (b"a/./b", ("a", "b")),
    ],
)
def test_split_path_normalises_and_clamps_at_the_root(raw, parts):
    assert split_path(raw) == parts


@pytest.mark.parametrize("raw", [b"/a\x00b", b"/\xff\xfe"])
def test_split_path_refuses_nul_and_non_utf8(raw):
    with pytest.raises(asyncssh.SFTPFailure):
        split_path(raw)


def test_realpath_is_lexical_and_never_leaves_the_tree(server):
    assert run(_awaited(server.realpath(b"/app-feat/../../x/./y"))) == b"/x/y"
    assert run(_awaited(server.realpath(b"."))) == b"/"


async def _awaited(value):
    return await value if asyncio.iscoroutine(value) or hasattr(value, "__await__") else value


# ---- visibility ------------------------------------------------------------


def test_root_lists_only_running_in_scope_managed_containers(repo):
    incus = LocalIncus(
        [
            raw_container("app-feat", repo_dir=str(repo)),
            raw_container("app-stopped", repo_dir=str(repo), status="Stopped"),
            raw_container("priv-x", repo="priv", repo_dir=str(repo)),
            raw_container("app-nolabel", repo_dir=None),
            {"name": "unmanaged", "status": "Running", "profiles": ["default"], "config": {}},
        ]
    )
    srv = make_server(incus, excluded=("priv",))
    assert run(names(srv, b"/")) == ["app-feat"]


@pytest.mark.parametrize("hidden", ["app-stopped", "priv-x", "nothing"])
def test_a_hidden_container_is_no_such_file(repo, hidden):
    incus = LocalIncus(
        [
            raw_container("app-stopped", repo_dir=str(repo), status="Stopped"),
            raw_container("priv-x", repo="priv", repo_dir=str(repo)),
        ]
    )
    srv = make_server(incus, excluded=("priv",))
    with pytest.raises(asyncssh.SFTPNoSuchFile):
        run(srv.stat(f"/{hidden}/a.txt".encode()))


def test_a_mount_mode_container_is_not_offered(repo):
    incus = LocalIncus(
        [
            raw_container("app-feat", repo_dir=str(repo)),
            raw_container("app-mnt", repo_dir=str(repo), mode="mount"),
            raw_container("app-clone", repo_dir=str(repo), mode="clone"),
        ]
    )
    srv = make_server(incus)
    assert run(names(srv, b"/")) == ["app-clone", "app-feat"]
    with pytest.raises(asyncssh.SFTPNoSuchFile):
        run(srv.stat(b"/app-mnt/a.txt"))


def test_a_session_without_a_trustworthy_scope_sees_nothing(repo):
    chan = Mock()
    chan.get_extra_info.return_value = None
    service = SFTPService(
        LocalIncus([raw_container("app-feat", repo_dir=str(repo))]),
        lambda: None,
        asyncio.Semaphore(8),
    )
    srv = JailbeeSFTPServer(chan, service)
    assert run(names(srv, b"/")) == []
    with pytest.raises(asyncssh.SFTPNoSuchFile):
        run(srv.stat(b"/app-feat/a.txt"))


def test_an_internal_error_log_cannot_be_forged_by_the_container_name(caplog):
    class Boom(LocalIncus):
        def list_containers(self, **kw):
            raise RuntimeError("boom")

    srv = make_server(Boom())
    with caplog.at_level(logging.ERROR), pytest.raises(asyncssh.SFTPFailure):
        run(srv.stat(b"/evil\nforged/a"))
    assert caplog.records
    assert all("\n" not in r.getMessage() for r in caplog.records)


def test_a_repo_directory_owned_by_root_is_refused(repo):
    class RootOwned(LocalIncus):
        def exec_bytes(self, name, cmd, **kw):
            if cmd[:3] == ["stat", "-c", "%u %g"]:
                from jailbee.incus import ExecResult

                return ExecResult(0, b"0 0\n", b"")
            return super().exec_bytes(name, cmd, **kw)

    srv = make_server(RootOwned([raw_container("app-feat", repo_dir=str(repo))]))
    with pytest.raises(asyncssh.SFTPPermissionDenied):
        run(srv.stat(b"/app-feat/a.txt"))


def test_the_virtual_root_is_read_only(server):
    for call in (
        lambda: server.mkdir(b"/new", SFTPAttrs()),
        lambda: server.remove(b"/app-feat"),
        lambda: server.rmdir(b"/app-feat"),
        lambda: server.rename(b"/app-feat", b"/other"),
        lambda: server.open(b"/file", FXF_WRITE | FXF_CREAT, SFTPAttrs()),
        lambda: server.setstat(b"/", SFTPAttrs(permissions=0o777)),
    ):
        with pytest.raises(asyncssh.SFTPPermissionDenied):
            run(call())


# ---- file operations -------------------------------------------------------


def test_stat_lstat_and_listing_inside_a_repo(server):
    st = run(server.stat(b"/app-feat/a.txt"))
    assert st.size == 11 and stat_mod.S_ISREG(st.permissions)
    assert stat_mod.S_ISLNK(run(server.lstat(b"/app-feat/ok")).permissions)
    assert set(run(names(server, b"/app-feat"))) == {"a.txt", "sub", "esc", "ok"}
    assert stat_mod.S_ISDIR(run(server.stat(b"/app-feat")).permissions)


def test_a_link_out_of_the_repo_is_refused_everywhere(server, repo):
    with pytest.raises(asyncssh.SFTPPermissionDenied):
        run(server.stat(b"/app-feat/esc"))
    with pytest.raises(asyncssh.SFTPPermissionDenied):
        run(server.open(b"/app-feat/esc", FXF_WRITE | FXF_TRUNC, SFTPAttrs()))
    with pytest.raises(asyncssh.SFTPPermissionDenied):
        run(server.readlink(b"/app-feat/esc"))
    assert (repo.parent / "secret").read_text() == "SECRET"
    assert run(server.readlink(b"/app-feat/ok")) == b"a.txt"


def test_dotdot_cannot_climb_out_of_the_repo_or_the_tree(server):
    with pytest.raises(asyncssh.SFTPNoSuchFile):
        run(server.stat(b"/app-feat/../secret"))  # clamps to /secret: no such container


def test_binary_upload_and_download_round_trip(server, repo):
    data = bytes(range(256)) * 3 + b"\r\n\x00"

    async def go():
        h = await server.open(b"/app-feat/blob.bin", FXF_WRITE | FXF_CREAT | FXF_TRUNC, SFTPAttrs())
        assert await server.write(h, 0, data[:500]) == 500
        assert await server.write(h, 500, data[500:]) == len(data) - 500
        server.close(h)
        h = await server.open(b"/app-feat/blob.bin", FXF_READ, SFTPAttrs())
        return await server.read(h, 0, 4096)

    assert run(go()) == data
    assert (repo / "blob.bin").read_bytes() == data


def test_reading_past_the_end_returns_empty_bytes(server):
    async def go():
        h = await server.open(b"/app-feat/a.txt", FXF_READ, SFTPAttrs())
        return await server.read(h, 500, 10)

    assert run(go()) == b""


def test_a_handle_opened_read_only_cannot_write(server, repo):
    async def go():
        h = await server.open(b"/app-feat/a.txt", FXF_READ, SFTPAttrs())
        await server.write(h, 0, b"X")

    with pytest.raises(asyncssh.SFTPPermissionDenied):
        run(go())
    assert (repo / "a.txt").read_text() == "hello world"


def test_append_writes_at_the_end(server, repo):
    async def go():
        h = await server.open(b"/app-feat/a.txt", FXF_WRITE | FXF_APPEND, SFTPAttrs())
        await server.write(h, 0, b"!!")

    run(go())
    assert (repo / "a.txt").read_text() == "hello world!!"


def test_exclusive_create_fails_when_the_file_exists(server):
    with pytest.raises(asyncssh.SFTPFailure):
        run(server.open(b"/app-feat/a.txt", FXF_WRITE | FXF_CREAT | FXF_EXCL, SFTPAttrs()))


def test_an_upload_past_the_size_cap_fails_without_writing(server, repo, monkeypatch):
    monkeypatch.setattr(sftp, "MAX_FILE_BYTES", 10)

    async def go():
        h = await server.open(b"/app-feat/big", FXF_WRITE | FXF_CREAT, SFTPAttrs())
        await server.write(h, 8, b"12345")

    with pytest.raises(asyncssh.SFTPFailure):
        run(go())
    assert (repo / "big").read_bytes() == b""


def test_mkdir_remove_rmdir_and_rename(server, repo):
    run(server.mkdir(b"/app-feat/d", SFTPAttrs(permissions=0o750)))
    assert (repo / "d").is_dir()
    run(server.rmdir(b"/app-feat/d"))
    run(server.rename(b"/app-feat/a.txt", b"/app-feat/b.txt"))
    assert (repo / "b.txt").exists() and not (repo / "a.txt").exists()
    run(server.remove(b"/app-feat/b.txt"))
    assert not (repo / "b.txt").exists()
    with pytest.raises(asyncssh.SFTPFailure):
        run(server.remove(b"/app-feat/sub"))  # remove never deletes a directory


def test_rename_refuses_an_existing_target_but_posix_rename_overwrites(server, repo):
    (repo / "b.txt").write_text("B")
    with pytest.raises(asyncssh.SFTPFailure):
        run(server.rename(b"/app-feat/a.txt", b"/app-feat/b.txt"))
    run(server.posix_rename(b"/app-feat/a.txt", b"/app-feat/b.txt"))
    assert (repo / "b.txt").read_text() == "hello world"


def test_rename_across_containers_is_refused(repo):
    incus = LocalIncus(
        [raw_container("app-a", repo_dir=str(repo)), raw_container("app-b", repo_dir=str(repo))]
    )
    srv = make_server(incus)
    with pytest.raises(asyncssh.SFTPPermissionDenied):
        run(srv.rename(b"/app-a/a.txt", b"/app-b/a.txt"))


def test_setstat_applies_permissions_and_ignores_ownership(server, repo):
    run(server.setstat(b"/app-feat/a.txt", SFTPAttrs(permissions=0o600, uid=0, gid=0)))
    assert stat_mod.S_IMODE((repo / "a.txt").stat().st_mode) == 0o600


def test_setstat_size_cannot_grow_past_the_cap(server, repo, monkeypatch):
    monkeypatch.setattr(sftp, "MAX_FILE_BYTES", 10)
    with pytest.raises(asyncssh.SFTPFailure, match="too large"):
        run(server.setstat(b"/app-feat/a.txt", SFTPAttrs(size=11)))
    assert (repo / "a.txt").read_text() == "hello world"
    run(server.setstat(b"/app-feat/a.txt", SFTPAttrs(size=5)))
    assert (repo / "a.txt").read_text() == "hello"


def test_a_rename_audit_line_names_the_new_path(server, repo, caplog):
    with caplog.at_level(logging.INFO, logger="jailbee.remote_ssh.sftp"):
        run(server.rename(b"/app-feat/a.txt", b"/app-feat/b.txt"))
    line = next(r.getMessage() for r in caplog.records if "op=rename" in r.getMessage())
    assert "path='/app-feat/a.txt'" in line and "new='/app-feat/b.txt'" in line


def test_links_cannot_be_created(server):
    for call in (
        lambda: server.symlink(b"/etc/passwd", b"/app-feat/x"),
        lambda: server.link(b"/app-feat/a.txt", b"/app-feat/y"),
        lambda: server.lsetstat(b"/app-feat/ok", SFTPAttrs(permissions=0o777)),
    ):
        with pytest.raises(asyncssh.SFTPPermissionDenied):
            run(_awaited(call()))


def test_unsupported_operations_say_so(server):
    for call in (
        lambda: server.statvfs(b"/app-feat"),
        lambda: server.lock(object(), 0, 1, 0),
        lambda: server.unlock(object(), 0, 1),
    ):
        with pytest.raises(asyncssh.SFTPOpUnsupported):
            run(_awaited(call()))


def test_a_name_with_metacharacters_is_literal(server, repo):
    name = "a b'; touch pwned; '$(id)"
    run(server.open(f"/app-feat/{name}".encode(), FXF_WRITE | FXF_CREAT, SFTPAttrs()))
    assert (repo / name).exists() and not (repo / "pwned").exists()


# ---- the base class must never reach the host ------------------------------

# Pure helpers the base class implements without touching a filesystem.
_INHERITED_OK = {"convert_attrs", "format_longname"}


def test_every_method_that_could_touch_the_host_is_overridden():
    public = {
        name
        for name, value in vars(SFTPServer).items()
        if callable(value) and not name.startswith("_")
    }
    missing = sorted(name for name in public - _INHERITED_OK if name not in vars(JailbeeSFTPServer))
    assert missing == []


def test_user_and_group_names_are_numeric_never_looked_up_on_the_host(server):
    assert server.format_user(0) == "0"
    assert server.format_group(1000) == "1000"
    assert server.format_user(None) == ""
    with pytest.raises(asyncssh.SFTPFailure):
        server.map_path(b"/etc")


# ---- audit -----------------------------------------------------------------


def test_every_operation_is_audited_with_the_key_and_the_result(server, caplog):
    with caplog.at_level(logging.INFO, logger="jailbee.remote_ssh.sftp"):
        run(server.stat(b"/app-feat/a.txt"))
        with pytest.raises(asyncssh.SFTPNoSuchFile):
            run(server.stat(b"/app-feat/nope"))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SFTP ")]
    assert len(lines) == 2
    assert FINGERPRINT in lines[0] and "container='app-feat'" in lines[0]
    assert "op=stat" in lines[0] and "result=ok" in lines[0]
    assert "result=not_found" in lines[1]


def _sftp_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("SFTP ")]


@pytest.mark.parametrize(
    ("call", "op", "result"),
    [
        (lambda s: s.mkdir(b"/new", SFTPAttrs()), "mkdir", "denied"),
        (lambda s: s.remove(b"/app-feat"), "remove", "denied"),
        (lambda s: s.rename(b"/app-a/a.txt", b"/app-b/a.txt"), "rename", "denied"),
        (lambda s: s.write(object(), 0, b"x"), "write", "failed"),
        (lambda s: s.symlink(b"/x", b"/app-feat/y"), "symlink", "denied"),
        (lambda s: s.statvfs(b"/app-feat"), "statvfs", "unsupported"),
        (lambda s: s.stat(b"/app-feat/a\x00b"), "stat", "invalid"),
        (lambda s: s.setstat(b"/", SFTPAttrs()), "setstat", "denied"),
        (lambda s: names(s, b"/"), "scandir", "ok"),
        (lambda s: s.stat(b"/"), "stat", "ok"),
    ],
)
def test_every_operation_incl_refusals_is_audited_exactly_once(repo, caplog, call, op, result):
    incus = LocalIncus(
        [raw_container("app-a", repo_dir=str(repo)), raw_container("app-b", repo_dir=str(repo))]
    )
    srv = make_server(incus)
    with caplog.at_level(logging.INFO, logger="jailbee.remote_ssh.sftp"):
        try:
            run(_awaited(call(srv)))
        except asyncssh.SFTPError:
            pass
    lines = _sftp_lines(caplog)
    assert len(lines) == 1
    assert f"op={op} " in lines[0] and f"result={result}" in lines[0]


def test_a_newline_in_the_container_name_cannot_forge_an_audit_line(server, caplog):
    with caplog.at_level(logging.INFO, logger="jailbee.remote_ssh.sftp"):
        with pytest.raises(asyncssh.SFTPNoSuchFile):
            run(server.stat(b"/evil\nSFTP forged result=ok/a"))
    lines = _sftp_lines(caplog)
    assert len(lines) == 1 and "\n" not in lines[0]


# ---- file types: asyncssh's SCP server reads SFTPAttrs.type ----------------


def test_attrs_carry_a_file_type_for_the_in_process_scp_server(server):
    fs = SFTPServerFS(server)
    assert run(fs.isdir(b"/")) and run(fs.isdir(b"/app-feat")) and run(fs.isdir(b"/app-feat/sub"))
    assert run(fs.exists(b"/app-feat/a.txt")) and not run(fs.isdir(b"/app-feat/a.txt"))
    assert run(fs.stat(b"/app-feat/a.txt")).type == FILEXFER_TYPE_REGULAR
    assert run(fs.stat(b"/app-feat/sub")).type == FILEXFER_TYPE_DIRECTORY
    assert run(server.lstat(b"/app-feat/ok")).type == FILEXFER_TYPE_SYMLINK
