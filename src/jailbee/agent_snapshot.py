"""Capture and consume container-local agent sessions across restarts."""

import fcntl
import json
import os
import pwd
import re
import stat
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from subprocess import TimeoutExpired

SNAPSHOT_RELPATH = ".local/state/jailbee/agent-sessions.json"
BOOT_RELPATH = ".local/state/jailbee/boot-agent-sessions.json"
LAUNCHERS_RELPATH = ".local/state/jailbee/launchers"
SESSIONS_RELPATH = ".claude/sessions"
REGISTRY_RELPATH = ".local/state/jailbee/live-agent-sessions.json"
PROC = Path("/proc")
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


def _boot_id(proc: Path) -> str:
    kernel = (_read(proc / "sys/kernel/random/boot_id") or b"").decode(errors="replace").strip()
    return f"{kernel}:{_start(proc, '1') or ''}"


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
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _hook_process(home: Path, proc: Path) -> tuple[str, str, list[str], str | None]:
    pid = str(os.getppid())
    for _ in range(32):
        start = _start(proc, pid)
        args = _argv(proc / pid / "cmdline")
        if (
            len(args) > 1
            and Path(args[0]).name in {"node", "nodejs", "bun"}
            and (
                Path(args[1]).name == "claude"
                or args[1].endswith("/@anthropic-ai/claude-code/cli.js")
            )
        ):
            args = ["claude", *args[2:]]
        if start is None:
            break
        launcher = _launcher(home, pid, start, args)
        data = _record(home / SESSIONS_RELPATH / f"{pid}.json")
        matching_record = data is not None and str(data["procStart"]) == start
        if _valid_launcher(args) or launcher != ["claude"] or matching_record:
            tmux = data.get("tmux") if data and matching_record else None
            return pid, start, launcher, tmux if isinstance(tmux, str) else None
        raw = _read(proc / pid / "stat")
        if raw is None:
            break
        try:
            parent = raw.decode().rsplit(")", 1)[1].split()[1]
        except (UnicodeError, IndexError):
            break
        if parent == pid or parent == "0":
            break
        pid = parent
    return "", "", ["claude"], None


def _lock(fd: int) -> None:
    # Serialize ordinary concurrent hooks, but never wait on a stuck owner.
    deadline = time.monotonic() + 5.0
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _merge_pending(home: Path, proc: Path, source: Path) -> None:
    """Durably transfer a bounded batch before pruning its source."""
    path = home / BOOT_RELPATH
    saved = _json(path)
    if not isinstance(saved, dict) or not isinstance(saved.get("sessions"), list):
        saved = {"version": 1, "source": "capture", "sessions": []}
    fresh = _json(source)
    if not isinstance(fresh, dict) or not isinstance(fresh.get("sessions"), list):
        return
    rows = saved["sessions"]
    assert isinstance(rows, list)
    remaining = []
    saved["boot_id"] = _boot_id(proc)
    for row in fresh["sessions"]:
        if (
            not isinstance(row, dict)
            or row.get("ended")
            or (
                source == home / REGISTRY_RELPATH
                and row.get("pid")
                and _start(proc, str(row["pid"])) == row.get("procStart")
            )
        ):
            remaining.append(row)
            continue
        if any(
            isinstance(old, dict) and old.get("session_id") == row.get("session_id") for old in rows
        ):
            continue
        candidate = {**saved, "sessions": [*rows, row]}
        if len(json.dumps(candidate, separators=(",", ":")).encode()) + 1 > _LIMIT:
            remaining.append(row)
        else:
            rows.append(row)
    _save_pending(path, saved)
    if source == home / SNAPSHOT_RELPATH and not remaining:
        source.unlink(missing_ok=True)
    else:
        _write_snapshot(
            source,
            json.dumps(
                {**fresh, "sessions": remaining, "boot_id": _boot_id(proc)}, separators=(",", ":")
            ).encode(),
        )


