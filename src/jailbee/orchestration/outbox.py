"""Container outbox reading, inbox writing, and parked delivery for the broker."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.config.common import CONTAINER_USERNAME
from jailbee.incus import IncusError
from jailbee.orchestration.protocol import inbox_file_name
from jailbee.orchestration.state import atomic_write_text, pending_dir
from jailbee.outbox_io import _validate_outbox_name, read_text_outbox

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from jailbee.incus import Incus
    from jailbee.orchestration.protocol import InboxItem

_BASE = f"/home/{CONTAINER_USERNAME}/.jailbee/orchestration"
OUTBOX_DIR = f"{_BASE}/outbox"
INBOX_DIR = f"{_BASE}/inbox"
GRANT_FILE = f"{_BASE}/grant.json"

MAX_MANIFESTS_PER_ROUND = 32
MAX_FILE_BYTES = 128 * 1024

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


def read_outbox(incus: Incus, full: str, *, uid: int | None) -> list[RawManifest]:
    """Read `full`'s outbox; return up to 32 `*.json` manifests, sorted by name."""
    files = read_text_outbox(incus, full, OUTBOX_DIR, uid=uid, max_file_bytes=MAX_FILE_BYTES)
    names = sorted(n for n in files if n.endswith(".json"))[:MAX_MANIFESTS_PER_ROUND]
    return [RawManifest(file_name=n, text=files[n], files=files) for n in names]


def delete_outbox_files(
    incus: Incus, full: str, names: Sequence[str], *, uid: int | None
) -> None:
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
    incus: Incus, full: str, item: InboxItem, *, uid: int | None, now: datetime
) -> None:
    """Atomically write `item` into `full`'s inbox (temp file, then rename)."""
    _atomic_write(
        incus, full, INBOX_DIR, inbox_file_name(item, now), _item_json(item), "0644", uid=uid
    )


def deliver(
    incus: Incus,
    root: Path,
    recipient_full: str,
    item: InboxItem,
    *,
    running: bool,
    uid: int | None,
    now: datetime,
) -> None:
    """Write directly when the recipient runs; otherwise (or on failure) park it."""
    if running:
        try:
            write_inbox(incus, recipient_full, item, uid=uid, now=now)
        except IncusError:
            pass
        else:
            return
    atomic_write_text(pending_dir(root, recipient_full) / inbox_file_name(item, now), _item_json(item))


def flush_pending(incus: Incus, root: Path, recipient_full: str, *, uid: int | None) -> int:
    """Deliver parked files in name order; stop at the first failure."""
    directory = pending_dir(root, recipient_full)
    if not directory.is_dir():
        return 0
    delivered = 0
    for path in sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")):
        try:
            _atomic_write(
                incus,
                recipient_full,
                INBOX_DIR,
                path.name,
                path.read_text(encoding="utf-8"),
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
