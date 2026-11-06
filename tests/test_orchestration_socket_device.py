from __future__ import annotations

import json
import logging
import queue
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from jailbee.incus import ORCHESTRATION_DEVICE, IncusError
from jailbee.orchestration import broker, labels, socket_device, state
from jailbee.orchestration.store import BrokerStore, store_path
from tests.test_orchestration_broker import GRANT, Rig, _c

if TYPE_CHECKING:
    from pathlib import Path

UUID = "uuid-orch"


class FakeListener:
    def __init__(self) -> None:
        self.open: set[str] = set()
        self.dirs: dict[str, Path] = {}
        self.created = True
        self.fail_open_after_mkdir = False
        self.fail_rotate = False
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.rotated: list[tuple[str, Path]] = []

    def has_container(self, uuid: str) -> bool:
        return uuid in self.open

    def socket_dir(self, uuid: str) -> Path | None:
        return self.dirs.get(uuid)

    def open_container(self, uuid: str, sock_dir: Path) -> bool:
        if self.fail_open_after_mkdir:
            sock_dir.mkdir(parents=True, exist_ok=True)
            raise OSError("bind failed")
        sock_dir.mkdir(parents=True, exist_ok=True)  # like the real one
        self.open.add(uuid)
        self.dirs[uuid] = sock_dir
        self.opened.append(uuid)
        return self.created

    def close_container(self, uuid: str) -> None:
        self.open.discard(uuid)
        old = self.dirs.pop(uuid, None)
        if old is not None and old.exists():
            old.rmdir()
        self.closed.append(uuid)

    def rotate_container(self, uuid: str, sock_dir: Path) -> None:
        if self.fail_rotate or uuid not in self.open:
            raise OSError("rotate failed")
        self.dirs[uuid] = sock_dir
        self.rotated.append((uuid, sock_dir))


