"""Confined file operations inside one container's repo directory.

Every operation is a single ``incus exec`` of a *constant* ``sh -c`` script.
Client-supplied values arrive only as positional arguments (``$1`` ...), never
inside the script text, so a hostile file name is just data. Confinement is
enforced where the symlinks live: the script resolves the target with
``realpath`` inside the container and refuses anything outside the repo root.
The host filesystem is not involved at all.

Accepted limit: the check and the action are separate steps inside the
container, so a process *in that container* could race a symlink swap. The
boundary protects the container from the remote client, not from itself.

Script exit codes: 2 not found, 4 outside the repo / refused, 5 not a
directory, 6 not a regular file, 7 already exists, 8 is a directory, anything
else is a plain failure (stderr carries the reason).
"""

from __future__ import annotations

import posixpath
import stat as stat_mod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.incus import Incus

FSErrorKind = Literal["not_found", "denied", "exists", "not_dir", "not_file", "is_dir", "failure"]

_EXIT_KINDS: dict[int, FSErrorKind] = {
    2: "not_found",
    4: "denied",
    5: "not_dir",
    6: "not_file",
    7: "exists",
    8: "is_dir",
}

# $1 repo root, $2 parent dir relative to it, $3 final component ("" = the dir
# itself). `dir` is the resolved parent (contained); `path` = dir/base.
_PREAMBLE = r"""set -eu
root=$(realpath -e -- "$1") || exit 2
dir=$(realpath -m -- "$root/$2") || exit 2
case $dir in "$root"|"$root"/*) ;; *) exit 4 ;; esac
if [ -n "$3" ]; then path=$dir/$3; else path=$dir; fi
"""

# Resolve `path` following symlinks, and require the result to stay in the repo.
_FOLLOW = r"""[ -e "$path" ] || exit 2
real=$(realpath -e -- "$path") || exit 2
case $real in "$root"|"$root"/*) ;; *) exit 4 ;; esac
"""

_STAT_FORMAT = r"%f %s %u %g %X %Y"

