"""Live broker queries through the per-container socket boundary."""
# ruff: noqa: F811 - fixture imported from the sibling test module

import queue
import threading

import pytest

from jailbee.orchestration import wire
from tests.test_orchestration_listener import _connect_long, recv_line
from tests.test_orchestration_query import Env, env  # noqa: F401 - shared fixture


def test_slow_live_queries_leave_store_workers_available(env: Env) -> None:
    from jailbee.orchestration.live_query import LiveQuery

    entered, unblock = threading.Event(), threading.Event()

    def blocked(uuid, view):
        entered.set()
        assert unblock.wait(10)
        return {"caller": uuid}

    env.queries.live = {"host.status": LiveQuery(blocked, {"caller": "all"})}
    env.put("ordinary-message")
    clients = []
    try:
        for uuid in ("proj-former", "proj-kid", "proj-orch", "unknown"):
            for _ in range(2):
                client = _connect_long(env.rig.root / uuid)
                clients.append(client)
                client.sendall(wire.encode_request(wire.Request("host.status")))
        assert entered.wait(2)
        for client in clients[:6]:
            client.settimeout(0.05)
            with pytest.raises(TimeoutError):
                client.recv(1)
        with _connect_long(env.rig.root / "proj-orch") as ordinary:
            ordinary.settimeout(1)
            ordinary.sendall(wire.encode_request(wire.Request("read", {"id": "ordinary-message"})))
            response = wire.decode_response(recv_line(ordinary))
            assert response.ok
            assert response.result["body"] == "ordinary-message"
    finally:
        unblock.set()
        for client in clients:
            client.close()


def test_live_budget_survives_disconnect_and_resumes_siblings(env: Env) -> None:
    from jailbee.orchestration.live_query import LiveQuery

    seen = queue.Queue()
    unblock = threading.Event()

    def blocked(uuid, view):
        seen.put(uuid)
        assert unblock.wait(5)
        return {"caller": uuid}

    env.queries.live = {"host.status": LiveQuery(blocked, {"caller": "all"})}
    first = _connect_long(env.rig.root / "proj-kid")
    try:
        first.sendall(wire.encode_request(wire.Request("host.status")))
        assert seen.get(timeout=2) == "proj-kid"
        first.close()
        with _connect_long(env.rig.root / "proj-kid") as sibling:
            sibling.sendall(wire.encode_request(wire.Request("host.status")))
            sibling.settimeout(0.1)
            with pytest.raises(TimeoutError):
                sibling.recv(1)
            assert seen.empty()
            unblock.set()
            sibling.settimeout(3)
            assert wire.decode_response(recv_line(sibling)).result == {"caller": "proj-kid"}
            assert seen.get(timeout=2) == "proj-kid"
    finally:
        unblock.set()
        first.close()


@pytest.mark.parametrize("uuid", ["proj-former", "proj-kid", "proj-orch"])
def test_host_status_is_live_for_every_container_role(
    env: Env, monkeypatch: pytest.MonkeyPatch, uuid: str
) -> None:
    from jailbee import sysstat
    from jailbee.orchestration.live_query import host_queries

    now = [100.0]
    monkeypatch.setattr("jailbee.orchestration.live_query.monotonic", lambda: now[0], raising=False)
    env.queries.live = host_queries(env.rig.incus)
    monkeypatch.setattr(sysstat, "read_load", lambda: sysstat.LoadStatus(2.5, 8))
    monkeypatch.setattr(sysstat, "read_memory", lambda: sysstat.MemoryStatus(16000, 6000, 0))
    inventory = [
        {
            "name": name,
            "status": status,
            "config": {"volatile.uuid": name, "user.jailbee.repo_dir": "/work/repo"},
            "profiles": ["proj-base"] if name != "unmanaged" else ["default"],
            "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "work"}},
        }
        for name, status in [
            ("proj-orch", "Running"),
            ("proj-kid", "Running"),
            ("proj-former", "Stopped"),
            ("unmanaged", "Running"),
        ]
    ]
    inventory.append({"status": "Running", "profiles": ["unrelated-base"], "config": {}})
    env.rig.incus.reset_mock()
    env.rig.incus.list_containers.return_value = inventory
    env.rig.incus.storage_pool_resources.return_value = {"space": {"total": 10000, "used": 2500}}
    result = env.call("host.status", uuid=uuid)
    assert result.ok, result
    assert result.result == {
        "load1": 2.5,
        "cpus": 8,
        "memory_total": 16000,
        "memory_available": 6000,
        "storage_pool": "work",
        "disk_available": 7500,
        "running_containers": 2,
    }
    assert env.call("host.status", uuid="proj-kid").ok
    assert env.rig.incus.list_containers.call_count == 1
    assert env.rig.incus.storage_pool_resources.call_count == 1
    now[0] += 2.1
    inventory[2]["status"] = "Running"
    monkeypatch.setattr(sysstat, "read_load", lambda: sysstat.LoadStatus(4.0, 8))
    env.rig.incus.storage_pool_resources.return_value = {"space": {"total": 10000, "used": 3000}}
    updated = env.call("host.status", uuid=uuid)
    assert updated.result["load1"] == 4.0
    assert updated.result["disk_available"] == 7000
    assert updated.result["running_containers"] == 3
    assert env.memory.requests.empty()  # live queries do not wait on the executor
    env.rig.incus.list_containers.assert_called_with(fast=True, all_projects=True, timeout=10)
    env.rig.incus.storage_pool_resources.assert_called_with("work", timeout=10)


