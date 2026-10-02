"""SFTP and SCP over a virtual tree of containers' repo directories.

``/`` lists the containers this service may show; ``/<container>/…`` is that
container's repository directory. Nothing here reads or writes a host path: the
base ``asyncssh.SFTPServer`` implements every operation on the *host*
filesystem (and looks users up in the host's passwd), so this subclass
overrides every method that is not a pure helper — a test keeps it that way —
and sends the real work to ``ContainerFS``.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import stat as stat_mod
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

import asyncssh
from asyncssh import (
    FXF_APPEND,
    FXF_CREAT,
    FXF_EXCL,
    FXF_TRUNC,
    FXF_WRITE,
    SFTPAttrs,
    SFTPFailure,
    SFTPName,
    SFTPNoSuchFile,
    SFTPOpUnsupported,
    SFTPPermissionDenied,
    SFTPServer,
)
from asyncssh.constants import (
    FILEXFER_TYPE_DIRECTORY,
    FILEXFER_TYPE_REGULAR,
    FILEXFER_TYPE_SPECIAL,
    FILEXFER_TYPE_SYMLINK,
    FILEXFER_TYPE_UNKNOWN,
)

from jailbee.remote_ssh.container_fs import ContainerFS, FileStat, FSError

if TYPE_CHECKING:
    from jailbee.incus import Incus
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

log = logging.getLogger(__name__)

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])

# Largest file one upload may grow to, and the largest single read served.
MAX_FILE_BYTES = 2 * 1024**3
MAX_READ_BYTES = 1024 * 1024
# How long a container listing is trusted before `incus list` is asked again.
_CATALOG_TTL_SECONDS = 5.0
# Concurrent `incus exec` calls this service lets through, across every session.
MAX_CONCURRENT_EXECS = 8


class _InvalidPath(SFTPFailure):
    """A client path that cannot name anything here (audited as `invalid`)."""


def split_path(path: bytes) -> tuple[str, ...]:
    """A client path as components: `.`/empty dropped, `..` clamped at the root.

    An absolute path means "from the root of the tree". NUL and non-UTF-8 bytes
    are refused: names travel to the container as UTF-8 argv.
    """
    try:
        text = path.decode("utf-8")
    except UnicodeDecodeError:
        raise _InvalidPath("file names must be UTF-8") from None
    if "\0" in text:
        raise _InvalidPath("invalid file name")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return tuple(parts)


def _file_type(mode: int) -> int:
    """asyncssh's `FILEXFER_TYPE_*` for a stat mode; its SCP server reads `.type`."""
    kind = stat_mod.S_IFMT(mode)
    if kind == stat_mod.S_IFREG:
        return FILEXFER_TYPE_REGULAR
    if kind == stat_mod.S_IFDIR:
        return FILEXFER_TYPE_DIRECTORY
    if kind == stat_mod.S_IFLNK:
        return FILEXFER_TYPE_SYMLINK
    return FILEXFER_TYPE_SPECIAL if kind else FILEXFER_TYPE_UNKNOWN


def _virtual_dir_attrs() -> SFTPAttrs:
    now = int(time.time())
    mode = 0o040555
    return SFTPAttrs(
        type=_file_type(mode), size=0, uid=0, gid=0, permissions=mode, atime=now, mtime=now
    )


def _attrs(st: FileStat) -> SFTPAttrs:
    return SFTPAttrs(
        type=_file_type(st.mode),
        size=st.size,
        uid=st.uid,
        gid=st.gid,
        permissions=st.mode,
        atime=st.atime,
        mtime=st.mtime,
    )


def _sftp_error(exc: FSError) -> asyncssh.SFTPError:
    message = str(exc)
    if exc.kind == "not_found":
        return SFTPNoSuchFile(message)
    if exc.kind == "denied":
        return SFTPPermissionDenied(message)
    return SFTPFailure(message)


@dataclass(frozen=True)
class ContainerRef:
    name: str
    repo: str
    repo_dir: str


