import os
import threading
import time

import pytest

from jailbee.inbox import cli, waiting


def test_watcher_wakes_on_rename_quickly(tmp_path):
    from jailbee.inbox.watch import InboxWatcher

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = tmp_path / "source"
    source.write_text("{}")
    watcher = InboxWatcher(inbox, poll=5)
    if watcher.mode != "inotify":
        pytest.skip("inotify unavailable")
    rename_started = threading.Event()

    def publish():
        rename_started.set()
        os.rename(source, inbox / "new.json")

    timer = threading.Timer(0.2, publish)
    try:
        timer.start()
        start = time.monotonic()
        watcher.wait(5)
        elapsed = time.monotonic() - start
        assert rename_started.is_set()
        assert 0.1 <= elapsed < 1.5
    finally:
        timer.join()
        watcher.close()


def test_watcher_poll_mode_when_disabled(tmp_path):
    from jailbee.inbox.watch import InboxWatcher

    watcher = InboxWatcher(tmp_path, poll=1, inotify=False)
    assert watcher.mode == "poll"
    watcher.close()


def test_watcher_armed_before_selection(tmp_path, monkeypatch):
    order = []

    class Watcher:
        def __init__(self, *args, **kwargs):
            order.append("watch")

        def close(self):
            pass

    monkeypatch.setattr(waiting, "select_wakeup", lambda *args: order.append("select") or "found")
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    assert waiting.run_wait(settings, timeout=0, wake=None, quiet=0, watcher_factory=Watcher) == (
        0,
        "found",
    )
    assert order == ["watch", "select"]


def test_wait_slices_capped_at_watch_slice(tmp_path, monkeypatch):
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    monkeypatch.setattr(waiting, "select_wakeup", lambda *args: None)
    for timeout, quiet, expected in [(25, 0, [10, 10, 5]), (3, 60, [1, 1, 1])]:
        now = [0.0]
        slices = []

        class Watcher:
            def __init__(self, *args, **kwargs):
                pass

            def wait(self, duration, slices=slices, now=now):
                slices.append(duration)
                now[0] += duration

            def close(self):
                pass

        assert waiting.run_wait(
            settings,
            timeout=timeout,
            wake=None,
            quiet=quiet,
            watcher_factory=Watcher,
            clock=lambda now=now: now[0],
        ) == (1, None)
        assert slices == expected


def test_wait_warns_once_per_bad_file_across_poll_iterations(tmp_path, capsys):
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    settings.inbox.mkdir()
    corrupt = settings.inbox / "corrupt.json"
    corrupt.write_text("not json{")
    idless = settings.inbox / "idless.json"
    idless.write_text('{"type":"message","kind":"note"}')
    (tmp_path / "children.json").write_text(
        '{"version":1,"updated_at":' + str(time.time()) + ',"children":[]}'
    )
    now = [0.0]
    slices = []

    class Watcher:
        def __init__(self, *args, **kwargs):
            pass

        def wait(self, duration):
            slices.append(duration)
            now[0] += duration

        def close(self):
            pass

    assert waiting.run_wait(
        settings,
        timeout=3,
        wake=None,
        quiet=60,
        watcher_factory=Watcher,
        clock=lambda: now[0],
    ) == (1, None)
    assert slices == [1, 1, 1]
    assert sorted(capsys.readouterr().err.splitlines()) == sorted(
        f"jailbee inbox: skipping unreadable message {path}" for path in (corrupt, idless)
    )


def test_wait_settles_only_after_waiting(tmp_path, monkeypatch):
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    calls = []
    monkeypatch.setattr(waiting, "settle", lambda *a, **k: calls.append("settle"))

    class Watcher:
        def __init__(self, *args, **kwargs):
            pass

        def wait(self, duration):
            pass

        def close(self):
            pass

    for responses, expected in [(["found"], []), ([None, "found", "found"], ["settle"])]:
        calls.clear()
        values = iter(responses)
        monkeypatch.setattr(waiting, "select_wakeup", lambda *args, values=values: next(values))
        assert waiting.run_wait(
            settings, timeout=10, wake=None, quiet=0, watcher_factory=Watcher
        ) == (0, "found")
        assert calls == expected


def test_forever_slices_and_poll_environment(tmp_path, monkeypatch):
    from jailbee.inbox.watch import InboxWatcher

    monkeypatch.setenv("JAILBEE_INBOX_INOTIFY", "0")
    watcher = InboxWatcher(tmp_path, poll=1)
    assert watcher.mode == "poll"
    watcher.close()
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    monkeypatch.setattr(waiting, "select_wakeup", lambda *args: None)
    slices = []

    class StopError(Exception):
        pass

    class Watcher:
        def __init__(self, *args, **kwargs):
            pass

        def wait(self, duration):
            slices.append(duration)
            if len(slices) == 3:
                raise StopError()

        def close(self):
            pass

    with pytest.raises(StopError):
        waiting.run_wait(settings, timeout=None, wake=None, quiet=0, watcher_factory=Watcher)
    assert slices == [10, 10, 10]