_STAT = (
    _FOLLOW
    + rf"""exec stat -c '{_STAT_FORMAT}' -- "$real"
"""
)
_LSTAT = (
    r"""[ -e "$path" ] || [ -L "$path" ] || exit 2
"""
    + rf"""exec stat -c '{_STAT_FORMAT}' -- "$path"
"""
)
_LIST = (
    _FOLLOW
    + r"""[ -d "$real" ] || exit 5
exec find "$real" -mindepth 1 -maxdepth 1 -printf '%f\0%m\0%y\0%s\0%U\0%G\0%A@\0%T@\0'
"""
)
# $4 offset, $5 size
_READ = (
    _FOLLOW
    + r"""[ -f "$real" ] || exit 6
exec dd if="$real" bs=65536 skip="$4" count="$5" iflag=skip_bytes,count_bytes status=none
"""
)
# $4 offset; data on stdin; never creates
_WRITE = (
    _FOLLOW
    + r"""[ -f "$real" ] || exit 6
exec dd of="$real" bs=65536 seek="$4" oflag=seek_bytes conv=notrunc,nocreat status=none
"""
)
# $4 flags: any of c (create) t (truncate) x (exclusive)
_OPEN = (
    r"""case $4 in *x*) if [ -e "$path" ] || [ -L "$path" ]; then exit 7; fi;; esac
if [ -e "$path" ] || [ -L "$path" ]; then
"""
    + _FOLLOW
    + r"""[ -f "$real" ] || exit 6; target=$real
else case $4 in *c*) ;; *) exit 2;; esac; [ -d "$dir" ] || exit 2; target=$path; fi
case $4 in *t*) : > "$target";; *c*) : >> "$target";; esac
"""
)
# $4 octal mode
_MKDIR = r"""[ -n "$3" ] || exit 4
[ -d "$dir" ] || exit 2
if [ -e "$path" ] || [ -L "$path" ]; then exit 7; fi
exec mkdir -m "$4" -- "$path"
"""
_RMDIR = r"""[ -n "$3" ] || exit 4
[ -e "$path" ] || [ -L "$path" ] || exit 2
if [ -L "$path" ] || [ ! -d "$path" ]; then exit 5; fi
exec rmdir -- "$path"
"""
_REMOVE = r"""[ -n "$3" ] || exit 4
[ -e "$path" ] || [ -L "$path" ] || exit 2
if [ -d "$path" ] && [ ! -L "$path" ]; then exit 8; fi
exec rm -- "$path"
"""
_READLINK = r"""[ -L "$path" ] || exit 9
t=$(readlink -- "$path")
case $t in /*) res=$(realpath -m -- "$t");; *) res=$(realpath -m -- "$dir/$t");; esac
case $res in "$root"|"$root"/*) ;; *) exit 4 ;; esac
printf '%s' "$t"
"""
# $4 octal mode | -, $5 mtime | -, $6 atime | -, $7 size | -. Size goes first: a
# truncate resets mtime, which the caller may be setting explicitly.
_SETSTAT = (
    _FOLLOW
    + r"""[ "$real" != "$root" ] || exit 4
if [ "$7" != - ]; then [ -f "$real" ] || exit 6; truncate -s "$7" -- "$real"; fi
if [ "$4" != - ]; then chmod "$4" -- "$real"; fi
if [ "$5" != - ]; then touch -m -d "@$5" -- "$real"; fi
if [ "$6" != - ]; then touch -a -d "@$6" -- "$real"; fi
"""
)
# Own preamble: $1 root, $2 old parent, $3 old base, $4 new parent, $5 new base,
# $6 `n` (refuse an existing target) or `o` (overwrite).
_RENAME = r"""set -eu
root=$(realpath -e -- "$1") || exit 2
d1=$(realpath -m -- "$root/$2") || exit 2
d2=$(realpath -m -- "$root/$4") || exit 2
for d in "$d1" "$d2"; do case $d in "$root"|"$root"/*) ;; *) exit 4 ;; esac; done
[ -n "$3" ] && [ -n "$5" ] || exit 4
src=$d1/$3
dst=$d2/$5
[ -e "$src" ] || [ -L "$src" ] || exit 2
[ -d "$d2" ] || exit 2
if [ "$6" = n ] && { [ -e "$dst" ] || [ -L "$dst" ]; }; then exit 7; fi
exec mv -T -- "$src" "$dst"
"""

_TYPE_BITS = {
    "d": stat_mod.S_IFDIR,
    "f": stat_mod.S_IFREG,
    "l": stat_mod.S_IFLNK,
}
# Cap on one directory listing a container may hand back (bytes of `find` output).
_MAX_LISTING_BYTES = 8 * 1024 * 1024


