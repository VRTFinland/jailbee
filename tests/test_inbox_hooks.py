import json

import pytest
from typer.testing import CliRunner

from jailbee.inbox import role
from jailbee.inbox.container_cmd import inbox_app


@pytest.fixture
def set_role(monkeypatch, tmp_path):
    marker = tmp_path / "role"
    monkeypatch.setattr(role, "ROLE_FILE", marker)
    monkeypatch.setattr(role, "LEGACY_CHILD_FILE", tmp_path / "legacy")
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)

    def set_value(value):
        marker.write_text(value)

    return set_value


@pytest.mark.parametrize("source", ["resume", "fork", "startup", "clear", "compact"])
@pytest.mark.parametrize("container_role", ["child", "orchestrator", "plain"])
def test_session_start(set_role, source, container_role):
    set_role(container_role)
    result = CliRunner().invoke(
        inbox_app, ["hook", "session-start"], input=json.dumps({"source": source})
    )
    assert result.exit_code == 0
    if source in {"resume", "fork"} and container_role != "plain":
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "restarted automatically" in context
        assert "task list" in context
    else:
        assert result.stdout == ""


@pytest.mark.parametrize("container_role", ["child", "orchestrator", "plain"])
@pytest.mark.parametrize(
    "payload,block",
    [
        ({"stop_hook_active": True, "background_tasks": []}, False),
        ({}, False),
        ({"background_tasks": [{"command": "jailbee inbox follow", "status": "running"}]}, False),
        (
            {
                "background_tasks": [
                    {"command": "jailbee inbox wait --timeout none", "status": "running"}
                ]
            },
            False,
        ),
        ({"background_tasks": []}, True),
        ({"background_tasks": [{"command": "jailbee inbox follow", "status": "completed"}]}, True),
    ],
)
def test_stop(set_role, container_role, payload, block):
    set_role(container_role)
    result = CliRunner().invoke(inbox_app, ["hook", "stop"], input=json.dumps(payload))
    assert result.exit_code == 0
    if container_role == "child" and block:
        output = json.loads(result.stdout)
        assert output["decision"] == "block"
        assert "jailbee inbox wait --timeout none" in output["reason"]
    else:
        assert result.stdout == ""


