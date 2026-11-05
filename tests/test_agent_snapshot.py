import json
import os
import sys
from pathlib import Path

import pytest

from jailbee import agent_snapshot as snapshot

ID = "01234567-1234-1234-1234-123456789abc"


def record(home: Path, pid: int, **kwargs: object) -> None:
    directory = home / snapshot.SESSIONS_RELPATH
    directory.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "procStart": "777", "sessionId": ID, "cwd": "/work", "kind": "interactive", "tmux": "autostart:@3.%3"}
    data.update(kwargs)
    (directory / f"{pid}.json").write_text(json.dumps(data))


def live(proc: Path, pid: int, start: str = "777") -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "stat").write_text(f"{pid} (claude) S " + "0 " * 18 + start + " 0")
    (directory / "cmdline").write_bytes(b"claude\0--model\0opus\0")


def marker(home: Path, pid: int, value: bytes) -> Path:
    directory = home / snapshot.LAUNCHERS_RELPATH
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / str(pid)
    path.write_bytes(value)
    return path


def sessions(home: Path) -> list[dict[str, object]]:
    return json.loads((home / snapshot.SNAPSHOT_RELPATH).read_text())["sessions"]


def test_capture_records_live_interactive_sessions(tmp_path: Path) -> None:
    home, proc = tmp_path / "home", tmp_path / "proc"
    record(home, 1)
    record(home, 2, kind="background")
    live(proc, 1)
    live(proc, 2)
    assert snapshot.capture(home, proc=proc, windows={"@3": "claude"}) == 1
    assert sessions(home) == [{"agent": "claude", "session_id": ID, "cwd": "/work", "launcher": ["claude", "--model", "opus"], "window": "claude"}]
    assert (home / snapshot.SNAPSHOT_RELPATH).stat().st_mode & 0o777 == 0o600


def test_capture_uses_launcher_file_when_proc_start_matches(tmp_path: Path) -> None:
    home, proc = tmp_path / "home", tmp_path / "proc"
    record(home, 1)
    live(proc, 1)
    marker(home, 1, b"777\0claude-jb\0-P\0codex\0")
    snapshot.capture(home, proc=proc, windows={})
    assert sessions(home)[0]["launcher"] == ["claude-jb", "-P", "codex"]


def test_launcher_file_with_other_proc_start_is_ignored(tmp_path: Path) -> None:
    home, proc = tmp_path / "home", tmp_path / "proc"
    record(home, 1)
    live(proc, 1)
    marker(home, 1, b"778\0claude-jb\0")
    snapshot.capture(home, proc=proc, windows={})
    assert sessions(home)[0]["launcher"] == ["claude", "--model", "opus"]


def test_capture_skips_dead_pid_and_foreign_tmux_session(tmp_path: Path) -> None:
    home, proc = tmp_path / "home", tmp_path / "proc"
    record(home, 1, tmux="other:@3.%3")
    record(home, 2)
    live(proc, 1)
    assert snapshot.capture(home, proc=proc, windows={"@3": "claude"}) == 1
    assert sessions(home)[0]["window"] is None


def test_capture_with_no_sessions_keeps_existing_snapshot(tmp_path: Path) -> None:
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b"previous")
    assert snapshot.capture(tmp_path, proc=tmp_path / "proc", windows={}) == 0
    assert path.read_bytes() == b"previous"


def test_boot_returns_and_deletes_snapshot(tmp_path: Path) -> None:
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    path.parent.mkdir(parents=True)
    data = {"version": 1, "source": "capture", "sessions": []}
    path.write_text(json.dumps(data))
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc") == data
    assert not path.exists()


def test_boot_consumes_snapshot_and_stale_files(tmp_path: Path) -> None:
    record(tmp_path, 1)
    key = tmp_path / snapshot.SESSIONS_RELPATH / "1.foo.key"
    key.touch()
    launch = marker(tmp_path, 1, b"777\0claude\0")
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    data = {"version": 1, "source": "capture", "sessions": [{"session_id": ID}]}
    path.write_text(json.dumps(data))
    record(tmp_path, 2)
    live(tmp_path / "proc", 2)
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc") == data
    assert not key.exists() and not launch.exists()
    assert (tmp_path / snapshot.SESSIONS_RELPATH / "2.json").exists()
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []


def test_boot_cleanup_directories_do_not_block_snapshot(tmp_path: Path) -> None:
    record(tmp_path, 4)
    directory = tmp_path / snapshot.SESSIONS_RELPATH
    (directory / "1.json").mkdir()
    (directory / "2.foo.key").mkdir()
    launches = tmp_path / snapshot.LAUNCHERS_RELPATH
    launches.mkdir(parents=True)
    (launches / "3").mkdir()
    (launches / "4").write_bytes(b"777\0claude\0")
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    data = {"version": 1, "source": "capture", "sessions": [{"session_id": ID}]}
    path.write_text(json.dumps(data))
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc") == data
    assert not (directory / "4.json").exists()
    assert not (launches / "4").exists()


