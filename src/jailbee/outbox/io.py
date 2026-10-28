"""Bounded, read-only snapshots of container-written outbox stores."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import io
import os
import tarfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import local
from typing import TYPE_CHECKING, cast

from jailbee.db import state_dir
from jailbee.incus import IncusError
from jailbee.outbox.models import Kind, OutboxChanged, OutboxExecutionError, StoreSnapshot
from jailbee.outbox_io import ContainerIdentity, acquire_outbox_lock

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

FILE_LIMIT = 256 * 1024
SNAPSHOT_LIMIT = 8 * 1024 * 1024
# Inspection is interactive: a wedged daemon/helper must fail, not hang or look empty.
READ_TIMEOUT = 30
_METADATA = ".jailbee-snapshot"

# Copy into a private staging directory before archiving. Never follow links or
# block on a raced FIFO; recheck identity/link count after copying. Metadata is a
# versioned NUL-delimited record, so arbitrary rejected names need no JSON tools.
_READ_SCRIPT = r"""
set -euo pipefail
export LC_ALL=C
store=$1
limit=$2
cap=$3
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
mkdir "$tmp/files"
meta="$tmp/files/.jailbee-snapshot"
check_path() {
    local path=/ component
    IFS=/ read -ra components <<< "${store#/}"
    for component in "${components[@]}"; do
        [[ -x "$path" && -d "$path" && ! -L "$path" ]] || {
            echo 'inaccessible ancestor' >&2; exit 1;
        }
        path="${path%/}/$component"
        [[ ! -L "$path" ]] || { echo 'symlink ancestor' >&2; exit 1; }
        if [[ ! -e "$path" ]]; then
            [[ -r "${path%/*}" ]] || { echo 'inaccessible ancestor' >&2; exit 1; }
            return 2
        fi
        [[ -d "$path" ]] || { echo 'non-directory ancestor' >&2; exit 1; }
    done
    [[ -r "$store" && -x "$store" ]] || { echo 'inaccessible store' >&2; exit 1; }
}
if check_path; then
    printf 'v1\0present\0' > "$meta"
    cd -- "$store"
    store_identity=$(stat -c '%d:%i' .)
    shopt -s nullglob dotglob
    total=0
    for file in ./*; do
        name=${file##*/}
        reject=false
        if [[ "$name" == .jailbee-snapshot || -L "$file" || ! -f "$file" ]]; then
            reject=true
        else
            before=$(stat -c '%d:%i:%h:%s:%y:%z' -- "$file")
            links=$(stat -c %h -- "$file")
            size=$(stat -c %s -- "$file")
            if (( links != 1 || size > limit )); then
                reject=true
            else
                # GNU dd bounds even a concurrently growing file and refuses symlinks.
                dd if="$file" of="$tmp/files/$name" iflag=nofollow,nonblock \
                    bs=262145 count=1 status=none
                after=$(stat -c '%d:%i:%h:%s:%y:%z' -- "$file")
                copied=$(stat -c %s -- "$tmp/files/$name")
                if [[ "$before" != "$after" ]] || (( copied != size )); then
                    echo 'store changed while reading' >&2; exit 1
                fi
                total=$((total + ((copied + 511) / 512 + 1) * 512))
                (( total * 4 / 3 <= cap )) || { echo 'snapshot overflow' >&2; exit 1; }
            fi
        fi
        if "$reject"; then
            printf 'R\0%s\0' "$name" >> "$meta"
        fi
        (( $(stat -c %s -- "$meta") <= limit )) || { echo 'metadata overflow' >&2; exit 1; }
    done
    check_path || { echo 'store changed while reading' >&2; exit 1; }
    [[ "$(stat -c '%d:%i' -- "$store")" == "$store_identity" ]] || {
        echo 'store replaced' >&2; exit 1;
    }
else
    printf 'v1\0missing\0' > "$meta"
fi
# The extra byte detects overflow rather than silently accepting truncation.
# pipefail preserves tar failures, including SIGPIPE when the bound is reached.
if ! tar -C "$tmp/files" --format=ustar -cf - -- . | base64 -w0 |
    head -c "$((cap + 1))" > "$tmp/encoded"; then
    echo 'snapshot overflow or archive failure' >&2; exit 1
fi
(( $(stat -c %s "$tmp/encoded") <= cap )) || { echo 'snapshot overflow' >&2; exit 1; }
cat "$tmp/encoded"
"""


def store_directory(kind: Kind) -> str:
    """Return the existing store location; inspection never creates it."""
    if kind not in ("pr", "issue"):
        raise OutboxExecutionError("unknown outbox kind")
    return f"/home/dev/.jailbee/{kind}-outbox"


# Walk via open directory descriptors: a concurrent rename/symlink replacement
# cannot redirect creation through an unchecked ancestor.
_CREATE_SCRIPT = r"""
import os
import sys

flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
for store in sys.argv[1:]:
    fd = os.open('/', flags)
    try:
        for component in store.strip('/').split('/'):
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)
"""


def ensure_directories(cfg: Config, incus: Incus, container: str) -> None:
    """Bootstrap fixed stores as their writer, refusing links and non-directories."""
    try:
        incus.exec(
            container,
            ["python3", "-c", _CREATE_SCRIPT, store_directory("pr"), store_directory("issue")],
            uid=cfg.container_user.uid,
            timeout=30,
        )
    except IncusError as exc:
        raise OutboxExecutionError(
            f"Could not create outbox directories in '{container}': {exc}"
        ) from exc


def _decode(kind: Kind, raw: str) -> StoreSnapshot:
    if len(raw) > SNAPSHOT_LIMIT:
        raise OutboxExecutionError("snapshot overflow")
    try:
        blob = base64.b64decode(raw, validate=True)
        files: dict[str, str] = {}
        rejected: list[str] = []
        warnings: list[str] = []
        metadata: bytes | None = None
        seen: set[str] = set()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
            for member in archive:
                if member.name == "." and member.isdir():
                    continue
                name = member.name.removeprefix("./")
                if not name or "/" in name or name in seen:
                    raise OutboxExecutionError("invalid snapshot archive entry")
                seen.add(name)
                if name == _METADATA:
                    if not member.isfile() or member.size > FILE_LIMIT:
                        raise OutboxExecutionError("invalid snapshot metadata")
                elif not member.isfile() or member.size > FILE_LIMIT:
                    rejected.append(name)
                    warnings.append(f"Rejected unsafe or oversized file: {name}")
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    raise OutboxExecutionError("unreadable snapshot entry")
                content = stream.read(FILE_LIMIT + 1)
                if len(content) != member.size:
                    raise OutboxExecutionError("truncated snapshot entry")
                if name == _METADATA:
                    metadata = content
                else:
                    try:
                        files[name] = content.decode("utf-8")
                    except UnicodeDecodeError:
                        rejected.append(name)
                        warnings.append(f"Rejected non-UTF8 file: {name}")
        if metadata is None:
            raise OutboxExecutionError("missing snapshot metadata")
        fields = metadata.decode("utf-8").split("\0")
        if fields[:2] not in (["v1", "present"], ["v1", "missing"]) or fields[-1] != "":
            raise OutboxExecutionError("invalid snapshot metadata")
        records = fields[2:-1]
        if len(records) % 2 or (fields[1] == "missing" and (records or files or rejected)):
            raise OutboxExecutionError("invalid snapshot metadata")
        for tag, value in zip(records[::2], records[1::2], strict=True):
            if tag not in ("R", "W") or not value:
                raise OutboxExecutionError("invalid snapshot metadata record")
            if tag == "R":
                if "/" in value or value in files or value in rejected:
                    raise OutboxExecutionError("invalid rejected inventory")
                rejected.append(value)
            else:
                warnings.append(value)
        return StoreSnapshot(kind, tuple(sorted(files.items())), tuple(rejected), tuple(warnings))
    except (ValueError, UnicodeError, binascii.Error, tarfile.TarError, OSError) as exc:
        raise OutboxExecutionError(f"invalid snapshot: {exc}") from exc


def read_store(incus: Incus, container: str, kind: Kind, *, uid: int | None) -> StoreSnapshot:
    """Read a strict bounded snapshot, raising on unavailable or malformed stores."""
    directory = store_directory(kind)
    try:
        raw = incus.exec(
            container,
            ["bash", "-c", _READ_SCRIPT, "bash", directory, str(FILE_LIMIT), str(SNAPSHOT_LIMIT)],
            uid=uid,
            timeout=READ_TIMEOUT,
        )
    except IncusError as exc:
        raise OutboxExecutionError(f"outbox unavailable: {exc}") from exc
    return _decode(kind, raw)


MUTATION_TIMEOUT = 30


class MutationExecutionError(OutboxExecutionError):
    """Failed execution carrying only positively acknowledged removals."""

    def __init__(self, message: str, removed_names: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.removed_names = removed_names


# stdout is the wrapper's only result channel. Capture internal stderr even when
# a command exits zero; acknowledge each unlink before reporting a partial error.
_MUTATE_SCRIPT = r"""
set -uo pipefail
export LC_ALL=C
store=$1; limit=$2; target=$3; progress=$4; shift 4
count=$1; shift
declare -A hashes inventory
for ((i=0; i<count; i++)); do
    inventory["$1"]=1; hashes["$1"]=$2; shift 2
