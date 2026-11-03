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

WAKE_TOKENS = (
    "note",
    "question",
    "done",
    "blocked",
    "idle",
    "queued",
    "started",
    "refused",
    "failed",
)


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


def _records(base: Path) -> list[tuple[Path, dict[str, Any], int]]:
    records: list[tuple[Path, dict[str, Any], int]] = []
    for directory in (base / "inbox", base / "inbox/read"):
        for path in directory.glob("*.json"):
            if path.name.startswith("."):
                continue
            data = _load(path)
            if data is None:
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


def last_from_child(base: Path) -> dict[str, float]:
    """Newest mtime of each sender's `message` files in `inbox/` and `inbox/read/`."""
    return _newest_from_child(_records(base))


def select_wakeup(base: Path, wake: frozenset[str] | None, quiet: int, now: float) -> str | None:
    snapshot = _load(base / "children.json")
    children: dict[str, float] = {}
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
            if child["running"] and not done:
                running.add(child["name"])
    except (AssertionError, KeyError, TypeError):
        if quiet:
            raise InboxUsage(
                "--quiet-after needs a fresh broker children.json (at most 15 minutes old)"
            ) from None
        children = {}
        running.clear()

    records = _records(base)

    first: dict[str, Path] = {}
    latest = {name: children[name] for name in running}
    newest = _newest_from_child(records)
    for path, data, stamp in sorted(records, key=lambda r: (r[2], r[0].name)):
        name = data.get("from")
        if data.get("type") == "message" and isinstance(name, str) and name in children:
            if stamp / 1e9 >= children[name]:
                first.setdefault(name, path)
    for name in latest:
        latest[name] = max(latest[name], newest.get(name, 0.0))

    for path, data, _ in sorted(records, key=lambda r: r[0].name):
        if path.parent != base / "inbox":
            continue
        is_status = data.get("type") == "status"
        if is_status and _reserved(base, data.get("ref")):
            continue
        token = data.get("state") if is_status else data.get("kind")
        name = data.get("from")
        is_first = isinstance(name, str) and first.get(name) == path
        if wake is None or (isinstance(token, str) and token in wake) or is_first:
            return str(path)
    if quiet:
        for name in sorted(latest, key=lambda n: (latest[n], n)):
            if now - latest[name] >= quiet:
                clean = "".join("?" if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in name)
                return "quiet-child " + clean
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
    if data is None or ident is None or cmd_read(settings, [ident]):
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
    timeout: int | None,
    wake: frozenset[str] | None,
    quiet: int,
    status_ref: str | None = None,
    watcher_factory: Callable[..., InboxWatcher] = InboxWatcher,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, str | None]:
    deadline = None if timeout is None else clock() + timeout
    watcher = watcher_factory(settings.inbox, poll=settings.poll)
    waited = False
    try:
        while True:
            if status_ref is not None:
                path = find_status(settings.inbox, status_ref)
                first = str(path) if path is not None else None
            else:
                first = select_wakeup(settings.base, wake, quiet, time.time())
            if first is not None:
                if waited and status_ref is None:
                    settle(settings, deadline if deadline is not None else clock() + 5, clock=clock)
                    first = select_wakeup(settings.base, wake, quiet, time.time())
                    if first is None:
                        continue
                return 0, first
            remaining = 10.0 if deadline is None else deadline - clock()
            if remaining <= 0:
                return 1, None
            watcher.wait(min(remaining, 1 if quiet else 10))
            waited = True
    finally:
        watcher.close()