@pytest.mark.parametrize("source", ["inventory", "pool"])
def test_concurrent_status_queries_share_incus_readings(env: Env, monkeypatch, source: str) -> None:
    from jailbee.orchestration.live_query import host_queries

    env.queries.live = host_queries(env.rig.incus)
    inventory = [
        {
            "config": {"volatile.uuid": uuid},
            "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "work"}},
        }
        for uuid in ("proj-orch", "proj-kid")
    ]
    env.rig.incus.reset_mock()
    env.rig.incus.list_containers.return_value = inventory
    env.rig.incus.storage_pool_resources.return_value = {"space": {"total": 10, "used": 3}}
    entered, unblock = threading.Event(), threading.Event()
    operation = (
        env.rig.incus.list_containers
        if source == "inventory"
        else env.rig.incus.storage_pool_resources
    )
    value = operation.return_value
    now = [100.0]
    monkeypatch.setattr("jailbee.orchestration.live_query.monotonic", lambda: now[0])

    def blocked(*args, **kwargs):
        entered.set()
        assert unblock.wait(5)
        now[0] += 10.0
        return value

    operation.side_effect = blocked
    with (
        _connect_long(env.rig.root / "proj-orch") as first,
        _connect_long(env.rig.root / "proj-kid") as second,
    ):
        try:
            first.sendall(wire.encode_request(wire.Request("host.status")))
            assert entered.wait(2)
            second.sendall(wire.encode_request(wire.Request("host.status")))
            second.settimeout(0.1)
            with pytest.raises(TimeoutError):
                second.recv(1)
        finally:
            unblock.set()
        second.settimeout(3)
        assert wire.decode_response(recv_line(first)).result["disk_available"] == 7
        assert wire.decode_response(recv_line(second)).result["disk_available"] == 7
    assert env.rig.incus.list_containers.call_count == 1
    assert env.rig.incus.storage_pool_resources.call_count == 1


def test_slow_new_pool_is_coalesced_after_inventory_cache_expires(env: Env, monkeypatch) -> None:
    from jailbee.orchestration.live_query import host_queries

    now = [100.0]
    monkeypatch.setattr("jailbee.orchestration.live_query.monotonic", lambda: now[0])
    env.queries.live = host_queries(env.rig.incus)
    env.rig.incus.list_containers.return_value = [
        {
            "config": {"volatile.uuid": uuid},
            "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": pool}},
        }
        for uuid, pool in (("proj-orch", "work"), ("proj-kid", "other"), ("proj-former", "other"))
    ]
    env.rig.incus.storage_pool_resources.return_value = {"space": {"total": 10, "used": 3}}
    assert env.call("host.status").ok
    entered, unblock = threading.Event(), threading.Event()

    def slow_pool(pool, **kwargs):
        entered.set()
        assert unblock.wait(5)
        now[0] += 10.0
        return {"space": {"total": 20, "used": 4}}

    env.rig.incus.storage_pool_resources.side_effect = slow_pool
    with (
        _connect_long(env.rig.root / "proj-kid") as first,
        _connect_long(env.rig.root / "proj-former") as second,
    ):
        try:
            first.sendall(wire.encode_request(wire.Request("host.status")))
            assert entered.wait(2)
            second.sendall(wire.encode_request(wire.Request("host.status")))
            second.settimeout(0.1)
            with pytest.raises(TimeoutError):
                second.recv(1)
        finally:
            unblock.set()
        second.settimeout(3)
        assert wire.decode_response(recv_line(first)).result["disk_available"] == 16
        assert wire.decode_response(recv_line(second)).result["disk_available"] == 16
    assert env.rig.incus.storage_pool_resources.call_count == 2


