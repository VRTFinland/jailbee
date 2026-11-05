"""Capture and consume container-local agent sessions across restarts."""

import json
import os
import pwd
import re
import stat
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from jailbee.services.local_proc import LocalProc

SNAPSHOT_RELPATH = ".local/state/jailbee/agent-sessions.json"
LAUNCHERS_RELPATH = ".local/state/jailbee/launchers"
SESSIONS_RELPATH = ".claude/sessions"
_LIMIT = 65536
_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")


def _read(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            result = b""
            while len(result) <= _LIMIT:
                chunk = os.read(fd, _LIMIT + 1 - len(result))
                if not chunk:
                    break
                result += chunk
            return result if len(result) <= _LIMIT else None
        finally:
            os.close(fd)
    except OSError:
        return None


def _json(path: Path) -> object:
    raw = _read(path)
    if raw is not None:
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            pass
    return None


def _start(proc: Path, pid: str) -> str | None:
    raw = _read(proc / pid / "stat")
    if raw is None:
        return None
    try:
        return raw.decode().rsplit(")", 1)[1].split()[19]
    except (UnicodeError, IndexError):
        return None


def _argv(path: Path) -> list[str]:
    raw = _read(path)
    if raw is None:
        return []
    try:
        return (raw[:-1] if raw.endswith(b"\0") else raw).decode().split("\0") if raw else []
    except UnicodeError:
        return []


def _valid_launcher(args: list[str]) -> bool:
    return bool(args and Path(args[0]).name in {"claude", "claude-jb"})


def _launcher(home: Path, pid: str, start: str, fallback: list[str]) -> list[str]:
    args = _argv(home / LAUNCHERS_RELPATH / pid)
    if args and args[0] == start and _valid_launcher(args[1:]):
        return args[1:]
    return fallback if _valid_launcher(fallback) else ["claude"]


def _entries(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except OSError:
        return []


def _record(path: Path) -> dict[str, object] | None:
    data = _json(path)
    if not isinstance(data, dict):
        return None
    pid, start = data.get("pid"), data.get("procStart")
    if isinstance(pid, bool) or not isinstance(pid, (int, str)):
        return None
    pid_text = str(pid)
    if not pid_text.isascii() or not pid_text.isdigit():
        return None
    pid_text = pid_text.lstrip("0")
    if not pid_text or path.name != f"{pid_text}.json":
        return None
    data["pid"] = pid_text
    if isinstance(start, bool) or not isinstance(start, (int, str)):
        return None
    if not str(start).isascii() or not str(start).isdigit():
        return None
    if not isinstance(data.get("kind"), str):
        return None
    sid, cwd = data.get("sessionId"), data.get("cwd")
    if not isinstance(sid, str) or not _UUID.fullmatch(sid) or not isinstance(cwd, str) or not cwd:
        return None
    return data


def _item(data: dict[str, object], launcher: list[str], window: str | None) -> dict[str, object]:
    return {
        "agent": "claude",
        "session_id": data["sessionId"],
        "cwd": data["cwd"],
        "launcher": launcher,
        "window": window,
    }


def _write_snapshot(path: Path, payload: bytes) -> None:
    if len(payload) + 1 > _LIMIT:
        raise ValueError("agent snapshot exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".agent-sessions-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def capture(home: Path, *, proc: Path = Path("/proc"), windows: Mapping[str, str]) -> int:
    sessions: list[dict[str, object]] = []
    for path in _entries(home / SESSIONS_RELPATH):
        data = _record(path)
        if data is None or data.get("kind") != "interactive":
            continue
        pid, start = str(data["pid"]), str(data["procStart"])
        if _start(proc, pid) != start:
            continue
        tmux = data.get("tmux")
        match = re.fullmatch(r"autostart:(@\d+)\.%\d+", tmux) if isinstance(tmux, str) else None
        window = windows.get(match[1]) if match else None
        sessions.append(
            _item(data, _launcher(home, pid, start, _argv(proc / pid / "cmdline")), window)
        )
    if sessions:
        payload = json.dumps({"version": 1, "source": "capture", "sessions": sessions}).encode(
            "utf-8"
        )
        _write_snapshot(home / SNAPSHOT_RELPATH, payload)
    return len(sessions)


def _unlink_stale(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        print(f"agent snapshot: cannot remove {path}: {exc}", file=sys.stderr)
        return False


def boot(home: Path, *, proc: Path = Path("/proc")) -> dict[str, object]:
    path = home / SNAPSHOT_RELPATH
    has_snapshot = (path.exists() or path.is_symlink()) and not path.is_dir()
    result: dict[str, object] = {
        "version": 1,
        "source": "capture" if has_snapshot else "stale",
        "sessions": [],
    }
    if has_snapshot:
        data = _json(path)
        if (
            isinstance(data, dict)
            and data.get("version") == 1
            and data.get("source") in ("capture", "stale")
            and isinstance(data.get("sessions"), list)
        ):
            result = {"version": 1, "source": data["source"], "sessions": data["sessions"]}
    stale: list[dict[str, object]] = []
    unconsumed: set[str] = set()
    blocked_pids: set[str] = set()
    live: set[str] = set()
    directory = home / SESSIONS_RELPATH
    for entry in _entries(directory):
        if not re.fullmatch(r"[0-9]+\.json", entry.name):
            continue
        pid = entry.stem
        data = _record(entry)
        start = _start(proc, pid)
        if data is not None and start == str(data["procStart"]):
            live.add(pid)
            continue
        item = None
        if data is not None and data.get("kind") == "interactive":
            item = _item(data, _launcher(home, pid, str(data["procStart"]), ["claude"]), None)
        if not has_snapshot and item is not None:
            candidate = {**result, "sessions": [*stale, item]}
            if len(json.dumps(candidate).encode("utf-8")) + 1 > _LIMIT:
                single = {**result, "sessions": [item]}
                if len(json.dumps(single).encode("utf-8")) + 1 <= _LIMIT:
                    blocked_pids.add(pid)
                    continue
                print(f"agent snapshot: dropping oversized session {pid}", file=sys.stderr)
                item = None
        if _unlink_stale(entry):
            if not has_snapshot and item is not None:
                stale.append(item)
        else:
            blocked_pids.add(pid)
            if item is not None:
                unconsumed.add(str(item["session_id"]))
    for entry in _entries(directory):
        match = re.fullmatch(r"([0-9]+)\..*\.key", entry.name)
        if match and match[1] not in live and match[1] not in blocked_pids:
            _unlink_stale(entry)
    for entry in _entries(home / LAUNCHERS_RELPATH):
        if not entry.name.isascii() or not entry.name.isdigit() or entry.name in blocked_pids:
            continue
        args = _argv(entry)
        if not args or _start(proc, entry.name) != args[0]:
            _unlink_stale(entry)
    if not has_snapshot:
        result["sessions"] = stale
    else:
        entries = result["sessions"]
        assert isinstance(entries, list)
        blocked = [
            item
            for item in entries
            if isinstance(item, dict)
            and isinstance(item.get("session_id"), str)
            and item["session_id"] in unconsumed
        ]
        emitted = [item for item in entries if item not in blocked]
        while (
            len(json.dumps({**result, "sessions": emitted}, separators=(",", ":")).encode()) + 1
            > _LIMIT
        ):
            item = emitted.pop()
            single = {**result, "sessions": [item]}
            if len(json.dumps(single, separators=(",", ":")).encode()) + 1 > _LIMIT:
                print("agent snapshot: dropping oversized snapshot session", file=sys.stderr)
            else:
                blocked.insert(0, item)
        if blocked:
            remaining = {**result, "sessions": blocked}
            try:
                _write_snapshot(path, json.dumps(remaining, separators=(",", ":")).encode())
            except (OSError, ValueError) as exc:
                print(f"agent snapshot: cannot retain snapshot: {exc}", file=sys.stderr)
                emitted = []
        elif not _unlink_stale(path):
            emitted = []
        result["sessions"] = emitted
    return result


def window_names() -> dict[str, str]:
    try:
        result = LocalProc().tmux(
            ["list-windows", "-a", "-F", "#{session_name}\t#{window_id}\t#{window_name}"]
        )
        if result.returncode:
            return {}
        rows = (line.split("\t", 2) for line in result.stdout.splitlines())
        return {row[1]: row[2] for row in rows if len(row) == 3 and row[0] == "autostart"}
    except (OSError, ValueError):
        return {}


def main(argv: list[str]) -> int:
    if argv == ["--capture"]:
        try:
            capture(Path(pwd.getpwuid(os.getuid()).pw_dir), windows=window_names())
        except Exception as exc:
            print(f"agent snapshot: {exc}", file=sys.stderr)
        return 0
    if argv == ["--boot"]:
        print(json.dumps(boot(Path(pwd.getpwuid(os.getuid()).pw_dir)), separators=(",", ":")))
        return 0
    return 2