@dataclass
class SFTPService:
    """What every SFTP session of one server run shares."""

    incus: Incus
    # Called once per SFTP channel. `None` means the policy could not be
    # established (or file transfer is off now): that session sees nothing.
    scope_source: Callable[[], RemoteRepoScope | None]
    gate: asyncio.Semaphore

    def server(self, chan: asyncssh.SSHServerChannel[bytes]) -> JailbeeSFTPServer:
        return JailbeeSFTPServer(chan, self)


class _Catalog:
    """Which containers a session may see, and each one's `ContainerFS` (blocking calls)."""

    def __init__(self, incus: Incus, scope: RemoteRepoScope | None) -> None:
        self._incus = incus
        self._scope = scope
        self._listed_at = float("-inf")
        self._refs: dict[str, ContainerRef] = {}
        self._fs: dict[str, ContainerFS] = {}

    def containers(self) -> dict[str, ContainerRef]:
        if self._scope is None:
            return {}  # fail closed: no trustworthy policy for this session
        if time.monotonic() - self._listed_at < _CATALOG_TTL_SECONDS:
            return self._refs
        refs: dict[str, ContainerRef] = {}
        for raw in self._incus.list_containers(fast=True):
            repo = next(
                (
                    p[: -len("-base")]
                    for p in raw.get("profiles") or []
                    if p.endswith("-base") and p != "default"
                ),
                None,
            )
            config = raw.get("config") or {}
            repo_dir = config.get("user.jailbee.repo_dir")
            if (
                repo is None
                # `--mount` containers bind the HOST checkout at the repo dir.
                or config.get("user.jailbee.mode") == "mount"
                or not isinstance(repo_dir, str)
                or not repo_dir
                or raw.get("status") != "Running"
                or not self._scope.allows(repo)
            ):
                continue
            refs[raw["name"]] = ContainerRef(raw["name"], repo, repo_dir)
        self._refs, self._listed_at = refs, time.monotonic()
        return refs

    def fs(self, ref: ContainerRef) -> ContainerFS:
        cached = self._fs.get(ref.name)
        if cached is not None:
            return cached
        probe = self._incus.exec_bytes(
            ref.name, ["stat", "-c", "%u %g", "--", ref.repo_dir], timeout=30
        )
        try:
            uid, gid = (int(x) for x in probe.stdout.split())
        except ValueError:
            raise FSError("not_found", "repository directory not found") from None
        if probe.returncode != 0:
            raise FSError("not_found", "repository directory not found")
        if uid == 0:
            # Files must not be created as root; the clone is the container user's.
            raise FSError("denied", "repository directory is owned by root")
        fs = ContainerFS(self._incus, ref.name, ref.repo_dir, uid, gid)
        self._fs[ref.name] = fs
        return fs


@dataclass(frozen=True)
class _Handle:
    parts: tuple[str, ...]
    writable: bool
    append: bool


def _result_of(exc: BaseException) -> str:
    if isinstance(exc, _InvalidPath):
        return "invalid"
    if isinstance(exc, SFTPNoSuchFile):
        return "not_found"
    if isinstance(exc, SFTPPermissionDenied):
        return "denied"
    if isinstance(exc, SFTPOpUnsupported):
        return "unsupported"
    if isinstance(exc, SFTPFailure):
        return "failed"
    return "error" if isinstance(exc, Exception) else "aborted"