def test_cmd_wait_inotify_environment_uses_poll_factory(tmp_path, monkeypatch):
    from jailbee.inbox.watch import InboxWatcher

    monkeypatch.setenv("JAILBEE_INBOX_INOTIFY", "0")
    modes = []
    real_run = waiting.run_wait

    def factory(path, *, poll):
        watcher = InboxWatcher(path, poll=poll)
        modes.append(watcher.mode)
        return watcher

    def run(settings, **kwargs):
        return real_run(settings, watcher_factory=factory, **kwargs)

    monkeypatch.setattr(waiting, "run_wait", run)
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    assert cli.cmd_wait(settings, timeout="0") == 1
    assert modes == ["poll"]


def test_interrupt_releases_wait_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_DIR", str(tmp_path))

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(waiting, "run_wait", interrupt)
        with pytest.raises(KeyboardInterrupt):
            cli.cmd_wait(cli.load_settings(os.environ), timeout="none")
    assert cli.cmd_wait(cli.load_settings(os.environ), timeout="0") == 1
    assert "Traceback" not in capsys.readouterr().err


def test_wait_rechecks_without_event(tmp_path):
    settings = cli.load_settings(
        {"JAILBEE_ORCHESTRATION_DIR": str(tmp_path), "JAILBEE_INBOX_SETTLE": "0"}
    )
    settings.inbox.mkdir()
    message = settings.inbox / "message.json"

    class Watcher:
        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout):
            message.write_text('{"id":"arrival","type":"message","kind":"note"}')

        def close(self):
            pass

    assert waiting.run_wait(settings, timeout=30, wake=None, quiet=0, watcher_factory=Watcher) == (
        0,
        str(message),
    )


def test_settle_burst_extends_quiet_period(tmp_path):
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    settings.inbox.mkdir()
    (settings.inbox / "one.json").write_text("{}")
    now = [0.0]

    def sleep(duration):
        now[0] += duration
        if 0.4 <= now[0] < 0.6:
            (settings.inbox / "two.json").write_text("{}")
        if 1.2 <= now[0] < 1.4:
            (settings.inbox / "three.json").write_text("{}")

    waiting.settle(settings, 10, clock=lambda: now[0], sleep=sleep)
    assert now[0] >= 2.2
    assert len(list(settings.inbox.glob("*.json"))) == 3


def test_settle_stops_at_deadline(tmp_path):
    settings = cli.load_settings(
        {"JAILBEE_ORCHESTRATION_DIR": str(tmp_path), "JAILBEE_INBOX_SETTLE": "3"}
    )
    settings.inbox.mkdir()
    now = [0.0]

    def sleep(duration):
        now[0] += duration

    waiting.settle(settings, 0.7, clock=lambda: now[0], sleep=sleep)
    assert now[0] == pytest.approx(0.7)


@pytest.mark.parametrize("race", ["vanished", "unreadable"])
@pytest.mark.parametrize("replacement", [False, True])
def test_cmd_wait_reselects_when_selected_message_becomes_unreadable(
    tmp_path, monkeypatch, capsys, race, replacement
):
    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    settings.inbox.mkdir()
    first = settings.inbox / "a.json"
    first.write_text('{"id":"first","type":"message","kind":"note"}')
    if replacement:
        (settings.inbox / "b.json").write_text(
            '{"id":"replacement","type":"message","kind":"note"}'
        )
    select = waiting.select_wakeup

    def select_then_race(*args):
        selected = select(*args)
        if selected == str(first):
            if race == "vanished":
                first.unlink()
            else:
                first.write_text("not json{")
        return selected

    monkeypatch.setattr(waiting, "select_wakeup", select_then_race)
    assert cli.cmd_wait(settings, timeout="0") == (0 if replacement else 1)
    assert capsys.readouterr().out == ("replacement\n" if replacement else "")


def test_read_vanished_during_move(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    settings = cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})
    settings.inbox.mkdir()
    (settings.inbox / "message.json").write_text('{"id":"hello"}')

    def vanished(self, target):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "rename", vanished)
    assert cli.cmd_read(settings, "hello") == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "no unread message" in output.err


def test_watchdog_wakes_without_inbox_events(tmp_path, monkeypatch):
    import json

    settings = cli.load_settings(
        {"JAILBEE_ORCHESTRATION_DIR": str(tmp_path), "JAILBEE_INBOX_SETTLE": "0"}
    )
    settings.inbox.mkdir()
    (tmp_path / "children.json").write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": 1000,
                "children": [{"name": "kid", "running": True, "started_at": 1000}],
            }
        )
    )
    now = [1000.0]
    monkeypatch.setattr(waiting.time, "time", lambda: now[0])
    slices = []

    class Watcher:
        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout):
            slices.append(timeout)
            now[0] += timeout

        def close(self):
            pass

    assert waiting.run_wait(
        settings, timeout=5, wake=None, quiet=2, watcher_factory=Watcher, clock=lambda: now[0]
    ) == (0, "quiet-child kid")
    assert slices == [1, 1]


def test_sigint_releases_wait_lock_in_process(tmp_path):
    import fcntl
    import signal
    import subprocess
    import sys

    env = os.environ | {"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)}
    process = subprocess.Popen(
        [sys.executable, "-m", "jailbee.inbox", "wait", "--timeout", "none"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        acquired = False
        while time.monotonic() < deadline:
            path = tmp_path / "wait.lock"
            if path.exists():
                with path.open("r") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        acquired = True
                        break
            time.sleep(0.01)
        assert acquired
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130
        assert "Traceback" not in stderr
        assert stdout == ""
        result = subprocess.run(
            [sys.executable, "-m", "jailbee.inbox", "wait", "--timeout", "0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 1, result.stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
