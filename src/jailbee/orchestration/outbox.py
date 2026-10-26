"""Container outbox reading, inbox writing, and parked delivery for the broker."""

from __future__ import annotations

import contextlib
import importlib.resources
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from jailbee.config.common import CONTAINER_USERNAME
from jailbee.incus import IncusError
from jailbee.orchestration.protocol import FINISH_SUFFIX, PendingFinish, inbox_file_name
from jailbee.orchestration.state import atomic_write_text, pending_dir
from jailbee.outbox_io import _validate_outbox_name, read_text_outbox

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from jailbee.incus import Incus
    from jailbee.orchestration.protocol import InboxItem, InboxMessage

_BASE = f"/home/{CONTAINER_USERNAME}/.jailbee/orchestration"
OUTBOX_DIR = f"{_BASE}/outbox"
INBOX_DIR = f"{_BASE}/inbox"
GRANT_FILE = f"{_BASE}/grant.json"
INBOX_HELPER = "/usr/local/bin/jailbee-inbox"

_INSTALL_HELPER = """set -e
dir=$(dirname "$1")
tmp=$(mktemp "$dir/.jailbee-inbox.XXXXXX")
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
if cmp -s "$tmp" "$1"; then exit 0; fi
chmod 0755 "$tmp"
mv -f "$tmp" "$1"
trap - EXIT
"""

MAX_MANIFESTS_PER_ROUND = 32
MAX_FILE_BYTES = 128 * 1024
# Body files archived per read, selected separately from the manifests so a
# backlog of manifests can never crowd out the bodies of the ones read. A
# round needs at most one body per manifest; the rest is headroom.
MAX_BODY_FILES = 2 * MAX_MANIFESTS_PER_ROUND
# Container-side selection: the first 32 manifests and up to 64 bodies by name.
OUTBOX_SELECT: tuple[tuple[str, int], ...] = (
    ("*.json", MAX_MANIFESTS_PER_ROUND),
    ("*.md", MAX_BODY_FILES),
)

# Values reach the script only as positional parameters ($1 directory, $2 file
# name, $3 mode), never as shell text.
_ATOMIC_WRITE = (
    'mkdir -p "$1" && rm -f "$1/.$2.tmp" && cat > "$1/.$2.tmp" '
    '&& chmod "$3" "$1/.$2.tmp" && mv -f "$1/.$2.tmp" "$1/$2"'
)


@dataclass(frozen=True)
class RawManifest:
    file_name: str
    text: str
    files: Mapping[str, str]


def helper_script() -> str:
    """Read the packaged helper shared by golden builds and the broker."""
    return importlib.resources.files("jailbee.provision").joinpath("jailbee-inbox").read_text()


def ensure_orchestration_env(incus: Incus, full: str, *, uid: int | None) -> None:
    """Create user-owned inbox/outbox dirs and atomically install the helper as root."""
    incus.exec(full, ["mkdir", "-p", INBOX_DIR, OUTBOX_DIR], uid=uid)
    incus.exec_with_input(full, ["sh", "-c", _INSTALL_HELPER, "sh", INBOX_HELPER], helper_script())


def read_outbox(incus: Incus, full: str, *, uid: int | None) -> list[RawManifest]:
    """Read `full`'s outbox; return up to 32 `*.json` manifests, sorted by name."""
    files = read_text_outbox(
        incus,
        full,
        OUTBOX_DIR,
        uid=uid,
        max_file_bytes=MAX_FILE_BYTES,
        select=OUTBOX_SELECT,
    )
    # Dot names are a writer's temp files (`.name.tmp` before the rename).
    files = {n: t for n, t in files.items() if not n.startswith(".")}
    names = sorted(n for n in files if n.endswith(".json"))[:MAX_MANIFESTS_PER_ROUND]
    return [RawManifest(file_name=n, text=files[n], files=files) for n in names]


def delete_outbox_files(incus: Incus, full: str, names: Sequence[str], *, uid: int | None) -> None:
    """Delete plain-named files from the outbox in one exec (names are argv only)."""
    for name in names:
        _validate_outbox_name(name)
    if not names:
        return
    incus.exec(full, ["rm", "-f", "--", *names], uid=uid, cwd=OUTBOX_DIR)


def _atomic_write(
    incus: Incus, full: str, directory: str, name: str, text: str, mode: str, *, uid: int | None
) -> None:
    incus.exec_with_input(
        full,
        ["sh", "-c", _ATOMIC_WRITE, "sh", directory, name, mode],
        text,
        uid=uid,
    )


def _item_json(item: InboxItem) -> str:
    return item.model_dump_json(by_alias=True) + "\n"


def write_inbox(
    incus: Incus, full: str, item: InboxItem, *, uid: int | None, now: datetime, seq: int = 0
) -> None:
    """Atomically write `item` into `full`'s inbox (temp file, then rename)."""
    name = inbox_file_name(item, now, seq=seq)
    _atomic_write(incus, full, INBOX_DIR, name, _item_json(item), "0644", uid=uid)


def deliver(
    incus: Incus,
    root: Path,
    recipient_full: str,
    item: InboxItem,
    *,
    running: bool,
    uid: int | None,
    now: datetime,
    seq: int = 0,
) -> Literal["delivered", "parked"]:
    """Write directly when the recipient runs; otherwise (or on failure) park it.

    An item is also parked behind any already-parked one, so order is kept.
    `seq` orders items emitted within one second (see `inbox_file_name`).
    """
    if running and not has_pending(root, recipient_full):
        try:
            write_inbox(incus, recipient_full, item, uid=uid, now=now, seq=seq)
        except IncusError:
            pass
        else:
            return "delivered"
    atomic_write_text(
        pending_dir(root, recipient_full) / inbox_file_name(item, now, seq=seq), _item_json(item)
    )
    return "parked"


def has_pending(root: Path, recipient_full: str) -> bool:
    """Whether any parked (non-dot) file waits for `recipient_full`."""
    directory = pending_dir(root, recipient_full)
    if not directory.is_dir():
        return False
    return any(p.is_file() and not p.name.startswith(".") for p in directory.iterdir())


def flush_pending(
    incus: Incus,
    root: Path,
    recipient_full: str,
    *,
    uid: int | None,
    finish: Callable[[PendingFinish], InboxMessage] | None = None,
) -> int:
    """Deliver parked files in name order; stop at the first failure."""
    directory = pending_dir(root, recipient_full)
    if not directory.is_dir():
        return 0
    delivered = 0
    for path in sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".bad")
    ):
        name = path.name
        text = path.read_text(encoding="utf-8")
        if name.endswith(FINISH_SUFFIX):
            try:
                item = PendingFinish.model_validate_json(text)
            except ValidationError:
                logging.getLogger(__name__).exception("invalid pending finish %s", path)
                path.rename(path.with_name(path.name + ".bad"))
                continue
            if finish is None:
                break
            text = _item_json(finish(item))
            name = name.removesuffix(FINISH_SUFFIX) + ".json"
        try:
            _atomic_write(
                incus,
                recipient_full,
                INBOX_DIR,
                name,
                text,
                "0644",
                uid=uid,
            )
        except IncusError:
            break
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        delivered += 1
    return delivered


def write_grant_json(incus: Incus, full: str, grant_json: str, *, uid: int | None) -> None:
    """Atomically write the read-only (0444) grant copy into the container."""
    directory, _, name = GRANT_FILE.rpartition("/")
    _atomic_write(incus, full, directory, name, grant_json, "0444", uid=uid)
