import os
import threading
import time

import pytest

from jailbee.inbox import cli, waiting


def test_watcher_wakes_on_rename_quickly(tmp_path):
    from jailbee.inbox.watch import InboxWatcher

    inbox = tmp_path / 'inbox'
    inbox.mkdir()
    source = tmp_path / 'source'
    source.write_text('{}')
    watcher = InboxWatcher(inbox, poll=5)
    if watcher.mode != 'inotify':
        pytest.skip('inotify unavailable')
    timer = threading.Timer(0.2, os.rename, args=(source, inbox / 'new.json'))
    try:
        timer.start()
        start = time.monotonic()
        watcher.wait(5)
        assert time.monotonic() - start < 1.5
    finally:
        timer.join()
        watcher.close()


def test_watcher_poll_mode_when_disabled(tmp_path):
    from jailbee.inbox.watch import InboxWatcher

    watcher = InboxWatcher(tmp_path, poll=1, inotify=False)
    assert watcher.mode == 'poll'
    watcher.close()


def test_watcher_armed_before_selection(tmp_path, monkeypatch):
    order = []
    class Watcher:
        def __init__(self, *args, **kwargs):
            order.append('watch')
        def close(self):
            pass
    monkeypatch.setattr(waiting, 'select_wakeup', lambda *args: order.append('select') or 'found')
    settings = cli.load_settings({'JAILBEE_ORCHESTRATION_DIR': str(tmp_path)})
    assert waiting.run_wait(settings, timeout=0, wake=None, quiet=0, watcher_factory=Watcher) == (0, 'found')
    assert order == ['watch', 'select']


def test_wait_slices_capped_at_watch_slice(tmp_path, monkeypatch):
    settings = cli.load_settings({'JAILBEE_ORCHESTRATION_DIR': str(tmp_path)})
    monkeypatch.setattr(waiting, 'select_wakeup', lambda *args: None)
    for timeout, quiet, expected in [(25, 0, [10, 10, 5]), (3, 60, [1, 1, 1])]:
        now = [0.0]
        slices = []
        class Watcher:
            def __init__(self, *args, **kwargs): pass
            def wait(self, duration):
                slices.append(duration)
                now[0] += duration
            def close(self): pass
        assert waiting.run_wait(settings, timeout=timeout, wake=None, quiet=quiet, watcher_factory=Watcher, clock=lambda: now[0]) == (1, None)
        assert slices == expected


def test_wait_settles_only_after_waiting(tmp_path, monkeypatch):
    settings = cli.load_settings({'JAILBEE_ORCHESTRATION_DIR': str(tmp_path)})
    calls = []
    monkeypatch.setattr(waiting, 'settle', lambda *a, **k: calls.append('settle'))
    class Watcher:
        def __init__(self, *args, **kwargs): pass
        def wait(self, duration): pass
        def close(self): pass
    for responses, expected in [(['found'], []), ([None, 'found', 'found'], ['settle'])]:
        calls.clear()
        values = iter(responses)
        monkeypatch.setattr(waiting, 'select_wakeup', lambda *args: next(values))
        assert waiting.run_wait(settings, timeout=10, wake=None, quiet=0, watcher_factory=Watcher) == (0, 'found')
        assert calls == expected


def test_forever_slices_and_poll_environment(tmp_path, monkeypatch):
    from jailbee.inbox.watch import InboxWatcher
    monkeypatch.setenv('JAILBEE_INBOX_INOTIFY', '0')
    watcher = InboxWatcher(tmp_path, poll=1)
    assert watcher.mode == 'poll'
    watcher.close()
    settings = cli.load_settings({'JAILBEE_ORCHESTRATION_DIR': str(tmp_path)})
    monkeypatch.setattr(waiting, 'select_wakeup', lambda *args: None)
    slices = []
    class Stop(Exception): pass
    class Watcher:
        def __init__(self, *args, **kwargs): pass
        def wait(self, duration):
            slices.append(duration)
            if len(slices) == 3: raise Stop()
        def close(self): pass
    with pytest.raises(Stop):
        waiting.run_wait(settings, timeout=None, wake=None, quiet=0, watcher_factory=Watcher)
    assert slices == [10, 10, 10]
