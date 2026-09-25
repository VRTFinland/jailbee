"""Tests for stable work-network reservations."""

from __future__ import annotations

from multiprocessing import get_context
from typing import Any
from unittest.mock import MagicMock

import pytest

from jailbee.work_network import reserve_work_ipv4, work_network_lock, work_nic


def _hold_lock(state_home: str, acquired: Any, release: Any) -> None:
    import os

    os.environ["XDG_STATE_HOME"] = state_home
    with work_network_lock():
        acquired.set()
        release.wait(5)


def _try_lock(state_home: str, queue: Any) -> None:
    import os

    os.environ["XDG_STATE_HOME"] = state_home
    with work_network_lock():
        queue.put(True)


def test_reserves_free_usable_address_excluding_gateway_and_local_devices(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    incus = MagicMock()
    incus.network_get.return_value = "10.42.0.1/29"
    incus.list_containers.return_value = [
        {
            "name": "stopped",
            "devices": {"eth0": {"network": "jailbee-work", "ipv4.address": "10.42.0.2"}},
        }
    ]
    incus.network_leases.return_value = [{"address": "10.42.0.3"}]
    assert reserve_work_ipv4(incus, "new") == "10.42.0.4"


def test_returns_existing_filtered_local_reservation(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    incus = MagicMock()
    incus.network_get.return_value = "10.42.0.1/29"
    incus.list_containers.return_value = [
        {
            "name": "same",
            "devices": {
                "eth0": {
                    "network": "jailbee-work",
                    "ipv4.address": "10.42.0.5",
                    "security.ipv4_filtering": "true",
                }
            },
        }
    ]
    assert reserve_work_ipv4(incus, "same") == "10.42.0.5"


def test_exhaustion_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    incus = MagicMock()
    incus.network_get.return_value = "10.42.0.2/30"
    incus.list_containers.return_value = []
    incus.network_leases.return_value = [{"address": "10.42.0.1"}, {"address": "10.42.0.3"}]
    with pytest.raises(ValueError, match="No free IPv4"):
        reserve_work_ipv4(incus, "new")


def test_work_nic_is_fixed_and_filtered():
    assert work_nic("10.42.0.2", ["strict", "extra"]) == {
        "type": "nic",
        "network": "jailbee-work",
        "ipv4.address": "10.42.0.2",
        "security.ipv4_filtering": "true",
        "security.acls": "strict,extra",
    }


def test_lock_is_exclusive_across_processes(tmp_path):
    context = get_context("spawn")
    acquired, release = context.Event(), context.Event()
    queue = context.Queue()
    first = context.Process(target=_hold_lock, args=(str(tmp_path), acquired, release))
    second = context.Process(target=_try_lock, args=(str(tmp_path), queue))
    first.start()
    assert acquired.wait(5)
    second.start()
    assert queue.empty()
    release.set()
    assert queue.get(timeout=5)
    first.join(5)
    second.join(5)
    assert first.exitcode == 0
    assert second.exitcode == 0