def test_start_hook_survives_unclean_stop_without_capture(tmp_path, monkeypatch, set_role):
    import pwd
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot

    sid = "01234567-1234-1234-1234-123456789abc"
    set_role("plain")
    monkeypatch.setattr(snapshot, "PROC", tmp_path / "proc")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    result = CliRunner().invoke(
        inbox_app,
        ["hook", "session-start"],
        input=json.dumps({"source": "startup", "session_id": sid, "cwd": "/work"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert snapshot.boot(tmp_path, proc=tmp_path / "dead-proc")["sessions"] == [
        {
            "agent": "claude",
            "session_id": sid,
            "cwd": "/work",
            "launcher": ["claude"],
            "window": None,
        }
    ]
    assert snapshot.boot(tmp_path, proc=tmp_path / "dead-proc")["sessions"] == []


def test_end_hook_removes_session_even_with_clean_stop_snapshot(tmp_path, monkeypatch, set_role):
    import pwd
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot

    sid = "01234567-1234-1234-1234-123456789abc"
    set_role("child")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    runner = CliRunner()
    payload = json.dumps({"session_id": sid, "cwd": "/work"})
    assert runner.invoke(inbox_app, ["hook", "session-start"], input=payload).exit_code == 0
    result = runner.invoke(inbox_app, ["hook", "session-end"], input=payload)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert snapshot.capture(tmp_path, proc=tmp_path / "proc", windows={}) == 0
    assert snapshot.boot(tmp_path, proc=tmp_path / "proc")["sessions"] == []


@pytest.mark.parametrize("source", ["resume", "fork", "clear", "compact"])
def test_replacing_hook_session_preserves_launcher_and_drops_old_id(
    tmp_path, monkeypatch, set_role, source
):
    import pwd
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot
    from tests.test_agent_snapshot import ID, live, marker, record

    proc = tmp_path / "proc"
    live(proc, 7)
    record(tmp_path, 7)
    marker(tmp_path, 7, b"777\0claude-jb\0-P\0codex\0--tools\0\0")
    monkeypatch.setattr(snapshot, "PROC", proc)
    monkeypatch.setattr(snapshot.os, "getppid", lambda: 7)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    set_role("plain")
    runner = CliRunner()

    def start(sid, event):
        return runner.invoke(
            inbox_app,
            ["hook", "session-start"],
            input=json.dumps({"session_id": sid, "cwd": "/work", "source": event}),
        )

    assert start(ID, "startup").exit_code == 0
    new_id = "aaaaaaaa-1234-1234-1234-123456789abc"
    assert start(new_id, source).exit_code == 0
    assert start(new_id, source).exit_code == 0
    # Claude's own PID file may still contain the pre-clear session id.
    assert snapshot.capture(tmp_path, proc=proc, windows={}) == 1
    result = snapshot.boot(tmp_path, proc=tmp_path / "dead-proc")
    assert result["sessions"] == [
        {
            "agent": "claude",
            "session_id": new_id,
            "cwd": "/work",
            "launcher": ["claude-jb", "-P", "codex", "--tools", ""],
            "window": None,
        }
    ]
    assert snapshot.boot(tmp_path, proc=tmp_path / "dead-proc")["sessions"] == []


def test_hook_registry_keeps_other_launchers_and_ignores_background_sessions(
    tmp_path, monkeypatch, set_role
):
    import pwd
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot
    from tests.test_agent_snapshot import ID, live, record

    proc = tmp_path / "proc"
    live(proc, 7)
    live(proc, 8)
    record(tmp_path, 7)
    record(tmp_path, 8, kind="background")
    monkeypatch.setattr(snapshot, "PROC", proc)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    set_role("plain")
    runner = CliRunner()
    for pid, sid in [(7, ID), (8, "aaaaaaaa-1234-1234-1234-123456789abc")]:
        monkeypatch.setattr(snapshot.os, "getppid", lambda pid=pid: pid)
        result = runner.invoke(
            inbox_app,
            ["hook", "session-start"],
            input=json.dumps({"session_id": sid, "cwd": "/work"}),
        )
        assert result.exit_code == 0
    assert [
        s["session_id"] for s in snapshot.boot(tmp_path, proc=tmp_path / "dead")["sessions"]
    ] == [ID]


def test_hook_walks_parent_shell_and_preserves_autostart_window(tmp_path, monkeypatch, set_role):
    import pwd
    from subprocess import CompletedProcess
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot
    from jailbee.services.local_proc import LocalProc
    from tests.test_agent_snapshot import ID, live, record

    proc = tmp_path / "proc"
    live(proc, 7)
    record(tmp_path, 7, tmux="autostart-2:@3.%3")
    (proc / "7" / "cmdline").write_bytes(b"node\0/usr/bin/claude\0")
    live(proc, 9)
    (proc / "9" / "cmdline").write_bytes(b"bash\0-c\0jailbee inbox hook session-start\0")
    (proc / "9" / "stat").write_text("9 (bash) S 7 " + "0 " * 17 + "888 0")
    monkeypatch.setattr(snapshot, "PROC", proc)
    monkeypatch.setattr(snapshot.os, "getppid", lambda: 9)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    monkeypatch.setenv("TMUX", "tmux-socket,1,0")
    calls = []

    def tmux(self, args, *, timeout=None):
        calls.append(timeout)
        return CompletedProcess(args, 0, "autostart\t@3\tclaude\n", "")

    monkeypatch.setattr(LocalProc, "tmux", tmux)
    set_role("plain")
    result = CliRunner().invoke(
        inbox_app, ["hook", "session-start"], input=json.dumps({"session_id": ID, "cwd": "/work"})
    )
    assert result.exit_code == 0
    assert calls == [0.2]
    assert snapshot.boot(tmp_path, proc=tmp_path / "dead")["sessions"] == [
        {
            "agent": "claude",
            "session_id": ID,
            "cwd": "/work",
            "launcher": ["claude"],
            "window": "claude",
        }
    ]


def test_ended_session_is_not_resurrected_from_claude_pid_file(tmp_path, monkeypatch, set_role):
    import pwd
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot
    from tests.test_agent_snapshot import ID, live, record

    proc = tmp_path / "proc"
    live(proc, 7)
    record(tmp_path, 7)
    monkeypatch.setattr(snapshot, "PROC", proc)
    monkeypatch.setattr(snapshot.os, "getppid", lambda: 7)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    set_role("plain")
    runner = CliRunner()
    payload = json.dumps({"session_id": ID, "cwd": "/work"})
    runner.invoke(inbox_app, ["hook", "session-start"], input=payload)
    runner.invoke(inbox_app, ["hook", "session-end"], input=payload)
    assert snapshot.boot(tmp_path, proc=proc)["sessions"] == []
    assert snapshot.boot(tmp_path, proc=tmp_path / "dead")["sessions"] == []


@pytest.mark.parametrize("failure", ["locked", "unwritable", "tmux-timeout"])
def test_hook_registry_failures_are_silent_and_keep_resume_context(
    tmp_path, monkeypatch, set_role, failure
):
    import fcntl
    import os
    import pwd
    from subprocess import TimeoutExpired
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot
    from jailbee.services.local_proc import LocalProc
    from tests.test_agent_snapshot import ID

    monkeypatch.setattr(snapshot, "PROC", tmp_path / "proc")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    path = tmp_path / snapshot.REGISTRY_RELPATH
    path.parent.mkdir(parents=True)
    lock = None
    if failure == "locked":
        lock = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif failure == "unwritable":
        path.mkdir()
    else:
        monkeypatch.setenv("TMUX", "socket,1,0")

        def timeout(self, args, **kwargs):
            raise TimeoutExpired("tmux", 0.2)

        monkeypatch.setattr(LocalProc, "tmux", timeout)
    set_role("child")
    try:
        result = CliRunner().invoke(
            inbox_app,
            ["hook", "session-start"],
            input=json.dumps({"session_id": ID, "cwd": "/work", "source": "resume"}),
        )
        assert result.exit_code == 0
        assert result.stderr == ""
        assert (
            "restarted automatically"
            in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        )
        result = CliRunner().invoke(
            inbox_app, ["hook", "session-end"], input=json.dumps({"session_id": ID})
        )
        assert result.exit_code == 0
        assert result.output == ""
    finally:
        if lock is not None:
            os.close(lock)


def test_clean_stop_freezes_registry_before_shutdown_end_hook(tmp_path, monkeypatch, set_role):
    import pwd
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot
    from tests.test_agent_snapshot import ID

    monkeypatch.setattr(snapshot, "PROC", tmp_path / "proc")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    set_role("plain")
    runner = CliRunner()
    payload = json.dumps({"session_id": ID, "cwd": "/work", "reason": "other"})
    assert runner.invoke(inbox_app, ["hook", "session-start"], input=payload).exit_code == 0
    assert snapshot.capture(tmp_path, proc=tmp_path / "proc", windows={}) == 1
    assert runner.invoke(inbox_app, ["hook", "session-end"], input=payload).exit_code == 0
    assert [row["session_id"] for row in snapshot.boot(tmp_path)["sessions"]] == [ID]
    assert snapshot.boot(tmp_path)["sessions"] == []


def test_capture_merges_pre_plugin_sessions_without_reviving_ended_ones(tmp_path, monkeypatch, set_role):
    import pwd
    from types import SimpleNamespace

    from jailbee import agent_snapshot as snapshot
    from tests.test_agent_snapshot import ID, live, record

    proc = tmp_path / "proc"
    other_id = "aaaaaaaa-1234-1234-1234-123456789abc"
    record(tmp_path, 7)
    record(tmp_path, 8, sessionId=other_id)
    live(proc, 7)
    live(proc, 8)
    monkeypatch.setattr(snapshot, "PROC", proc)
    monkeypatch.setattr(snapshot.os, "getppid", lambda: 7)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    set_role("plain")
    runner = CliRunner()
    payload = json.dumps({"session_id": ID, "cwd": "/work"})
    runner.invoke(inbox_app, ["hook", "session-start"], input=payload)
    runner.invoke(inbox_app, ["hook", "session-end"], input=payload)
    assert snapshot.capture(tmp_path, proc=proc, windows={}) == 1
    assert [row["session_id"] for row in snapshot.boot(tmp_path)["sessions"]] == [other_id]
    assert snapshot.boot(tmp_path)["sessions"] == []


def test_concurrent_hook_retries_brief_lock_contention_without_losing_session(tmp_path, monkeypatch):
    import fcntl
    import os
    import threading
    import time

    from jailbee import agent_snapshot as snapshot
    from tests.test_agent_snapshot import ID

    monkeypatch.setattr(snapshot, "PROC", tmp_path / "proc")
    path = tmp_path / snapshot.REGISTRY_RELPATH
    path.parent.mkdir(parents=True)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    def release():
        time.sleep(0.02)
        fcntl.flock(fd, fcntl.LOCK_UN)
    thread = threading.Thread(target=release)
    thread.start()
    try:
        snapshot.register_session(tmp_path, {"session_id": ID, "cwd": "/work"})
        assert [row["session_id"] for row in snapshot.boot(tmp_path)["sessions"]] == [ID]
    finally:
        thread.join()
        os.close(fd)


def test_node_launcher_clear_replaces_session_before_claude_pid_file_exists(tmp_path, monkeypatch):
    from jailbee import agent_snapshot as snapshot
    from tests.test_agent_snapshot import ID, live

    proc = tmp_path / "proc"
    live(proc, 7)
    (proc / "7" / "cmdline").write_bytes(b"node\0/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js\0--model\0opus\0")
    monkeypatch.setattr(snapshot, "PROC", proc)
    monkeypatch.setattr(snapshot.os, "getppid", lambda: 7)
    snapshot.register_session(tmp_path, {"session_id": ID, "cwd": "/work"})
    other_id = "aaaaaaaa-1234-1234-1234-123456789abc"
    snapshot.register_session(tmp_path, {"session_id": other_id, "cwd": "/work", "source": "clear"})
    assert snapshot.boot(tmp_path)["sessions"] == [{"agent": "claude", "session_id": other_id,
        "cwd": "/work", "launcher": ["claude", "--model", "opus"], "window": None}]


def test_new_start_invalidates_snapshot_from_cancelled_shutdown(tmp_path, monkeypatch):
    from jailbee import agent_snapshot as snapshot
    from tests.test_agent_snapshot import ID

    monkeypatch.setattr(snapshot, "PROC", tmp_path / "proc")
    snapshot.register_session(tmp_path, {"session_id": ID, "cwd": "/work"})
    snapshot.capture(tmp_path, proc=tmp_path / "proc", windows={})
    snapshot.register_session(tmp_path, {"session_id": ID}, ended=True)
    other_id = "aaaaaaaa-1234-1234-1234-123456789abc"
    snapshot.register_session(tmp_path, {"session_id": other_id, "cwd": "/work"})
    assert [row["session_id"] for row in snapshot.boot(tmp_path)["sessions"]] == [other_id]


@pytest.mark.parametrize("command", ["stop", "session-start", "session-end"])
@pytest.mark.parametrize("payload", ["{", "[]", "[" * 2000, "x" * 65537])
def test_malformed_input(set_role, command, payload):
    set_role("child")
    result = CliRunner().invoke(inbox_app, ["hook", command], input=payload)
    assert result.exit_code == 0
    assert result.stdout == ""
