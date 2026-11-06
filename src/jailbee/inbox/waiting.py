"""Read-only wakeup selection and inbox burst settling."""

import fcntl
import json
import math
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jailbee.inbox.cli import Settings, unread_files
from jailbee.inbox.errors import InboxUsage
from jailbee.inbox.watch import InboxWatcher

MESSAGE_KINDS = ("note", "question", "done", "blocked", "idle")
STATUS_STATES = ("queued", "started", "done", "refused", "failed")
# A bare token matches a message kind or a status state; `msg:<kind>` and
# `status:<state>` match only their own record type.
WAKE_TOKENS = tuple(dict.fromkeys((*MESSAGE_KINDS, *STATUS_STATES)))
MSG_PREFIX = "msg:"
STATUS_PREFIX = "status:"


def valid_wake_token(token: str) -> bool:
    if token.startswith(MSG_PREFIX):
        return token[len(MSG_PREFIX) :] in MESSAGE_KINDS
    if token.startswith(STATUS_PREFIX):
        return token[len(STATUS_PREFIX) :] in STATUS_STATES
    return token in WAKE_TOKENS


def _load(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _reserved(base: Path, ref: Any) -> bool:
    if not isinstance(ref, str) or not ref or "/" in ref or ref in (".", ".."):
        return False
    try:
        fd = os.open(
            base / "status-waits" / (ref + ".lock"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def _warn_once(path: Path, warned: set[Path]) -> None:
    from jailbee.inbox.cli import _warning

    if path not in warned:
        warned.add(path)
        _warning(path)


def _records(base: Path, warned: set[Path]) -> list[tuple[Path, dict[str, Any], int]]:
    records: list[tuple[Path, dict[str, Any], int]] = []

    for directory in (base / "inbox", base / "inbox/read"):
        for path in directory.glob("*.json"):
            if path.name.startswith("."):
                continue
            data = _load(path)
            if data is None:
                if directory == base / "inbox":
                    _warn_once(path, warned)
                continue
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                continue
            records.append((path, data, stamp))
    return records


def _newest_from_child(records: list[tuple[Path, dict[str, Any], int]]) -> dict[str, float]:
    newest: dict[str, float] = {}
    for _, data, stamp in records:
        name = data.get("from")
        if data.get("type") == "message" and isinstance(name, str):
            newest[name] = max(newest.get(name, 0.0), stamp / 1e9)
    return newest


def last_from_child(base: Path, warned: set[Path] | None = None) -> dict[str, float]:
    """Newest mtime of each sender's `message` files in `inbox/` and `inbox/read/`."""
    return _newest_from_child(_records(base, warned if warned is not None else set()))


def _read_digest_stamp(base: Path, now: float) -> float | None:
    """The last digest time, or None when missing, unreadable or in the future."""
    path = base / "digest-at"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = float(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return value if math.isfinite(value) and value <= now + 5 else None


def write_digest_stamp(base: Path, now: float) -> None:
    """Atomically record `now` as the last digest time (temp file + rename)."""
    tmp = base / f".digest-at.{os.getpid()}.tmp"
    try:
        tmp.write_text(repr(float(now)))
        os.replace(tmp, base / "digest-at")
    finally:
        tmp.unlink(missing_ok=True)


def _activity_at(child: dict[str, Any], now: float) -> float:
    """A child's published `status.activity_at`, or 0.0 when absent or unusable.

    Unusable is anything but a finite int/float no later than now + 5 s: a
    future value would otherwise silence the watchdog for that long, so it is
    ignored (not clamped). An old broker publishes no status at all.
    """
    status = child.get("status")
    value: Any = status.get("activity_at") if isinstance(status, dict) else None
    if not _number(value) or value > now + 5:
        return 0.0
    return float(value)


def select_wakeup(
    base: Path,
    wake: frozenset[str] | None,
    quiet: int,
    now: float,
    digest: int = 0,
    warned: set[Path] | None = None,
) -> str | None:
    if warned is None:
        warned = set()
    snapshot = _load(base / "children.json")
    children: dict[str, float] = {}
    activity: dict[str, float] = {}
    running: set[str] = set()
    try:
        assert snapshot and snapshot["version"] == 1
        assert _number(snapshot["updated_at"]) and -5 <= now - snapshot["updated_at"] <= 900
        assert isinstance(snapshot["children"], list)
        for child in snapshot["children"]:
            assert isinstance(child["name"], str) and isinstance(child["running"], bool)
            # `done` is absent from pre-#63 brokers, whose `running` already
            # excluded finished children.
            done = child.get("done", False)
            assert isinstance(done, bool)
            assert _number(child["started_at"]) and child["started_at"] <= now + 5
            children[child["name"]] = child["started_at"]
            activity[child["name"]] = _activity_at(child, now)
            if child["running"] and not done:
                running.add(child["name"])
    except (AssertionError, KeyError, TypeError):
        if quiet or digest:
            names = " and ".join(
                flag for flag, on in (("--quiet-after", quiet), ("--digest", digest)) if on
            )
            raise InboxUsage(
                f"{names} {'need' if quiet and digest else 'needs'} a fresh broker "
                "children.json (at most 15 minutes old)"
            ) from None
        children = {}
        running.clear()

    from jailbee.inbox.cli import message_id

    records = _records(base, warned)

    first: dict[str, Path] = {}
    latest = {name: children[name] for name in running}
    newest = _newest_from_child(records)
    for path, data, stamp in sorted(records, key=lambda r: (r[2], r[0].name)):
        name = data.get("from")
        if data.get("type") == "message" and isinstance(name, str) and name in children:
            if stamp / 1e9 >= children[name]:
                first.setdefault(name, path)
    for name in latest:
        latest[name] = max(latest[name], newest.get(name, 0.0), activity.get(name, 0.0))

    for path, data, _ in sorted(records, key=lambda r: r[0].name):
        if path.parent != base / "inbox":
            continue
        if message_id(path) is None:
            _warn_once(path, warned)
            continue
        is_status = data.get("type") == "status"
        if is_status and _reserved(base, data.get("ref")):
            continue
        token = data.get("state") if is_status else data.get("kind")
        name = data.get("from")
        is_first = isinstance(name, str) and first.get(name) == path
        prefix = STATUS_PREFIX if is_status else MSG_PREFIX
        matches = (
            wake is not None
            and isinstance(token, str)
            and (token in wake or prefix + token in wake)
        )
        if wake is None or matches or is_first:
            return str(path)
    if quiet:
        for name in sorted(latest, key=lambda n: (latest[n], n)):
            if now - latest[name] >= quiet:
                clean = "".join("?" if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in name)
                return "quiet-child " + clean
    if digest:
        last = _read_digest_stamp(base, now)
        if last is None:
            try:
                write_digest_stamp(base, now)
            except OSError:
                pass
        elif running and now - last >= digest:
            return "digest"
    return None


def find_status(inbox: Path, ref: str) -> Path | None:
    for path in unread_files(inbox):
        data = _load(path)
        if data is not None and data.get("type") == "status" and data.get("ref") == ref:
            return path
    return None


def wait_for_status(settings: Settings, ref: str, timeout: int | None) -> int:
    from jailbee.inbox.cli import cmd_read, message_id

    rc, selected = run_wait(settings, timeout=timeout, wake=None, quiet=0, status_ref=ref)
    if rc or selected is None:
        return 3
    path = Path(selected)
    data = _load(path)
    ident = message_id(path)
    if data is None or ident is None or cmd_read(settings, ident):
        return 1
    return 0 if data.get("state") in ("done", "queued", "started") else 1


def settle(
    settings: Settings,
    deadline: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    interval = min(settings.settle, 5.0)
    if interval <= 0:
        return
    current = clock()
    cap = min(current + 5.0, deadline)
    quiet = current + interval
    previous = len(unread_files(settings.inbox))
    while True:
        current = clock()
        if current >= cap or current >= quiet:
            break
        sleep(min(interval, 0.2, cap - current, quiet - current))
        count = len(unread_files(settings.inbox))
        if count > previous:
            quiet = clock() + interval
        previous = count


def run_wait(
    settings: Settings,
    *,
    timeout: float | None,
    wake: frozenset[str] | None,
    quiet: int,
    status_ref: str | None = None,
    digest: int = 0,
    warned: set[Path] | None = None,
    watcher_factory: Callable[..., InboxWatcher] = InboxWatcher,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, str | None]:
    deadline = None if timeout is None else clock() + timeout
    watcher = watcher_factory(settings.inbox, poll=settings.poll)
    waited = False
    if warned is None:
        warned = set()
    try:
        while True:
            if status_ref is not None:
                path = find_status(settings.inbox, status_ref)
                first = str(path) if path is not None else None
            else:
                first = select_wakeup(settings.base, wake, quiet, time.time(), digest, warned)
            if first is not None:
                if waited and status_ref is None:
                    settle(settings, deadline if deadline is not None else clock() + 5, clock=clock)
                    first = select_wakeup(settings.base, wake, quiet, time.time(), digest, warned)
                    if first is None:
                        continue
                return 0, first
            remaining = 10.0 if deadline is None else deadline - clock()
            if remaining <= 0:
                return 1, None
            watcher.wait(min(remaining, 1 if quiet or digest else 10))
            waited = True
    finally:
        watcher.close()