done
count=$1; shift
for ((i=0; i<count; i++)); do inventory["$1"]=1; shift; done
deletes=("$@")
removed=()
tmp=; err=
cleanup() {
    local failed=0
    [[ -z "$tmp" ]] || /bin/rm -f -- "$tmp" || failed=1
    [[ -z "$err" ]] || /bin/rm -f -- "$err" || failed=1
    return "$failed"
}
trap cleanup EXIT
# Initialization has no error file yet. Preserve single-line stdout separately
# from stderr using a trailing status/value frame, including zero-exit warnings.
initial_command() {
    local packet tail code
    packet=$({
        value=$("$@")
        code=$?
        printf '\n%d\n%s' "$code" "$value"
    } 2>&1)
    initial_value=${packet##*$'\n'}
    tail=${packet%$'\n'*}
    code=${tail##*$'\n'}
    [[ "$code" == 0 && "${tail%$'\n'*}" == "" ]]
}
path_check() {
    local path=/ component
    local -a components
    IFS=/ read -ra components <<< "${store#/}"
    for component in "${components[@]}"; do
        path="${path%/}/$component"
        [[ -d "$path" && ! -L "$path" && -x "$path" ]] || return 1
    done
    [[ -r "$store" && -w "$store" ]] || return 1
}
regular() {
    [[ ! -L "$1" && -f "$1" ]] && [[ $(stat -c %h -- "$1") == 1 ]]
}
validate() {
    path_check || return 1
    [[ $(stat -c '%d:%i' -- "$store") == "$directory_identity" ]] || return 1
    local file name before after digest size seen=0 directory_before
    directory_before=$(stat -c '%d:%i:%y:%z' -- "$store") || return 1
    shopt -s nullglob dotglob
    for file in "$store"/*; do
        [[ "$file" != "$tmp" && "$file" != "$err" ]] || continue
        name=${file##*/}
        [[ ${inventory["$name"]+yes} ]] || return 1
        seen=$((seen + 1))
    done
    (( seen == ${#inventory[@]} )) || return 1
    [[ -z "$progress" || ( ! -e "$store/$progress" && ! -L "$store/$progress" ) ]] || return 1
    for name in "${!hashes[@]}"; do
        file="$store/$name"
        regular "$file" || return 1
        before=$(stat -c '%d:%i:%h:%s:%y:%z' -- "$file") || return 1
        size=$(stat -c %s -- "$file") || return 1
        (( size <= limit )) || return 1
        digest=$(dd if="$file" iflag=nofollow,nonblock \
            bs=262145 count=1 status=none | sha256sum) || return 1
        after=$(stat -c '%d:%i:%h:%s:%y:%z' -- "$file") || return 1
        [[ "$before" == "$after" && "${digest%% *}" == "${hashes["$name"]}" ]] || return 1
    done
    # Detect membership changes during hashing, not just before it.
    [[ $(stat -c '%d:%i:%y:%z' -- "$store") == "$directory_before" ]] || return 1
    path_check || return 1
    [[ ! -s "$err" ]]
}
work() {
    validate || return 1
    if [[ -n "$target" ]]; then
        if [[ ${inventory["$target"]+yes} ]]; then
            regular "$store/$target" || return 1
        else
            [[ ! -e "$store/$target" && ! -L "$store/$target" ]] || return 1
        fi
        tmp=$(mktemp -- "$store/.outbox-XXXXXXXX.tmp") || return 1
        regular "$tmp" || return 1
        dd of="$tmp" oflag=nofollow iflag=fullblock status=none bs=262145 count=1 || return 1
        (( $(stat -c %s -- "$tmp") <= limit )) || return 1
        validate || return 1
        regular "$tmp" || return 1
        mv -T -- "$tmp" "$store/$target" || return 1
        tmp=
    else
        for name in "${deletes[@]}"; do regular "$store/$name" || return 1; done
        for name in "${deletes[@]}"; do
            # Previous removals narrow inventory; every remaining body still
            # requires all surviving neighbor texts and membership to match.
            validate || return 1
            rm -- "$store/$name" || return 1
            removed+=("$name")
            unset 'inventory[$name]' 'hashes[$name]'
            [[ ! -s "$err" ]] || return 1
        done
    fi
    [[ ! -s "$err" ]]
}
status=error
if path_check && initial_command stat -c '%d:%i' -- "$store"; then
    directory_identity=$initial_value
    if initial_command mktemp -- "$store/.outbox-XXXXXXXX.tmp"; then
        err=$initial_value
        if regular "$err" 2>"$err" && [[ ! -s "$err" ]] && work 2>>"$err"; then
            status=ok
        fi
    else
        # A warning may accompany a successfully created owned temp path.
        err=$initial_value
    fi
fi
# Cleanup is part of the result: no success receipt precedes its exit/stderr.
cleanup_packet=$({ cleanup; printf '\n%d' "$?"; } 2>&1)
trap - EXIT
[[ "$cleanup_packet" == $'\n0' ]] || status=error
printf 'v1\0%s\0%d\0' "$status" "${#removed[@]}"
for name in "${removed[@]}"; do printf '%s\0' "$name"; done
"""


def _member_name(name: str) -> None:
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\0")):
        raise MutationExecutionError("invalid outbox member name")


def mutate_store(
    incus: Incus,
    container: str,
    kind: Kind,
    *,
    uid: int | None,
    expected: Mapping[str, str],
    new_manifest: tuple[str, str] | None,
    delete_names: tuple[str, ...],
    forbidden_progress: str | None,
    rejected_names: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Compare full inventory and exact UTF-8 bytes at the final mutation boundary."""
    if new_manifest is not None and delete_names:
        raise MutationExecutionError("replacement must not delete body files")
    if len(set(delete_names)) != len(delete_names) or set(expected) & set(rejected_names):
        raise MutationExecutionError("invalid mutation scope")
    for name in (*expected, *rejected_names, *delete_names):
        _member_name(name)
    target, text = new_manifest if new_manifest is not None else ("", "")
    if new_manifest is not None:
        _member_name(target)
        if target in rejected_names or len(text.encode("utf-8")) > FILE_LIMIT:
            raise MutationExecutionError("invalid replacement scope or size")
    if not set(delete_names) <= set(expected):
        raise MutationExecutionError("missing or rejected deletion input")
    if forbidden_progress is not None:
        _member_name(forbidden_progress)
    args = [
        store_directory(kind),
        str(FILE_LIMIT),
        target,
        forbidden_progress or "",
        str(len(expected)),
    ]
    for name, content in sorted(expected.items()):
        args.extend((name, hashlib.sha256(content.encode("utf-8")).hexdigest()))
    args.extend((str(len(rejected_names)), *rejected_names, *delete_names))
    try:
        result = incus.exec_with_input(
            container,
            ["bash", "-c", _MUTATE_SCRIPT, "bash", *args],
            text,
            uid=uid,
            timeout=MUTATION_TIMEOUT,
        )
    except IncusError as exc:
        raise MutationExecutionError(
            f"mutation transport failed; outcome may be incomplete: {exc}"
        ) from exc
    if len(result) > FILE_LIMIT:
        raise MutationExecutionError("mutation result overflow; outcome may be incomplete")
    fields = result.split("\0")
    if (
        len(fields) < 4
        or fields[:1] != ["v1"]
        or fields[1] not in ("ok", "error")
        or fields[-1] != ""
    ):
        raise MutationExecutionError("invalid mutation result; outcome may be incomplete")
    removed = tuple(fields[3:-1])
    if fields[2] != str(len(removed)) or removed != delete_names[: len(removed)]:
        raise MutationExecutionError("invalid mutation acknowledgement")
    if fields[1] != "ok" or removed != delete_names:
        raise MutationExecutionError("outbox mutation refused or failed; journal retained", removed)
    return removed


class PrManagement:
    """Instance-reentrant, host-side serialization of one container's PR store."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else state_dir() / "pr-outbox" / "locks"
        self._lock_state = local()

    @property
    def identity(self) -> ContainerIdentity | None:
        """The identity owned by this thread's outermost operation, if any."""
        return cast("ContainerIdentity | None", getattr(self._lock_state, "identity", None))

    @contextmanager
    def lock(self, identity: ContainerIdentity, *, timeout: float | None = None) -> Iterator[None]:
        """Serialize the PR store; opt-in finite seconds bound lock contention."""
        if not identity.full_name or not identity.created_at:
            raise OutboxExecutionError("cannot lock an empty container identity")
        if self.identity is not None and self.identity != identity:
            raise OutboxChanged("container changed during publication; refresh required")
        digest = hashlib.sha256(f"{identity.full_name}\0{identity.created_at}".encode()).hexdigest()
        path = self.root / f"{digest}.lock"
        held = getattr(self._lock_state, "paths", None)
        if held is None:
            held = set()
            self._lock_state.paths = held
        if path in held:
            yield
            return
        descriptor = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.chmod(path, 0o600)
            acquire_outbox_lock(descriptor, timeout)
        except (OSError, ValueError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            if isinstance(exc, TimeoutError):
                raise OutboxExecutionError(
                    "Timed out waiting for PR outbox lock; "
                    "refresh and retry after the current operation finishes"
                ) from exc
            if isinstance(exc, ValueError):
                raise OutboxExecutionError(str(exc)) from exc
            raise OutboxExecutionError("could not lock PR outbox") from exc
        held.add(path)
        self._lock_state.identity = identity
        try:
            yield
        finally:
            held.remove(path)
            self._lock_state.identity = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