def register_session(
    home: Path,
    data: Mapping[str, object],
    *,
    ended: bool = False,
    windows: Mapping[str, str] | None = None,
) -> None:
    """Persist a hook session without relying on Claude's transient PID files."""
    sid, cwd = data.get("session_id"), data.get("cwd")
    if not isinstance(sid, str) or not _UUID.fullmatch(sid):
        return
    if not ended and (
        not isinstance(cwd, str) or not os.path.isabs(cwd) or any(c in cwd for c in "\0\n\r")
    ):
        return
    pid, start, launcher, tmux = _hook_process(home, PROC)
    if not ended and any(arg in {"-p", "--print"} for arg in launcher[1:]):
        return
    record = _record(home / SESSIONS_RELPATH / f"{pid}.json") if pid else None
    if (
        not ended
        and record
        and str(record["procStart"]) == start
        and record["kind"] != "interactive"
    ):
        return
    match = re.fullmatch(r"autostart(?:-\d+)?:(@\d+)\.%\d+", tmux) if tmux else None
    window = (windows or {}).get(match[1]) if match else None
    path = home / REGISTRY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _lock(fd)
        registry = _json(path)
        rows = registry.get("sessions", []) if isinstance(registry, dict) else []
        if not isinstance(rows, list):
            rows = []
        boot_id = _boot_id(PROC)
        previous_boot = isinstance(registry, dict) and registry.get("boot_id", ":") != boot_id
        if previous_boot:
            _merge_pending(home, PROC, path)
            registry = _json(path)
            rows = registry.get("sessions", []) if isinstance(registry, dict) else []
            if not isinstance(rows, list):
                rows = []
        pending = _json(home / BOOT_RELPATH)
        frozen = _json(home / SNAPSHOT_RELPATH)
        preserve_frozen = (
            isinstance(pending, dict) and isinstance(pending.get("sessions"), list)
        ) or (isinstance(frozen, dict) and frozen.get("boot_id") == boot_id)
        rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("session_id") != sid
            and (
                (not row.get("pid") and not row.get("ended"))
                or (
                    isinstance(row.get("pid"), str)
                    and bool(row["pid"])
                    and _start(PROC, row["pid"]) == row.get("procStart")
                )
            )
            and (ended or not pid or (row.get("pid"), row.get("procStart")) != (pid, start))
        ]
        rows.append(
            {
                "agent": "claude",
                "session_id": sid,
                "cwd": cwd,
                "launcher": launcher,
                "window": window,
                "pid": pid,
                "procStart": start,
                "ended": ended,
            }
        )
        _write_snapshot(
            path,
            json.dumps(
                {"version": 1, "source": "capture", "sessions": rows, "boot_id": boot_id}
            ).encode(),
        )
        frozen_path = home / SNAPSHOT_RELPATH
        if not ended and not preserve_frozen:
            frozen_path.unlink(missing_ok=True)
        else:
            frozen = _json(frozen_path)
            if isinstance(frozen, dict) and isinstance(frozen.get("sessions"), list):
                frozen["sessions"] = [
                    row
                    for row in frozen["sessions"]
                    if not isinstance(row, dict) or row.get("session_id") != sid
                ]
                _write_snapshot(frozen_path, json.dumps(frozen).encode())
        _acknowledge(home, {sid})
    finally:
        os.close(fd)


def capture(home: Path, *, proc: Path = Path("/proc"), windows: Mapping[str, str]) -> int:
    path = home / REGISTRY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _lock(fd)
        frozen = _json(home / SNAPSHOT_RELPATH)
        if isinstance(frozen, dict) and isinstance(frozen.get("sessions"), list):
            return len(frozen["sessions"])
        return _capture(home, proc=proc, windows=windows)
    finally:
        os.close(fd)