def test_capture_oversize_keeps_existing_snapshot(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b"previous")
    for pid in (1, 2):
        record(tmp_path, pid)
        live(proc, pid)
        marker(tmp_path, pid, b"777\0claude\0" + b"x" * 40000 + b"\0")
    with pytest.raises(ValueError, match="snapshot"):
        snapshot.capture(tmp_path, proc=proc, windows={})
    assert path.read_bytes() == b"previous"


def test_boot_without_snapshot_uses_stale_records(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    for pid in (1, 2, 3):
        record(tmp_path, pid)
    live(proc, 3)
    marker(tmp_path, 1, b"777\0claude-jb\0-P\0codex\0")
    marker(tmp_path, 2, b"778\0claude-jb\0")
    result = snapshot.boot(tmp_path, proc=proc)
    assert result["source"] == "stale"
    assert [s["launcher"] for s in result["sessions"]] == [["claude-jb", "-P", "codex"], ["claude"]]
    assert (tmp_path / snapshot.SESSIONS_RELPATH / "3.json").exists()


def test_rejects_unsafe_files_and_invalid_snapshot(tmp_path: Path) -> None:
    record(tmp_path, 1)
    path = tmp_path / snapshot.SESSIONS_RELPATH / "1.json"
    path.unlink()
    os.mkfifo(path)
    assert snapshot.capture(tmp_path, proc=tmp_path / "proc", windows={}) == 0
    target = tmp_path / snapshot.SNAPSHOT_RELPATH
    target.parent.mkdir(parents=True)
    target.symlink_to(path)
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc") == {"version": 1, "source": "capture", "sessions": []}


def test_boot_preserves_live_background_session_and_launcher(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    record(tmp_path, 7, kind="background")
    live(proc, 7)
    launch = marker(tmp_path, 7, b"777\0claude\0")
    key = tmp_path / snapshot.SESSIONS_RELPATH / "7.test.key"
    key.touch()
    assert snapshot.boot(tmp_path, proc=proc)["sessions"] == []
    assert (tmp_path / snapshot.SESSIONS_RELPATH / "7.json").exists()
    assert launch.exists() and key.exists()


def test_boot_consumes_malformed_and_oversized_records(tmp_path: Path) -> None:
    directory = tmp_path / snapshot.SESSIONS_RELPATH
    directory.mkdir(parents=True)
    (directory / "1.json").write_text("{")
    (directory / "2.json").write_bytes(b"x" * 65537)
    (directory / "2.foo.key").touch()
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []
    assert not list(directory.iterdir())


def test_window_names_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from subprocess import CompletedProcess

    calls = []
    def tmux(self: object, args: list[str]) -> CompletedProcess[str]:
        calls.append(args)
        return CompletedProcess(args, 0, "autostart\t@3\tclaude\nother\t@4\tignore\ninvalid\n", "")

    monkeypatch.setattr(snapshot.LocalProc, "tmux", tmux)
    assert snapshot.window_names() == {"@3": "claude"}
    assert calls == [["list-windows", "-a", "-F", "#{session_name}\t#{window_id}\t#{window_name}"]]
    def failure(self: object, args: list[str]) -> CompletedProcess[str]:
        raise OSError("tmux unavailable")
    monkeypatch.setattr(snapshot.LocalProc, "tmux", failure)
    assert snapshot.window_names() == {}


def test_main_capture_errors_and_boot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def failure(*args: object, **kwargs: object) -> int:
        raise OSError("failure")
    monkeypatch.setattr(snapshot, "window_names", lambda: {})
    monkeypatch.setattr(snapshot, "capture", failure)
    assert snapshot.main(["--capture"]) == 0
    assert "failure" in capsys.readouterr().err
    monkeypatch.setattr(snapshot, "boot", lambda home: {"version": 1, "source": "stale", "sessions": []})
    assert snapshot.main(["--boot"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "stale"
    assert snapshot.main([]) == 2


def test_main_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from jailbee import container_main
    monkeypatch.setattr(container_main.container_deps, "check", lambda: None)
    monkeypatch.setattr(container_main, "PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(snapshot, "main", lambda args: 42 if args == ["--boot"] else 1)
    monkeypatch.setattr(sys, "argv", ["jailbee", "_agent-snapshot", "--boot"])
    monkeypatch.delitem(sys.modules, "jailbee.container_cli", raising=False)
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 42
    assert "jailbee.container_cli" not in sys.modules