@pytest.mark.parametrize(
    "uuid,visible", [("proj-former", False), ("proj-kid", False), ("proj-orch", True)]
)
def test_live_query_field_policy_fails_closed(env: Env, uuid: str, visible: bool) -> None:
    from jailbee.orchestration.live_query import LiveQuery

    env.queries.live = {
        "host.status": LiveQuery(
            lambda caller, view: {"public": caller, "private": "secret", "undeclared": "secret"},
            {"public": "all", "private": "orchestrator"},
        )
    }
    response = env.call("host.status", uuid=uuid)
    assert response.ok
    assert response.result == (
        {"public": uuid, "private": "secret"} if visible else {"public": uuid}
    )
    assert env.call("host.status", uuid="unknown").error_code == wire.STORE
    assert env.call("host.status", uuid=uuid, fields=["private"]).error_code == wire.BAD_REQUEST


@pytest.mark.parametrize("source", ["inventory", "pool"])
@pytest.mark.parametrize("timed_out", [False, True])
def test_expected_incus_failures_return_clear_error_without_traceback(
    env: Env, caplog, source: str, timed_out: bool
) -> None:
    from jailbee.incus import IncusError, IncusTimeoutError
    from jailbee.orchestration.live_query import host_queries

    env.queries.live = host_queries(env.rig.incus)
    env.rig.incus.list_containers.return_value = [
        {
            "config": {"volatile.uuid": "proj-orch"},
            "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "work"}},
        }
    ]
    error = IncusTimeoutError if timed_out else IncusError
    operation = (
        env.rig.incus.list_containers
        if source == "inventory"
        else env.rig.incus.storage_pool_resources
    )
    operation.side_effect = error("Incus unavailable\nsecond line")
    response = env.call("host.status")
    assert response.error_code == wire.REFUSED
    assert response.error_detail == "host status unavailable: Incus unavailable second line"
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert record.exc_info is None
    assert "Incus unavailable second line" in record.getMessage()
    assert "\n" not in record.getMessage()


@pytest.mark.parametrize("source", ["inventory", "pool"])
def test_incus_failures_are_coalesced_then_retried_after_ttl(env: Env, monkeypatch, source) -> None:
    from jailbee.incus import IncusTimeoutError
    from jailbee.orchestration.live_query import host_queries

    now = [100.0]
    monkeypatch.setattr("jailbee.orchestration.live_query.monotonic", lambda: now[0])
    env.queries.live = host_queries(env.rig.incus)
    env.rig.incus.reset_mock()
    env.rig.incus.list_containers.return_value = [
        {
            "config": {"volatile.uuid": "proj-orch"},
            "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "work"}},
        }
    ]
    env.rig.incus.storage_pool_resources.return_value = {"space": {"total": 10, "used": 3}}
    operation = (
        env.rig.incus.list_containers
        if source == "inventory"
        else env.rig.incus.storage_pool_resources
    )
    operation.side_effect = IncusTimeoutError("timed out")
    for _ in range(2):
        assert env.call("host.status").error_code == wire.REFUSED
    assert operation.call_count == 1
    operation.side_effect = None
    now[0] += 2.1
    response = env.call("host.status")
    assert response.ok
    assert response.result["disk_available"] == 7
    assert operation.call_count == 2


def test_caller_absent_from_inventory_is_retryable(env: Env) -> None:
    from jailbee.orchestration.live_query import host_queries

    env.queries.live = host_queries(env.rig.incus)
    env.rig.incus.list_containers.return_value = []
    response = env.call("host.status")
    assert response.error_code == wire.STORE
    assert response.error_detail == "calling container is no longer listed; retry shortly"


def test_missing_live_handler_is_refused_not_retried(env: Env) -> None:
    response = env.call("host.status")
    assert response.error_code == wire.REFUSED
    assert response.error_detail == "live query is not supported by this broker"


def test_host_status_keeps_cpu_count_when_proc_readings_are_unavailable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee import sysstat
    from jailbee.orchestration.live_query import host_queries

    env.queries.live = host_queries(env.rig.incus)
    env.rig.incus.list_containers.return_value = [
        {"config": {"volatile.uuid": "proj-orch"}, "profiles": None, "expanded_devices": None}
    ]
    monkeypatch.setattr(sysstat, "read_load", lambda: None)
    monkeypatch.setattr(sysstat, "read_memory", lambda: None)
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    response = env.call("host.status")
    assert response.ok
    assert response.result == {
        "load1": None,
        "cpus": 4,
        "memory_total": None,
        "memory_available": None,
        "storage_pool": None,
        "disk_available": None,
        "running_containers": 0,
    }