class Env:
    """A Rig whose incus keeps per-instance devices and shows them in `list`."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.devices: dict[str, dict[str, dict[str, str]]] = {}
        self.entries: list[dict[str, Any]] = []
        # Instances of other Incus projects: only `list --all-projects` shows them.
        self.elsewhere: list[dict[str, Any]] = []
        self.add = MagicMock(side_effect=self._add)
        self.remove = MagicMock(side_effect=self._remove)
        inc = rig.incus
        inc.list_containers.side_effect = self._list
        inc.current_project.return_value = "default"
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

    def _list(self, *, fast: bool = False, all_projects: bool = False) -> list[dict[str, Any]]:
        out = []
        for e in self.entries:
            out.append(
                {**e, "project": "default", "devices": dict(self.devices.get(e["name"], {}))}
            )
        if all_projects:
            for e in self.elsewhere:
                key = f"{e['project']}/{e['name']}"
                out.append({**e, "devices": dict(self.devices.get(key, {}))})
        return out

    def _add(self, name: str, dev: str, typ: str, props: dict[str, str]) -> None:
        self.devices.setdefault(name, {})[dev] = {"type": typ, **props}

    def _remove(
        self, name: str, dev: str, *, missing_ok: bool = False, project: str | None = None
    ) -> None:
        key = f"{project}/{name}" if project else name
        self.devices.get(key, {}).pop(dev, None)

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
    cfg = socket_device.device_config(tmp_path / "sock" / "u1.ab12")
    assert cfg["source"] == str(tmp_path / "sock" / "u1.ab12")
    assert cfg["path"] == "/run/jailbee/orchestration"
    assert cfg["readonly"] == "true" and cfg["required"] == "false"
    entry = {"config": {"volatile.uuid": "u1"}, "devices": {ORCHESTRATION_DEVICE: cfg}}
    assert not socket_device.mismatched(entry)
    entry["config"] = {"volatile.uuid": "u2"}
    assert socket_device.mismatched(entry)
    assert not socket_device.mismatched({"config": {"volatile.uuid": "u2"}, "devices": {}})
    # the legacy, generation-less directory name still names its uuid
    legacy = {
        "config": {"volatile.uuid": "u1"},
        "devices": {ORCHESTRATION_DEVICE: {"source": str(tmp_path / "sock" / "u1")}},
    }
    assert socket_device.source_uuid(legacy) == "u1" and not socket_device.mismatched(legacy)


def test_fresh_directories_never_repeat(tmp_path: Path) -> None:
    a = socket_device.fresh_sock_dir(tmp_path, "u1")
    b = socket_device.fresh_sock_dir(tmp_path, "u1")
    assert a != b and a.parent == b.parent == tmp_path / "sock"
    assert socket_device.dir_uuid(a) == "u1"


@pytest.mark.parametrize(
    "make,name,uuid", [(orch, "proj-orch", UUID), (kid, "proj-kid", "uuid-kid")]
)
def test_role_opens_listener_attaches_device_and_records_recipient(env, make, name, uuid) -> None:
    env.set(orch(), kid()) if name == "proj-kid" else env.set(make())
    env.tick()
    assert uuid in env.listener.opened
    dev = env.devices[name][ORCHESTRATION_DEVICE]
    assert dev["source"] == str(env.listener.dirs[uuid])
    assert env.listener.dirs[uuid].parent == env.rig.root / "sock"
    assert socket_device.dir_uuid(env.listener.dirs[uuid]) == uuid
    assert env.store.recipients()[name] == uuid


def test_second_round_does_not_readd(env) -> None:
    env.set(orch())
    env.tick()
    env.rig.incus.config_device_get.reset_mock()
    env.tick()
    assert adds(env) == ["proj-orch"]
    assert env.listener.opened == [UUID]
    # a settled container costs no per-round incus call
    env.rig.incus.config_device_get.assert_not_called()


def test_created_dir_reasserts_device(env) -> None:
    env.set(orch())
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": str(env.rig.root / "sock" / UUID)}}
    env.tick()
    assert adds(env) == ["proj-orch"]  # created=True: remove + add despite same source
    assert env.remove.called


def test_existing_dir_with_correct_device_is_left_alone(env) -> None:
    """A broker restart keeps the directory the device records: no device churn."""
    env.listener.created = False
    env.set(orch())
    recorded = env.rig.root / "sock" / f"{UUID}.0f0f"
    recorded.mkdir(parents=True)
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": str(recorded)}}
    env.tick()
    assert adds(env) == []
    assert env.listener.dirs[UUID] == recorded
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
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(env.listener.dirs[UUID])


def test_foreign_device_on_a_served_container_is_replaced(env) -> None:
    env.set(orch())
    env.tick()
    assert adds(env) == ["proj-orch"]
    env.listener.created = False
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": "/x/sock/other"}}
    env.tick()
    assert adds(env) == ["proj-orch", "proj-orch"]
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(env.listener.dirs[UUID])


def _copy_of_the_source(env: Env) -> str:
    """A raw `incus copy` of the served orchestrator, carrying its device."""
    src = env.devices["proj-orch"][ORCHESTRATION_DEVICE]
    env.set(orch(), _c("proj-copy", **{"volatile.uuid": "uuid-copy"}))
    env.devices["proj-copy"] = {ORCHESTRATION_DEVICE: dict(src)}
    return str(src["source"])


def test_copy_moves_the_source_to_a_fresh_directory_even_after_a_clean_detach(env) -> None:
    """Incus may detach lazily (the copy holds the directory): never trust the detach."""
    env.set(orch())
    env.tick()
    old = _copy_of_the_source(env)
    env.tick()
    assert ORCHESTRATION_DEVICE not in env.devices["proj-copy"]
    assert [u for u, _ in env.listener.rotated] == [UUID]
    new = env.listener.dirs[UUID]
    assert str(new) != old and socket_device.dir_uuid(new) == UUID
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(new)
    assert env.memory.attached == {"proj-orch": UUID}


def test_copy_cannot_reach_the_source_when_the_detach_fails(env) -> None:
    env.set(orch())
    env.tick()

    def remove(name: str, dev: str, *, missing_ok: bool = False) -> None:
        if name == "proj-copy":
            raise IncusError("EBUSY")
        env._remove(name, dev, missing_ok=missing_ok)

    env.remove.side_effect = remove
    old = _copy_of_the_source(env)
    env.tick()
    assert env.devices["proj-copy"][ORCHESTRATION_DEVICE]["source"] == old  # still there
    assert str(env.listener.dirs[UUID]) != old  # but it points at a dead directory
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(env.listener.dirs[UUID])
    env.tick()  # the stale device is no threat: no second rotation
    assert len(env.listener.rotated) == 1


def test_a_failed_rotation_closes_the_source_s_socket(env) -> None:
    env.set(orch())
    env.tick()
    env.listener.fail_rotate = True
    _copy_of_the_source(env)
    env.tick()
    assert UUID in env.listener.closed
    assert env.listener.rotated == []


def _restart_broker(env: Env) -> None:
    """A new broker process: empty memory and listener, directories left on disk."""
    env.listener = FakeListener()
    env.memory = broker.BrokerMemory(
        listener=env.listener,  # type: ignore[arg-type]  # fake
        store=env.store,
    )


def test_a_copy_found_after_a_broker_restart_kills_the_directory_it_mounts(env) -> None:
    """The copy may hold the directory (lazy detach): it must never be served again."""
    env.set(orch())
    env.tick()
    old = env.listener.dirs[UUID]
    _restart_broker(env)
    _copy_of_the_source(env)
    env.tick()
    assert ORCHESTRATION_DEVICE not in env.devices["proj-copy"]
    assert not old.exists()
    assert env.listener.dirs[UUID] != old
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(env.listener.dirs[UUID])


@pytest.mark.parametrize("failure", ["list", "project"])
def test_a_restart_does_not_reuse_an_unverified_directory(env, failure) -> None:
    env.set(orch())
    env.tick()
    old = env.listener.dirs[UUID]
    src = dict(env.devices["proj-orch"][ORCHESTRATION_DEVICE])
    env.elsewhere = [{**_c("proj-copy", **{"volatile.uuid": "uuid-copy"}), "project": "other"}]
    env.devices["other/proj-copy"] = {ORCHESTRATION_DEVICE: src}
    _restart_broker(env)
    if failure == "list":
        def listing(*, fast=False, all_projects=False):
            if all_projects:
                raise IncusError("cannot list projects")
            return env._list(fast=fast)

        env.rig.incus.list_containers.side_effect = listing
    else:
        env.rig.incus.current_project.return_value = ""
    env.tick()
    assert env.listener.dirs[UUID] != old
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(env.listener.dirs[UUID])
    assert env.devices["other/proj-copy"][ORCHESTRATION_DEVICE]["source"] == str(old)
    env.tick()
    assert env.listener.opened == [UUID]  # no churn once the fresh listener is live


def test_a_failed_copy_directory_removal_never_allows_reuse(env, monkeypatch) -> None:
    env.set(orch())
    env.tick()
    old = env.listener.dirs[UUID]
    _restart_broker(env)
    _copy_of_the_source(env)
    monkeypatch.setattr(socket_device, "remove_dir", MagicMock(side_effect=OSError("EBUSY")))
    env.tick()
    assert old.exists()  # the copy may still hold it after a lazy detach
    assert env.listener.dirs[UUID] != old
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(env.listener.dirs[UUID])


def test_a_restart_reuses_no_legacy_directory(env) -> None:
    """The old broker reset the socket in place: a copy may hold `sock/<uuid>`."""
    legacy = env.rig.root / "sock" / UUID
    legacy.mkdir(parents=True)
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": str(legacy)}}
    env.set(orch())
    env.tick()
    assert env.listener.dirs[UUID] != legacy
    assert not legacy.exists()  # and it is cleaned up
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(env.listener.dirs[UUID])


def test_a_failed_teardown_detach_still_closes_the_socket(env) -> None:
    env.set(orch())
    env.tick()
    env.remove.side_effect = IncusError("EBUSY")
    env.set(_c("proj-orch", **{"volatile.uuid": UUID}))  # role ends
    env.tick()
    assert UUID in env.listener.closed


def test_a_reopened_socket_never_reuses_a_closed_directory(env) -> None:
    """A copy's lazily held mount of the old directory must stay dead after a teardown."""
    env.set(orch())
    env.tick()
    first = env.listener.dirs[UUID]
    env.set(_c("proj-orch", **{"volatile.uuid": UUID}))  # role ends
    env.tick()
    env.set(orch())  # and comes back
    env.tick()
    assert env.listener.dirs[UUID] != first