def _capture(home: Path, *, proc: Path, windows: Mapping[str, str]) -> int:
    registry = _json(home / REGISTRY_RELPATH)
    rows = registry.get("sessions", []) if isinstance(registry, dict) else []
    registered = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    sessions: list[dict[str, object]] = [
        row
        for row in registered
        if not row.get("ended")
        and (
            not row.get("pid")
            or (isinstance(row["pid"], str) and _start(proc, row["pid"]) == row.get("procStart"))
        )
    ]
    for path in _entries(home / SESSIONS_RELPATH):
        data = _record(path)
        if data is None or data.get("kind") != "interactive":
            continue
        pid, start = str(data["pid"]), str(data["procStart"])
        if _start(proc, pid) != start:
            continue
        if any(
            row.get("session_id") == data["sessionId"]
            or (row.get("pid"), row.get("procStart")) == (pid, start)
            for row in registered
        ):
            continue
        tmux = data.get("tmux")
        # An attached client runs in a session grouped with autostart, which
        # tmux names `autostart-<n>`; window ids are shared across the group.
        match = (
            re.fullmatch(r"autostart(?:-\d+)?:(@\d+)\.%\d+", tmux)
            if isinstance(tmux, str)
            else None
        )
        window = windows.get(match[1]) if match else None
        sessions.append(
            _item(data, _launcher(home, pid, start, _argv(proc / pid / "cmdline")), window)
        )
    if sessions or isinstance(registry, dict):
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


