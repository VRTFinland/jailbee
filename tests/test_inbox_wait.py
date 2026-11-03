import fcntl
import json
import os
from pathlib import Path

import pytest

from jailbee.inbox.cli import InboxUsage, Settings


def put(path: Path, data: dict, stamp: float = 1000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    os.utime(path, (stamp, stamp))
    return path


def test_select_respects_filter_and_first_report(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    put(tmp_path / 'children.json', {'version': 1, 'updated_at': 1000, 'children': [{'name': 'kid', 'running': True, 'started_at': 900}]})
    put(tmp_path / 'inbox/read/old.json', {'type': 'message', 'from': 'kid', 'kind': 'note'}, 950)
    put(tmp_path / 'inbox/a.json', {'type': 'message', 'from': 'kid', 'kind': 'note'}, 1000)
    selected = put(tmp_path / 'inbox/b.json', {'type': 'message', 'from': 'other', 'kind': 'question'}, 1001)
    assert select_wakeup(tmp_path, frozenset({'question'}), 0, 1001) == str(selected)
    (tmp_path / 'inbox/read/old.json').unlink()
    assert select_wakeup(tmp_path, frozenset({'question'}), 0, 1001) == str(tmp_path / 'inbox/a.json')


def test_select_skips_live_reserved_status(tmp_path):
    from jailbee.inbox.waiting import find_status, select_wakeup

    status = put(tmp_path / 'inbox/a.json', {'type': 'status', 'state': 'done', 'ref': 'abc'})
    lock = tmp_path / 'status-waits/abc.lock'
    lock.parent.mkdir()
    with lock.open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert select_wakeup(tmp_path, None, 0, 1000) is None
        assert find_status(tmp_path / 'inbox', 'abc') == status
    assert select_wakeup(tmp_path, None, 0, 1000) == str(status)
    assert find_status(tmp_path / 'inbox', 'missing') is None


def test_select_quiet_child_cleans_name(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    put(tmp_path / 'children.json', {'version': 1, 'updated_at': 1000, 'children': [{'name': 'k\x9bid', 'running': True, 'started_at': 900}]})
    assert select_wakeup(tmp_path, frozenset(), 30, 1000) == 'quiet-child k?id'
    with pytest.raises(InboxUsage, match='needs a fresh broker children.json'):
        select_wakeup(tmp_path, None, 1, 2000)


def test_settle_extends_on_new_arrival_and_caps_at_5s(tmp_path):
    from jailbee.inbox.waiting import settle

    settings = Settings(tmp_path, tmp_path / 'inbox', tmp_path / 'outbox', 1, 1)
    settings.inbox.mkdir()
    now = 0.0
    arrivals = iter([0.8, 1.6, 2.4, 3.2, 4.0, 4.8])
    next_arrival = next(arrivals, None)

    def clock():
        return now

    def sleep(seconds):
        nonlocal now, next_arrival
        now += seconds
        if next_arrival is not None and now + 1e-9 >= next_arrival:
            put(settings.inbox / f'{next_arrival}.json', {})
            next_arrival = next(arrivals, None)

    settle(settings, 100, clock=clock, sleep=sleep)
    assert now == pytest.approx(5)
    assert next_arrival is None
