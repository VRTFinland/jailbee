"""Lifecycle guard at broker.tick and the public state-store boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import broker, broker_state
from jailbee.orchestration.store import open_state_store
from jailbee.orchestration.journal import Journal

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
FULL = "proj-gone"
UUID = "uuid-gone"


def test_idle_round_forgets_vanished_probe_and_diagnostic_state(tmp_path, monkeypatch):
    """The no-work early return must clean caches too, not just busy rounds."""
    memory = broker_state.BrokerMemory()
    memory.status.host_due(FULL, 1.0)
    memory.status.exec_due(FULL, 1.0)
    memory.status.sampler_for(FULL)
    memory.warn_once(f"env:{FULL}", "old instance failed")
    memory.warn_once("repo-load:proj", "repo config failed")
    memory.failures[f"serve:{FULL}"] = "old failure"
    memory.reassert.add(UUID)  # bind/attach failed before attached was recorded
    incus = MagicMock()
    incus.list_containers.return_value = []
    monkeypatch.setattr(broker, "load_global_config", lambda _: (GlobalConfig(), []))

    assert not broker.tick(incus, root=tmp_path, journal=Journal(tmp_path), now=NOW, memory=memory)

    assert memory.status.host_at == {}
    assert memory.status.exec_at == {}
    assert memory.status.samplers == {}
    assert memory.warned == {"repo-load:proj"}
    assert memory.failures == {}
    assert memory.reassert == set()


def test_vanished_publisher_drops_dedup_and_rate_budget(tmp_path):
    store = open_state_store(tmp_path)
    store.kv_put("publish-accepted.json", {UUID: ["old-publish"]})
    store.kv_put("publish-budget.json", {FULL: [NOW.timestamp()]})
    incus = MagicMock()
    incus.list_containers.return_value = []
    memory = broker_state.BrokerMemory()

    assert not broker.tick(incus, root=tmp_path, journal=Journal(tmp_path), now=NOW, memory=memory)

    assert store.kv_get("publish-accepted.json") == {}
    assert store.kv_get("publish-budget.json") == {}