def test_a_copy_in_another_project_is_stripped_and_the_source_moves(env) -> None:
    """`incus copy --target-project`: invisible to a plain `incus list`."""
    env.set(orch())
    env.tick()
    src = env.devices["proj-orch"][ORCHESTRATION_DEVICE]
    env.elsewhere = [{**_c("proj-orch", **{"volatile.uuid": "uuid-copy"}), "project": "other"}]
    env.devices["other/proj-orch"] = {ORCHESTRATION_DEVICE: dict(src)}
    env.tick()
    assert ORCHESTRATION_DEVICE not in env.devices["other/proj-orch"]
    assert [u for u, _ in env.listener.rotated] == [UUID]
    assert env.devices["proj-orch"][ORCHESTRATION_DEVICE]["source"] == str(env.listener.dirs[UUID])
    assert env.memory.attached == {"proj-orch": UUID}  # the source is still served


def test_a_same_uuid_twin_in_another_project_tears_the_socket_down(env) -> None:
    env.set(orch())
    env.tick()
    src = env.devices["proj-orch"][ORCHESTRATION_DEVICE]
    env.elsewhere = [{**orch(), "project": "other"}]
    env.devices["other/proj-orch"] = {ORCHESTRATION_DEVICE: dict(src)}
    env.tick()
    assert ORCHESTRATION_DEVICE not in env.devices["other/proj-orch"]
    assert ORCHESTRATION_DEVICE not in env.devices["proj-orch"]
    assert UUID in env.listener.closed
    assert env.store.recipients() == {}


