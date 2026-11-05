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


@pytest.mark.parametrize("with_snapshot", [False, True])
def test_boot_does_not_return_unconsumed_stale_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    with_snapshot: bool,
) -> None:
    record(tmp_path, 1)
    path = tmp_path / snapshot.SESSIONS_RELPATH / "1.json"
    if with_snapshot:
        target = tmp_path / snapshot.SNAPSHOT_RELPATH
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"version": 1, "source": "capture", "sessions": [
            {"agent": "claude", "session_id": ID, "cwd": "/work", "launcher": ["claude"], "window": None}
        ]}))
    original = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == path:
            raise PermissionError("denied")
        original(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []
    assert "denied" in capsys.readouterr().err
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []


@pytest.mark.parametrize("with_snapshot", [False, True])
def test_boot_retries_unconsumed_session_with_launcher_and_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_snapshot: bool,
) -> None:
    record(tmp_path, 1)
    session = tmp_path / snapshot.SESSIONS_RELPATH / "1.json"
    launch = marker(tmp_path, 1, b"777\0claude-jb\0-P\0codex\0")
    expected = {"agent": "claude", "session_id": ID, "cwd": "/work", "launcher": ["claude-jb", "-P", "codex"], "window": "claude" if with_snapshot else None}
    if with_snapshot:
        path = tmp_path / snapshot.SNAPSHOT_RELPATH
        path.write_text(json.dumps({"version": 1, "source": "capture", "sessions": [expected]}))
    original = Path.unlink

    def blocked(self: Path, missing_ok: bool = False) -> None:
        if self == session:
            raise PermissionError("denied")
        original(self, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", blocked)
        assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []
        assert launch.exists()
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == [expected]
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []


def test_boot_retains_only_blocked_snapshot_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other_id = "aaaaaaaa-1234-1234-1234-123456789abc"
    record(tmp_path, 1)
    record(tmp_path, 2, sessionId=other_id)
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "source": "capture", "sessions": [{"session_id": ID}, {"session_id": other_id}]}))
    blocked = tmp_path / snapshot.SESSIONS_RELPATH / "1.json"
    original = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == blocked:
            raise PermissionError("denied")
        original(self, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == [{"session_id": other_id}]
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == [{"session_id": ID}]
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []


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


def test_main_uses_passwd_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pwd
    from types import SimpleNamespace
    monkeypatch.setenv("HOME", "/wrong")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    seen = []
    monkeypatch.setattr(snapshot, "boot", lambda home: seen.append(home) or {})
    assert snapshot.main(["--boot"]) == 0
    assert seen == [tmp_path]


def test_string_pid_and_empty_launcher_args(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    record(tmp_path, 7)
    path = tmp_path / snapshot.SESSIONS_RELPATH / "7.json"
    data = json.loads(path.read_text())
    data["pid"] = "7"
    path.write_text(json.dumps(data))
    live(proc, 7)
    marker(tmp_path, 7, b"777\0claude-jb\0--tools\0\0")
    assert snapshot.capture(tmp_path, proc=proc, windows={}) == 1
    assert sessions(tmp_path)[0]["launcher"] == ["claude-jb", "--tools", ""]
    snapshot.boot(tmp_path, proc=proc)
    assert path.exists()


def test_stale_overflow_batches_without_losing_launchers(tmp_path: Path) -> None:
    for pid in (1, 2):
        record(tmp_path, pid, sessionId=f"{pid:08x}-1234-1234-1234-123456789abc")
        marker(tmp_path, pid, b"777\0claude-jb\0--append-system-prompt\0" + b"x" * 33000 + b"\0")
    first = snapshot.boot(tmp_path, proc=tmp_path / "proc")
    assert len(json.dumps(first).encode()) + 1 <= 65536
    assert len(first["sessions"]) == 1
    assert (tmp_path / snapshot.LAUNCHERS_RELPATH / "2").exists()
    second = snapshot.boot(tmp_path, proc=tmp_path / "proc")
    assert len(json.dumps(second).encode()) + 1 <= 65536
    assert len(second["sessions"]) == 1
    assert first["sessions"] != second["sessions"]
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []


def test_snapshot_directory_does_not_block_stale_recovery(tmp_path: Path) -> None:
    record(tmp_path, 1)
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    path.mkdir(parents=True)
    assert len(snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"]) == 1
    assert path.is_dir()


def test_huge_pid_is_invalid_not_an_exception(tmp_path: Path) -> None:
    record(tmp_path, 7)
    path = tmp_path / snapshot.SESSIONS_RELPATH / "7.json"
    data = json.loads(path.read_text())
    data["pid"] = "1" * 5000
    path.write_text(json.dumps(data))
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []


def test_snapshot_unlink_failure_defers_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    path.parent.mkdir(parents=True)
    row = {"session_id": ID}
    path.write_text(json.dumps({"version": 1, "source": "capture", "sessions": [row]}))
    original = Path.unlink
    def blocked(self: Path, missing_ok: bool = False) -> None:
        if self == path:
            raise PermissionError("denied")
        original(self, missing_ok=missing_ok)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", blocked)
        assert snapshot.boot(tmp_path)["sessions"] == []
    assert snapshot.boot(tmp_path)["sessions"] == [row]
    assert snapshot.boot(tmp_path)["sessions"] == []


def test_old_full_size_snapshot_respects_wire_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    path.parent.mkdir(parents=True)
    row = {"session_id": ID, "value": ""}
    base = {"version": 1, "source": "capture", "sessions": [row]}
    row["value"] = "x" * (65536 - len(json.dumps(base)))
    path.write_text(json.dumps(base))
    assert path.stat().st_size == 65536
    import pwd
    from types import SimpleNamespace
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    assert snapshot.main(["--boot"]) == 0
    output = capsys.readouterr().out
    assert len(output.encode()) <= 65536
    assert json.loads(output)["sessions"] == [row]


def test_snapshot_storage_reserves_newline(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        snapshot._write_snapshot(tmp_path / "snapshot", b"x" * 65536)
    snapshot._write_snapshot(tmp_path / "snapshot", b"x" * 65535)


def test_oversized_single_stale_row_is_dropped_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record(tmp_path, 1)
    marker(tmp_path, 1, b"777\0claude\0" + b"x" * 65000 + b"\0")
    path = tmp_path / snapshot.SESSIONS_RELPATH / "1.json"
    data = json.loads(path.read_text())
    data["cwd"] = "/" + "y" * 1000
    path.write_text(json.dumps(data))
    assert snapshot.boot(tmp_path)["sessions"] == []
    assert "oversized" in capsys.readouterr().err
    assert snapshot.boot(tmp_path)["sessions"] == []
    assert not path.exists()


def test_old_compact_maximum_snapshot_drops_unwireable_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import pwd
    from types import SimpleNamespace
    path = tmp_path / snapshot.SNAPSHOT_RELPATH
    path.parent.mkdir(parents=True)
    row = {"agent": "claude", "session_id": ID, "cwd": "/work", "launcher": ["claude", ""], "window": None}
    payload = {"version": 1, "source": "capture", "sessions": [row]}
    row["launcher"][1] = "x" * (65536 - len(json.dumps(payload, separators=(",", ":"))))
    path.write_text(json.dumps(payload, separators=(",", ":")))
    assert path.stat().st_size == 65536
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    assert snapshot.main(["--boot"]) == 0
    result = capsys.readouterr()
    assert len(result.out.encode()) <= 65536
    assert json.loads(result.out)["sessions"] == []
    assert "oversized" in result.err
    assert not path.exists()


@pytest.mark.parametrize("home_env", ["", "/incorrect"])
@pytest.mark.parametrize("action", ["--capture", "--boot"])
def test_main_passwd_home_matrix(tmp_path, monkeypatch, home_env, action):
    import pwd
    from types import SimpleNamespace
    monkeypatch.setenv("HOME", home_env)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    seen = []
    monkeypatch.setattr(snapshot, "window_names", lambda: {})
    monkeypatch.setattr(snapshot, "capture", lambda home, **kwargs: seen.append(home) or 0)
    monkeypatch.setattr(snapshot, "boot", lambda home: seen.append(home) or {})
    assert snapshot.main([action]) == 0
    assert seen == [tmp_path]


@pytest.mark.parametrize("kind", ["interactive", "background"])
def test_string_pid_live_record_preserved(tmp_path, kind):
    record(tmp_path, 7, kind=kind)
    path = tmp_path / snapshot.SESSIONS_RELPATH / "7.json"
    data = json.loads(path.read_text())
    data["pid"] = "7"
    path.write_text(json.dumps(data))
    live(tmp_path / "proc", 7)
    key = path.parent / "7.test.key"
    key.touch()
    launch = marker(tmp_path, 7, b"777\0claude\0")
    snapshot.boot(tmp_path, proc=tmp_path / "proc")
    assert path.exists() and key.exists() and launch.exists()


@pytest.mark.parametrize("source", ["marker", "cmdline"])
@pytest.mark.parametrize("argv", [["claude-jb", "--tools", "", "--model", "opus"], ["claude-jb", "--tools", ""]])
def test_empty_launcher_values_survive_capture_and_replay(tmp_path, source, argv):
    from jailbee.agent_resume import ResumeEntry, resume_run
    import shlex
    proc = tmp_path / "proc"
    record(tmp_path, 1)
    live(proc, 1)
    encoded = b"\0".join(x.encode() for x in argv) + b"\0"
    if source == "marker":
        marker(tmp_path, 1, b"777\0" + encoded)
    else:
        (proc / "1" / "cmdline").write_bytes(encoded)
    snapshot.capture(tmp_path, proc=proc, windows={})
    launcher = sessions(tmp_path)[0]["launcher"]
    assert launcher == argv
    command = resume_run(ResumeEntry(ID, "/work", tuple(launcher), None))
    assert shlex.split(command.split(" && exec ", 1)[1]) == argv + ["--resume", ID]
