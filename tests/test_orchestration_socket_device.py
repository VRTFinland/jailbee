from __future__ import annotations

import logging
import queue
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from jailbee.incus import ORCHESTRATION_DEVICE, IncusError
from jailbee.orchestration import broker, labels, socket_device
from jailbee.orchestration.store import BrokerStore, store_path
from tests.test_orchestration_broker import GRANT, Rig, _c

if TYPE_CHECKING:
    from pathlib import Path

UUID = "uuid-orch"


class FakeListener:
    def __init__(self) -> None:
        self.open: set[str] = set()
        self.created = True
        self.opened: list[str] = []
        self.closed: list[str] = []

    def has_container(self, uuid: str) -> bool:
        return uuid in self.open

    def open_container(self, uuid: str, sock_dir: Path) -> bool:
        self.open.add(uuid)
        self.opened.append(uuid)
        return self.created

    def close_container(self, uuid: str) -> None:
        self.open.discard(uuid)
        self.closed.append(uuid)


class Env:
    """A Rig whose incus keeps per-instance devices and shows them in `list`."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.devices: dict[str, dict[str, dict[str, str]]] = {}
        self.entries: list[dict[str, Any]] = []
        self.add = MagicMock(side_effect=self._add)
        self.remove = MagicMock(side_effect=self._remove)
        inc = rig.incus
        inc.list_containers.side_effect = self._list
        inc.config_device_get.side_effect = lambda n, d, k: (
            self.devices.get(n, {}).get(d, {}).get(k)
        )
        inc.config_device_add = self.add
        inc.config_device_remove = self.remove
        self.listener = FakeListener()
        self.store = BrokerStore(store_path(rig.root))
        self.memory = broker.BrokerMemory(
            listener=self.listener,  # type: ignore[arg-type]  # fake
            store=self.store,
        )

    def _list(self) -> list[dict[str, Any]]:
        out = []
        for e in self.entries:
            out.append({**e, "devices": dict(self.devices.get(e["name"], {}))})
        return out

    def _add(self, name: str, dev: str, typ: str, props: dict[str, str]) -> None:
        self.devices.setdefault(name, {})[dev] = {"type": typ, **props}

    def _remove(self, name: str, dev: str, *, missing_ok: bool = False) -> None:
        self.devices.get(name, {}).pop(dev, None)

    def set(self, *entries: dict[str, Any]) -> None:
        self.entries = list(entries)
        self.rig.incus.config_get_checked.side_effect = lambda full, key: next(
            (e["config"].get(key) for e in self.entries if e["name"] == full), None
        )

    def tick(self) -> None:
        self.rig.tick(self.memory)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(Rig(tmp_path, monkeypatch))


def orch(uuid: str = UUID, **extra: str) -> dict[str, Any]:
    return _c("proj-orch", **{labels.GRANT: GRANT.to_label(), "volatile.uuid": uuid, **extra})


def kid(uuid: str = "uuid-kid") -> dict[str, Any]:
    return _c("proj-kid", **{labels.SPAWNED_BY: "proj-orch", "volatile.uuid": uuid})


def adds(env: Env) -> list[str]:
    return [c.args[0] for c in env.add.call_args_list]


def test_unit_helpers(tmp_path: Path) -> None:
    cfg = socket_device.device_config(tmp_path, "u1")
    assert cfg["source"] == str(tmp_path / "sock" / "u1")
    assert cfg["path"] == "/run/jailbee/orchestration"
    assert cfg["readonly"] == "true" and cfg["required"] == "false"
    entry = {"config": {"volatile.uuid": "u1"}, "devices": {ORCHESTRATION_DEVICE: cfg}}
    assert not socket_device.mismatched(entry)
    entry["config"] = {"volatile.uuid": "u2"}
    assert socket_device.mismatched(entry)
    assert not socket_device.mismatched({"config": {"volatile.uuid": "u2"}, "devices": {}})


@pytest.mark.parametrize(
    "make,name,uuid", [(orch, "proj-orch", UUID), (kid, "proj-kid", "uuid-kid")]
)
def test_role_opens_listener_attaches_device_and_records_recipient(env, make, name, uuid) -> None:
    env.set(orch(), kid()) if name == "proj-kid" else env.set(make())
    env.tick()
    assert uuid in env.listener.opened
    dev = env.devices[name][ORCHESTRATION_DEVICE]
    assert dev["source"] == str(env.rig.root / "sock" / uuid)
    assert env.store.recipients()[name] == uuid


def test_second_round_does_not_readd(env) -> None:
    env.set(orch())
    env.tick()
    env.tick()
    assert adds(env) == ["proj-orch"]
    assert env.listener.opened == [UUID]


def test_created_dir_reasserts_device(env) -> None:
    env.set(orch())
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": str(env.rig.root / "sock" / UUID)}}
    env.tick()
    assert adds(env) == ["proj-orch"]  # created=True: remove + add despite same source
    assert env.remove.called


def test_existing_dir_with_correct_device_is_left_alone(env) -> None:
    env.listener.created = False
    env.set(orch())
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": str(env.rig.root / "sock" / UUID)}}
    env.tick()
    assert adds(env) == []
    assert env.store.recipients() == {"proj-orch": UUID}


def test_role_end_removes_everything(env) -> None:
    env.set(orch())
    env.tick()
    env.set(_c("proj-orch", **{"volatile.uuid": UUID}))
    env.tick()
    assert "proj-orch" not in env.devices or ORCHESTRATION_DEVICE not in env.devices["proj-orch"]
    assert env.listener.closed == [UUID]
    assert env.store.recipients() == {}
    assert env.memory.attached == {}


def test_mismatched_source_is_removed_and_reattached(env) -> None:
    env.set(orch())
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": "/x/sock/other"}}
    env.tick()
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"].endswith(UUID)


def test_mismatched_on_unlabelled_container_is_removed(env) -> None:
    env.set(_c("proj-copy", **{"volatile.uuid": "new"}))
    env.devices["proj-copy"] = {ORCHESTRATION_DEVICE: {"source": "/x/sock/old"}}
    env.tick()
    assert ORCHESTRATION_DEVICE not in env.devices["proj-copy"]


def test_vanished_device_is_reattached(env) -> None:
    env.set(orch())
    env.tick()
    env.devices["proj-orch"].clear()  # e.g. a snapshot restore
    env.tick()
    assert adds(env) == ["proj-orch", "proj-orch"]


def test_add_failure_warns_once_and_retries(env, caplog: pytest.LogCaptureFixture) -> None:
    env.add.side_effect = IncusError("boom")
    env.set(orch())
    env.tick()
    env.tick()
    assert env.add.call_count == 2
    assert env.store.recipients() == {}
    assert len([r for r in caplog.records if "broker socket" in r.getMessage()]) == 1
    env.add.side_effect = env._add
    with caplog.at_level(logging.WARNING):
        env.tick()
    assert env.store.recipients() == {"proj-orch": UUID}


def test_no_volatile_uuid_skips_socket(env) -> None:
    e = orch()
    del e["config"]["volatile.uuid"]
    env.set(e)
    env.tick()
    assert env.listener.opened == [] and adds(env) == []
    assert env.store.recipients() == {}


def test_memory_has_request_queue() -> None:
    assert isinstance(broker.BrokerMemory().requests, queue.Queue)