def test_an_unrelated_container_in_another_project_is_left_alone(env) -> None:
    env.set(orch())
    env.tick()
    env.elsewhere = [{**_c("proj-x", **{"volatile.uuid": "uuid-x"}), "project": "other"}]
    env.devices["other/proj-x"] = {"eth0": {"type": "nic"}}
    env.remove.reset_mock()
    env.tick()
    env.remove.assert_not_called()
    assert env.listener.rotated == []


def test_mismatched_device_of_an_unserved_uuid_rotates_nothing(env) -> None:
    env.set(_c("proj-copy", **{"volatile.uuid": "new"}))
    env.devices["proj-copy"] = {ORCHESTRATION_DEVICE: {"source": "/x/sock/old"}}
    env.tick()
    assert env.listener.rotated == []


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


def test_reassert_survives_an_attach_failure(env) -> None:
    src = str(env.rig.root / "sock" / UUID)
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": src}}
    env.remove.side_effect = IncusError("boom")
    env.set(orch())
    env.tick()  # created=True, but the remove fails
    assert env.store.recipients() == {}
    env.remove.side_effect = env._remove
    env.listener.created = False
    env.tick()
    assert adds(env) == ["proj-orch"]  # same source, yet still re-asserted
    env.tick()
    env.rig.incus.config_device_get.reset_mock()
    env.tick()
    assert adds(env) == ["proj-orch"]  # and only once
    assert env.memory.reassert == set()


def test_reassert_survives_a_bind_failure(env) -> None:
    src = str(env.rig.root / "sock" / UUID)
    env.devices["proj-orch"] = {ORCHESTRATION_DEVICE: {"source": src}}
    env.listener.fail_open_after_mkdir = True
    env.set(orch())
    env.tick()
    assert env.store.recipients() == {}
    env.listener.fail_open_after_mkdir = False
    env.listener.created = False
    env.tick()
    assert adds(env) == ["proj-orch"]


def test_vanished_container_is_forgotten_without_a_device_call(env) -> None:
    env.set(orch())
    env.tick()
    env.remove.reset_mock()
    env.set()
    env.tick()
    assert env.listener.closed == [UUID]
    assert env.memory.attached == {}
    assert env.store.recipients() == {}
    env.remove.assert_not_called()


def test_recreated_container_closes_the_old_listener(env) -> None:
    env.set(orch("u1"))
    env.tick()
    env.set(orch("u2"))
    env.devices.clear()
    env.tick()
    assert env.listener.closed == ["u1"]
    assert env.listener.open == {"u2"}
    assert env.store.recipients() == {"proj-orch": "u2"}
    assert env.memory.attached == {"proj-orch": "u2"}


def test_recreated_container_is_dropped_even_when_not_served(env) -> None:
    env.set(orch("u1"))
    env.tick()
    env.set(_c("proj-orch", running=False, **{"volatile.uuid": "u2", labels.GRANT: "x"}))
    env.devices.clear()
    env.tick()
    assert "u1" in env.listener.closed
    assert env.store.recipients() == {}
    assert "proj-orch" not in env.memory.attached