def boot(home: Path, *, proc: Path = Path("/proc"), retain: bool = False) -> dict[str, object]:
    registry = home / REGISTRY_RELPATH
    pending = home / BOOT_RELPATH
    registry.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(registry.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            _lock(fd)
            saved = _json(pending)
            if isinstance(saved, dict) and isinstance(saved.get("sessions"), list):
                new_boot = saved.get("boot_id") != _boot_id(proc)
                _merge_pending(home, proc, home / SNAPSHOT_RELPATH)
                if new_boot:
                    _merge_pending(home, proc, registry)
                return _boot(home, proc=proc, path=pending, retain=retain)
            frozen = home / SNAPSHOT_RELPATH
            if _read(registry) is None:
                return _boot(home, proc=proc, path=frozen, retain=retain)
            if _read(frozen) is not None:
                result = _boot(home, proc=proc, path=frozen, retain=retain)
                registered = _json(registry)
                emitted = result["sessions"]
                assert isinstance(emitted, list)
                ids = {row.get("session_id") for row in emitted if isinstance(row, dict)}
                if isinstance(registered, dict) and isinstance(registered.get("sessions"), list):
                    registered["sessions"] = [
                        row
                        for row in registered["sessions"]
                        if not isinstance(row, dict) or row.get("session_id") not in ids
                    ]
                    _write_snapshot(
                        registry, json.dumps(registered, separators=(",", ":")).encode()
                    )
                return result
            return _boot(home, proc=proc, path=registry, registry=True, retain=retain)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return {"version": 1, "source": "capture", "sessions": []}


def acknowledge(home: Path, session_ids: list[str]) -> None:
    """Remove only launched boot entries, never newly registered live sessions."""
    registry = home / REGISTRY_RELPATH
    registry.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(registry.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _lock(fd)
        _acknowledge(home, set(session_ids))
    finally:
        os.close(fd)


def _save_pending(path: Path, saved: dict[str, object]) -> None:
    if saved["sessions"]:
        _write_snapshot(path, json.dumps(saved, separators=(",", ":")).encode())
    else:
        path.unlink(missing_ok=True)


def _acknowledge(home: Path, session_ids: set[str]) -> None:
    path = home / BOOT_RELPATH
    saved = _json(path)
    if isinstance(saved, dict) and isinstance(saved.get("sessions"), list):
        saved["sessions"] = [
            row
            for row in saved["sessions"]
            if not isinstance(row, dict) or row.get("session_id") not in session_ids
        ]
        _save_pending(path, saved)


def _boot(
    home: Path, *, proc: Path, path: Path, registry: bool = False, retain: bool = False
) -> dict[str, object]:
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
    registered = _json(home / REGISTRY_RELPATH)
    live_ids: set[str] = set()
    if (
        path != home / SNAPSHOT_RELPATH
        and isinstance(registered, dict)
        and registered.get("boot_id") == _boot_id(proc)
        and isinstance(registered.get("sessions"), list)
    ):
        live_ids = {
            row["session_id"]
            for row in registered["sessions"]
            if isinstance(row, dict)
            and isinstance(row.get("session_id"), str)
            and not row.get("ended")
            and isinstance(row.get("pid"), str)
            and bool(row["pid"])
            and _start(proc, row["pid"]) == row.get("procStart")
        }
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
        if retain and not has_snapshot and item is not None:
            _write_snapshot(
                home / BOOT_RELPATH,
                json.dumps(
                    {**result, "sessions": [*stale, item], "boot_id": _boot_id(proc)},
                    separators=(",", ":"),
                ).encode(),
            )
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
        live_rows = [
            row for row in entries if isinstance(row, dict) and row.get("session_id") in live_ids
        ]
        entries = [row for row in entries if row not in live_rows]
        if path == home / BOOT_RELPATH:
            _acknowledge(home, live_ids)
        blocked = [
            item
            for item in entries
            if isinstance(item, dict)
            and isinstance(item.get("session_id"), str)
            and item["session_id"] in unconsumed
        ]
        emitted = [
            item
            for item in entries
            if item not in blocked and not (isinstance(item, dict) and item.get("ended"))
        ]
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
        if retain and emitted and path != home / BOOT_RELPATH:
            _write_snapshot(
                home / BOOT_RELPATH,
                json.dumps(
                    {**result, "sessions": emitted, "boot_id": _boot_id(proc)},
                    separators=(",", ":"),
                ).encode(),
            )
        if retain and path == home / BOOT_RELPATH:
            result["sessions"] = emitted
        elif blocked or registry:
            remaining = {**result, "sessions": [*blocked, *live_rows] if registry else blocked}
            if registry and isinstance(data, dict) and "boot_id" in data:
                remaining["boot_id"] = data["boot_id"]
            try:
                _write_snapshot(path, json.dumps(remaining, separators=(",", ":")).encode())
            except (OSError, ValueError) as exc:
                print(f"agent snapshot: cannot retain snapshot: {exc}", file=sys.stderr)
                emitted = []
        elif not _unlink_stale(path):
            emitted = []
        result["sessions"] = emitted
    entries = result["sessions"]
    if isinstance(entries, list):
        result["sessions"] = [
            {key: value for key, value in row.items() if key not in {"pid", "procStart", "ended"}}
            if isinstance(row, dict)
            else row
            for row in entries
        ]
    return result


def window_names(*, timeout: float | None = None) -> dict[str, str]:
    from jailbee.services.local_proc import LocalProc

    try:
        args = ["list-windows", "-a", "-F", "#{session_name}\t#{window_id}\t#{window_name}"]
        result = (
            LocalProc().tmux(args) if timeout is None else LocalProc().tmux(args, timeout=timeout)
        )
        if result.returncode:
            return {}
        rows = (line.split("\t", 2) for line in result.stdout.splitlines())
        return {row[1]: row[2] for row in rows if len(row) == 3 and row[0] == "autostart"}
    except (OSError, ValueError, TimeoutExpired):
        return {}


def main(argv: list[str]) -> int:
    if argv == ["--capture"]:
        try:
            capture(Path(pwd.getpwuid(os.getuid()).pw_dir), windows=window_names())
        except Exception as exc:
            print(f"agent snapshot: {exc}", file=sys.stderr)
        return 0
    if argv in (["--boot"], ["--boot", "--retain"]):
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        result = boot(home, retain=True) if "--retain" in argv else boot(home)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    if argv[:1] == ["--ack-boot"] and all(_UUID.fullmatch(sid) for sid in argv[1:]):
        try:
            acknowledge(Path(pwd.getpwuid(os.getuid()).pw_dir), argv[1:])
        except (OSError, ValueError) as exc:
            print(f"agent snapshot: cannot acknowledge boot sessions: {exc}", file=sys.stderr)
            return 1
        return 0
    return 2