class FSError(Exception):
    """A file operation failed; ``kind`` says how, ``str(exc)`` is safe to show."""

    def __init__(self, kind: FSErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class FileStat:
    mode: int
    size: int
    uid: int
    gid: int
    atime: int
    mtime: int


@dataclass(frozen=True)
class DirEntry:
    name: str
    stat: FileStat


def _split(rel: str) -> tuple[str, str]:
    """Validate a normalised relative path; return (parent, base). ``""`` -> ("", "")."""
    if rel == "":
        return "", ""
    parts = rel.split("/")
    if "\0" in rel or any(part in ("", ".", "..") for part in parts):
        raise FSError("denied", "invalid path")
    parent, base = posixpath.split(rel)
    return parent, base


def _first_line(raw: bytes) -> str:
    line = raw.decode("utf-8", errors="replace").strip().splitlines()
    return line[0][:200] if line else ""


def _parse_stat(raw: bytes) -> FileStat:
    try:
        mode_hex, size, uid, gid, atime, mtime = raw.decode().split()
        return FileStat(int(mode_hex, 16), int(size), int(uid), int(gid), int(atime), int(mtime))
    except ValueError as exc:
        raise FSError("failure", "unreadable stat output") from exc


class ContainerFS:
    """One container's repo directory, reached through ``Incus.exec_bytes``."""

    def __init__(
        self,
        incus: Incus,
        container: str,
        repo_dir: str,
        uid: int,
        gid: int,
        *,
        timeout: int = 60,
    ) -> None:
        self._incus = incus
        self._container = container
        self._repo_dir = repo_dir
        self._uid = uid
        self._gid = gid
        self._timeout = timeout

    def _exec(
        self,
        script: str,
        *args: str,
        stdin: bytes | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        try:
            result = self._incus.exec_bytes(
                self._container,
                ["sh", "-c", script, "sh", self._repo_dir, *args],
                input_bytes=stdin,
                uid=self._uid,
                gid=self._gid,
                timeout=self._timeout,
                max_bytes=max_bytes,
            )
        except IncusError as exc:
            raise FSError("failure", "container unavailable") from exc
        if result.returncode == 0:
            return result.stdout
        kind = _EXIT_KINDS.get(result.returncode, "failure")
        raise FSError(kind, _first_line(result.stderr) or kind.replace("_", " "))

    def _on(
        self,
        script: str,
        rel: str,
        *extra: str,
        stdin: bytes | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        parent, base = _split(rel)
        return self._exec(
            _PREAMBLE + script, parent, base, *extra, stdin=stdin, max_bytes=max_bytes
        )

    def stat(self, rel: str, *, follow: bool) -> FileStat:
        return _parse_stat(self._on(_STAT if follow else _LSTAT, rel))

    def listdir(self, rel: str) -> list[DirEntry]:
        raw = self._on(_LIST, rel, max_bytes=_MAX_LISTING_BYTES)
        fields = raw.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        entries: list[DirEntry] = []
        for i in range(0, len(fields) - len(fields) % 8, 8):
            name_b, perm, kind, size, uid, gid, atime, mtime = fields[i : i + 8]
            try:
                name = name_b.decode("utf-8")
                mode = _TYPE_BITS.get(kind.decode(), stat_mod.S_IFREG) | int(perm, 8)
                stat = FileStat(
                    mode,
                    int(size),
                    int(uid),
                    int(gid),
                    int(atime.split(b".")[0]),
                    int(mtime.split(b".")[0]),
                )
            except ValueError:
                continue  # a name or field we cannot represent is left out, not guessed
            entries.append(DirEntry(name, stat))
        return entries

    def read(self, rel: str, offset: int, size: int) -> bytes:
        return self._on(_READ, rel, str(offset), str(size), max_bytes=size)

    def write(self, rel: str, offset: int, data: bytes) -> None:
        if data:
            self._on(_WRITE, rel, str(offset), stdin=data)

    def open(self, rel: str, *, create: bool, truncate: bool, exclusive: bool) -> None:
        flags = ("c" if create else "") + ("t" if truncate else "") + ("x" if exclusive else "")
        self._on(_OPEN, rel, flags or "-")

    def mkdir(self, rel: str, mode: int) -> None:
        self._on(_MKDIR, rel, f"{mode & 0o7777:o}")

    def remove(self, rel: str) -> None:
        self._on(_REMOVE, rel)

    def rmdir(self, rel: str) -> None:
        self._on(_RMDIR, rel)

    def rename(self, old: str, new: str, *, overwrite: bool) -> None:
        old_parent, old_base = _split(old)
        new_parent, new_base = _split(new)
        self._exec(_RENAME, old_parent, old_base, new_parent, new_base, "o" if overwrite else "n")

    def readlink(self, rel: str) -> str:
        return self._on(_READLINK, rel).decode("utf-8", errors="replace")

    def setstat(
        self,
        rel: str,
        *,
        mode: int | None = None,
        size: int | None = None,
        atime: int | None = None,
        mtime: int | None = None,
    ) -> None:
        if rel == "":
            raise FSError("denied", "the repository root cannot be modified")

        def num(value: int | None, fmt: str = "d") -> str:
            return "-" if value is None else format(value, fmt)

        self._on(
            _SETSTAT,
            rel,
            num(mode & 0o7777 if mode is not None else None, "o"),
            num(mtime),
            num(atime),
            num(size),
        )