def test_unparseable_grant_tears_down_and_exports_unacked_items(env) -> None:
    env.set(orch())
    env.tick()
    env.store.insert_item(
        UUID, "proj-orch", {"type": "message", "id": "m1", "kind": "note"}, now=1.0
    )
    env.store.insert_item(
        UUID, "proj-orch", {"type": "message", "id": "m2", "kind": "note"}, now=2.0
    )
    env.store.ack(UUID, "m1", now=3.0)
    env.store.insert_item(
        UUID, "proj-orch", {"type": "message", "id": "m3", "kind": "note"}, now=4.0
    )
    env.set(_c("proj-orch", **{labels.GRANT: "{not json", "volatile.uuid": UUID}))
    env.tick()
    assert env.store.recipients() == {}
    assert env.listener.closed == [UUID]
    assert ORCHESTRATION_DEVICE not in env.devices.get("proj-orch", {})
    assert env.store.unacked(UUID) == []
    files = sorted(state.pending_dir(env.rig.root, "proj-orch").iterdir())
    assert [f.name.rsplit("-", 1)[-1] for f in files] == ["m2.json", "m3.json"]
    assert json.loads(files[0].read_text())["id"] == "m2"


def test_teardown_keeps_items_when_export_fails(env, monkeypatch) -> None:
    env.set(orch())
    env.tick()
    env.store.insert_item(
        UUID, "proj-orch", {"type": "message", "id": "m1", "kind": "note"}, now=1.0
    )
    monkeypatch.setattr(
        broker, "atomic_write_text", MagicMock(side_effect=OSError("disk full")), raising=False
    )
    env.set(_c("proj-orch", **{labels.GRANT: "{not json", "volatile.uuid": UUID}))
    env.tick()
    assert [i.id for i in env.store.unacked(UUID)] == ["m1"]
    assert env.store.recipients() == {"proj-orch": UUID}


def test_two_instances_with_one_uuid_get_no_socket(env, caplog: pytest.LogCaptureFixture) -> None:
    """E.g. `incus import` of a backup beside the original: one identity, two speakers."""
    twin = _c("proj-twin", **{labels.GRANT: GRANT.to_label(), "volatile.uuid": UUID})
    env.set(orch(), twin)
    with caplog.at_level(logging.WARNING):
        env.tick()
    assert adds(env) == []
    assert env.store.recipients() == {}
    assert "share the instance uuid" in caplog.text


def test_a_twin_appearing_beside_a_served_container_tears_its_socket_down(env) -> None:
    env.set(orch())
    env.tick()
    assert env.store.recipients() == {"proj-orch": UUID}
    src = env.devices["proj-orch"][ORCHESTRATION_DEVICE]
    twin = _c("proj-twin", **{labels.GRANT: GRANT.to_label(), "volatile.uuid": UUID})
    env.devices["proj-twin"] = {ORCHESTRATION_DEVICE: dict(src)}
    env.set(orch(), twin)
    env.tick()
    assert ORCHESTRATION_DEVICE not in env.devices["proj-orch"]
    assert ORCHESTRATION_DEVICE not in env.devices["proj-twin"]
    assert UUID in env.listener.closed
    assert env.store.recipients() == {}
    env.set(orch())  # the twin is gone again: served as before
    env.tick()
    assert env.store.recipients() == {"proj-orch": UUID}


def test_a_twin_does_not_take_the_original_s_unread_messages(env) -> None:
    env.set(orch())
    env.tick()
    env.store.insert_item(
        UUID, "proj-orch", {"type": "message", "id": "m1", "kind": "note"}, now=1.0
    )
    src = env.devices["proj-orch"][ORCHESTRATION_DEVICE]
    twin = _c("proj-a-twin", **{labels.GRANT: GRANT.to_label(), "volatile.uuid": UUID})
    env.devices["proj-a-twin"] = {ORCHESTRATION_DEVICE: dict(src)}
    env.set(twin, orch())  # the twin is listed (and handled) first
    env.tick()
    assert not state.pending_dir(env.rig.root, "proj-a-twin").exists() or not list(
        state.pending_dir(env.rig.root, "proj-a-twin").iterdir()
    )
    files = list(state.pending_dir(env.rig.root, "proj-orch").iterdir())
    assert [f.name.rsplit("-", 1)[-1] for f in files] == ["m1.json"]