def _audited(op: str) -> Callable[[F], F]:
    """Audit one SFTP operation exactly once, whatever way it ends.

    The subject is the first argument: a path, or a handle. Sync, async and
    async-generator methods keep their kind, since asyncssh accepts all three.
    """

    def decorate(fn: F) -> F:
        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def agen(self: JailbeeSFTPServer, *args: Any) -> AsyncIterator[Any]:
                result = "ok"
                try:
                    async for item in fn(self, *args):
                        yield item
                except BaseException as exc:
                    result = _result_of(exc)
                    raise
                finally:
                    self._audit(op, args, result)

            return cast("F", agen)
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def coro(self: JailbeeSFTPServer, *args: Any) -> Any:
                result = "ok"
                try:
                    return await fn(self, *args)
                except BaseException as exc:
                    result = _result_of(exc)
                    raise
                finally:
                    self._audit(op, args, result)

            return cast("F", coro)

        @functools.wraps(fn)
        def sync(self: JailbeeSFTPServer, *args: Any) -> Any:
            result = "ok"
            try:
                return fn(self, *args)
            except BaseException as exc:
                result = _result_of(exc)
                raise
            finally:
                self._audit(op, args, result)

        return cast("F", sync)

    return decorate


class JailbeeSFTPServer(SFTPServer):
    def __init__(self, chan: asyncssh.SSHServerChannel[bytes], service: SFTPService) -> None:
        super().__init__(chan)
        self._service = service
        self._catalog = _Catalog(service.incus, service.scope_source())

    # ---- plumbing -----------------------------------------------------------

    async def _blocking(self, fn: Callable[..., T], *args: Any) -> T:
        async with self._service.gate:
            return await asyncio.to_thread(fn, *args)

    def _audit(self, op: str, args: tuple[Any, ...], result: str) -> None:
        subject = args[0] if args else None
        parts: tuple[str, ...] | None = None
        if isinstance(subject, _Handle):
            parts = subject.parts
        elif isinstance(subject, bytes):
            try:
                parts = split_path(subject)
            except SFTPFailure:
                vpath = subject.decode("utf-8", "backslashreplace")
        if parts is not None:
            vpath = "/" + "/".join(parts)
        elif not isinstance(subject, bytes):
            vpath = "?"
        container = parts[0] if parts else None
        new = "-"
        if op in ("rename", "posix_rename") and len(args) > 1 and isinstance(args[1], bytes):
            new = repr(args[1].decode("utf-8", "backslashreplace"))
        log.info(
            "SFTP source=%r fingerprint=%s container=%r op=%s path=%r new=%s result=%s",
            self.channel.get_extra_info("peername"),
            self.channel.get_extra_info("jailbee_key_fingerprint"),
            container,
            op,
            vpath,
            new,
            result,
        )

    async def _on_container(
        self, op: str, parts: tuple[str, ...], call: Callable[[ContainerFS, str], T]
    ) -> T:
        container = parts[0] if parts else None
        try:
            ref = (await self._blocking(self._catalog.containers)).get(parts[0]) if parts else None
            if ref is None:
                raise FSError("not_found", "no such file or directory")
            fs = await self._blocking(self._catalog.fs, ref)
            return await self._blocking(call, fs, "/".join(parts[1:]))
        except FSError as exc:
            raise _sftp_error(exc) from None
        except asyncssh.SFTPError:
            raise
        except Exception:
            log.exception("SFTP internal error op=%s container=%r", op, container)
            raise SFTPFailure("internal error") from None

    @staticmethod
    def _need_inside_repo(parts: tuple[str, ...]) -> None:
        if len(parts) < 2:
            raise SFTPPermissionDenied("the top of the tree is read-only")

    # ---- metadata -----------------------------------------------------------

    @_audited("stat")
    async def stat(self, path: bytes) -> SFTPAttrs:
        parts = split_path(path)
        if not parts:
            return _virtual_dir_attrs()
        return _attrs(
            await self._on_container("stat", parts, lambda fs, rel: fs.stat(rel, follow=True))
        )

    @_audited("lstat")
    async def lstat(self, path: bytes) -> SFTPAttrs:
        parts = split_path(path)
        if not parts:
            return _virtual_dir_attrs()
        return _attrs(
            await self._on_container("lstat", parts, lambda fs, rel: fs.stat(rel, follow=False))
        )

    @_audited("fstat")
    async def fstat(self, file_obj: object) -> SFTPAttrs:
        handle = _as_handle(file_obj)
        return _attrs(
            await self._on_container(
                "fstat", handle.parts, lambda fs, rel: fs.stat(rel, follow=True)
            )
        )

    @_audited("scandir")
    async def scandir(self, path: bytes) -> AsyncIterator[SFTPName]:
        parts = split_path(path)
        for dots in (b".", b".."):
            yield SFTPName(dots, attrs=_virtual_dir_attrs())
        if not parts:
            refs = await self._blocking(self._catalog.containers)
            for name in sorted(refs):
                yield SFTPName(name.encode(), attrs=_virtual_dir_attrs())
            return
        entries = await self._on_container("scandir", parts, lambda fs, rel: fs.listdir(rel))
        for entry in entries:
            yield SFTPName(entry.name.encode(), attrs=_attrs(entry.stat))

    def realpath(self, path: bytes) -> bytes:
        return ("/" + "/".join(split_path(path))).encode()

    @_audited("readlink")
    async def readlink(self, path: bytes) -> bytes:
        parts = split_path(path)
        self._need_inside_repo(parts)
        target = await self._on_container("readlink", parts, lambda fs, rel: fs.readlink(rel))
        return target.encode()

    @_audited("setstat")
    async def setstat(self, path: bytes, attrs: SFTPAttrs) -> None:
        await self._setstat("setstat", split_path(path), attrs)

    @_audited("fsetstat")
    async def fsetstat(self, file_obj: object, attrs: SFTPAttrs) -> None:
        await self._setstat("fsetstat", _as_handle(file_obj).parts, attrs)

    async def _setstat(self, op: str, parts: tuple[str, ...], attrs: SFTPAttrs) -> None:
        self._need_inside_repo(parts)
        mode = attrs.permissions & 0o7777 if attrs.permissions is not None else None
        size, atime, mtime = attrs.size, attrs.atime, attrs.mtime
        if size is not None and size > MAX_FILE_BYTES:
            raise SFTPFailure("file too large")
        if mode is None and size is None and atime is None and mtime is None:
            return  # ownership and the rest are ignored, never an error
        await self._on_container(
            op,
            parts,
            lambda fs, rel: fs.setstat(
                rel,
                mode=mode,
                size=size,
                atime=int(atime) if atime is not None else None,
                mtime=int(mtime) if mtime is not None else None,
            ),
        )

    # ---- file content -------------------------------------------------------

    @_audited("open")
    async def open(self, path: bytes, pflags: int, attrs: SFTPAttrs) -> object:
        parts = split_path(path)
        self._need_inside_repo(parts)
        create, truncate, exclusive = (
            bool(pflags & FXF_CREAT),
            bool(pflags & FXF_TRUNC),
            bool(pflags & FXF_EXCL),
        )
        writable = bool(pflags & (FXF_WRITE | FXF_APPEND))
        if (create or truncate or exclusive) and not writable:
            raise SFTPPermissionDenied("not opened for writing")
        await self._on_container(
            "open",
            parts,
            lambda fs, rel: fs.open(rel, create=create, truncate=truncate, exclusive=exclusive),
        )
        return _Handle(parts, writable, bool(pflags & FXF_APPEND))

    @_audited("read")
    async def read(self, file_obj: object, offset: int, size: int) -> bytes:
        handle = _as_handle(file_obj)
        wanted = min(size, MAX_READ_BYTES)
        return await self._on_container(
            "read", handle.parts, lambda fs, rel: fs.read(rel, offset, wanted)
        )

    @_audited("write")
    async def write(self, file_obj: object, offset: int, data: bytes) -> int:
        handle = _as_handle(file_obj)
        if not handle.writable:
            raise SFTPPermissionDenied("not opened for writing")
        if handle.append:
            offset = (
                await self._on_container(
                    "write", handle.parts, lambda fs, rel: fs.stat(rel, follow=True)
                )
            ).size
        if offset + len(data) > MAX_FILE_BYTES:
            raise SFTPFailure("file too large")
        await self._on_container("write", handle.parts, lambda fs, rel: fs.write(rel, offset, data))
        return len(data)

    def close(self, file_obj: object) -> None:
        return None  # handles hold no container state

    def fsync(self, file_obj: object) -> None:
        return None

    # ---- namespace changes --------------------------------------------------

    @_audited("mkdir")
    async def mkdir(self, path: bytes, attrs: SFTPAttrs) -> None:
        parts = split_path(path)
        self._need_inside_repo(parts)
        mode = attrs.permissions if attrs.permissions is not None else 0o755
        await self._on_container("mkdir", parts, lambda fs, rel: fs.mkdir(rel, mode & 0o777))

    @_audited("remove")
    async def remove(self, path: bytes) -> None:
        parts = split_path(path)
        self._need_inside_repo(parts)
        await self._on_container("remove", parts, lambda fs, rel: fs.remove(rel))

    @_audited("rmdir")
    async def rmdir(self, path: bytes) -> None:
        parts = split_path(path)
        self._need_inside_repo(parts)
        await self._on_container("rmdir", parts, lambda fs, rel: fs.rmdir(rel))

    @_audited("rename")
    async def rename(self, oldpath: bytes, newpath: bytes) -> None:
        await self._rename("rename", oldpath, newpath, overwrite=False)

    @_audited("posix_rename")
    async def posix_rename(self, oldpath: bytes, newpath: bytes) -> None:
        await self._rename("posix_rename", oldpath, newpath, overwrite=True)

    async def _rename(self, op: str, oldpath: bytes, newpath: bytes, *, overwrite: bool) -> None:
        old, new = split_path(oldpath), split_path(newpath)
        self._need_inside_repo(old)
        self._need_inside_repo(new)
        if old[0] != new[0]:
            raise SFTPPermissionDenied("cannot move files between containers")
        new_rel = "/".join(new[1:])
        await self._on_container(
            op, old, lambda fs, rel: fs.rename(rel, new_rel, overwrite=overwrite)
        )

    # ---- refused or unsupported ----------------------------------------------

    @_audited("symlink")
    def symlink(self, oldpath: bytes, newpath: bytes) -> None:
        raise SFTPPermissionDenied("links cannot be created")

    @_audited("link")
    def link(self, oldpath: bytes, newpath: bytes) -> None:
        raise SFTPPermissionDenied("links cannot be created")

    @_audited("lsetstat")
    def lsetstat(self, path: bytes, attrs: SFTPAttrs) -> None:
        raise SFTPPermissionDenied("links cannot be modified")

    @_audited("statvfs")
    def statvfs(self, path: bytes) -> Any:
        raise SFTPOpUnsupported("statvfs is not supported")

    @_audited("fstatvfs")
    def fstatvfs(self, file_obj: object) -> Any:
        raise SFTPOpUnsupported("statvfs is not supported")

    @_audited("lock")
    def lock(self, file_obj: object, offset: int, length: int, flags: int) -> None:
        raise SFTPOpUnsupported("byte range locks are not supported")

    @_audited("unlock")
    def unlock(self, file_obj: object, offset: int, length: int) -> None:
        raise SFTPOpUnsupported("byte range locks are not supported")

    @_audited("open56")
    def open56(self, path: bytes, desired_access: int, flags: int, attrs: SFTPAttrs) -> Any:
        raise SFTPOpUnsupported("only SFTP version 3 is supported")

    # ---- host-facing helpers the base class would otherwise use ---------------

    def map_path(self, path: bytes) -> bytes:
        raise SFTPFailure("host paths are not available")

    def reverse_map_path(self, path: bytes) -> bytes:
        raise SFTPFailure("host paths are not available")

    def format_user(self, uid: int | None) -> str:
        return "" if uid is None else str(uid)  # never the host's passwd

    def format_group(self, gid: int | None) -> str:
        return "" if gid is None else str(gid)

    def exit(self) -> None:
        return None


def _as_handle(file_obj: object) -> _Handle:
    if not isinstance(file_obj, _Handle):
        raise SFTPFailure("invalid handle")
    return file_obj
