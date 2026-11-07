"""D2: after `jb new` attaches a socket directory, the broker binds it within seconds.

`socket_device.attach_new` creates the directory at create time; only a broker
round binds the listener in it. While such a directory is fresh and unserved the
broker ends its idle wait early (no sooner than `ACTIVE_INTERVAL` after the last
round), so staging right after boot fits the client's 10 s connect retry.
"""

from __future__ import annotations

import os
import queue
import time
from pathlib import Path
from unittest.mock import MagicMock

from jailbee.orchestration import broker, broker_state, executor, socket_device, socket_lifecycle

UUID = "0123-uuid"


def _memory(served: dict[str, Path] | None = None) -> broker_state.BrokerMemory:
    memory = broker_state.BrokerMemory()
    listener = MagicMock()
    listener.socket_dir.side_effect = lambda uuid: (served or {}).get(uuid)
    memory.listener = listener
    return memory


def _attached(root: Path) -> Path:
    incus = MagicMock()
    incus.config_get.return_value = UUID
    path = socket_device.attach_new(incus, "proj-x", root)
    assert path is not None
    return path


def test_a_fresh_attached_directory_is_unserved_until_the_listener_binds_it(tmp_path):
    path = _attached(tmp_path)
    assert socket_lifecycle.unserved_new_dir(tmp_path, _memory(), now=time.time())
    served = _memory({UUID: path})
    assert not socket_lifecycle.unserved_new_dir(tmp_path, served, now=time.time())


def test_an_old_unserved_directory_stops_waking_the_broker(tmp_path):
    path = _attached(tmp_path)
    old = time.time() - socket_lifecycle.NEW_DIR_GRACE_SECONDS - 1
    os.utime(path, (old, old))
    assert not socket_lifecycle.unserved_new_dir(tmp_path, _memory(), now=time.time())


def test_no_socket_root_is_nothing_to_wake_for(tmp_path):
    assert not socket_lifecycle.unserved_new_dir(tmp_path, _memory(), now=time.time())


def test_the_wake_waits_an_active_interval_after_the_round_and_throttles_scans(tmp_path):
    _attached(tmp_path)
    now = [100.0]
    wake = broker.new_socket_wake(tmp_path, _memory(), 100.0, clock=lambda: now[0])
    assert not wake()  # the round just ran
    now[0] = 100.0 + broker.ACTIVE_INTERVAL
    assert wake()
    assert not wake()  # scanned this slice already
    now[0] += broker.SLEEP_SLICE
    assert wake()


def test_serve_requests_returns_early_on_wake():
    memory = broker_state.BrokerMemory()
    memory.requests = queue.Queue()
    ticks = iter([0.0, 0.0, 0.0])
    calls = []

    def wake() -> bool:
        calls.append(1)
        return True

    executor.serve_requests(
        memory,
        until=1e9,
        stop=broker_state.StopFlag(),
        clock=lambda: next(ticks, 0.0),
        wake=wake,
    )
    assert calls == [1]
