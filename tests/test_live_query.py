"""Live broker queries through the per-container socket boundary."""
# ruff: noqa: F811 - fixture imported from the sibling test module

import pytest

from jailbee.orchestration import wire
from tests.test_orchestration_query import Env, env  # noqa: F401 - shared fixture


@pytest.mark.parametrize("uuid", ["proj-former", "proj-kid", "proj-orch"])
def test_host_status_is_live_for_every_container_role(
    env: Env, monkeypatch: pytest.MonkeyPatch, uuid: str
) -> None:
    from jailbee import sysstat
    from jailbee.orchestration.live_query import host_queries

    env.queries.live = host_queries(env.rig.incus)
    monkeypatch.setattr(sysstat, "read_load", lambda: sysstat.LoadStatus(2.5, 8))
    monkeypatch.setattr(sysstat, "read_memory", lambda: sysstat.MemoryStatus(16000, 6000, 0))
    inventory = [
        {
            "name": name,
            "status": status,
            "config": {"volatile.uuid": name},
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


@pytest.mark.parametrize("uuid", ["proj-former", "proj-kid", "proj-orch"])
def test_model_map_uses_callers_repo_and_reloads_host_config(env: Env, monkeypatch, uuid) -> None:
    from jailbee.global_config import default_global_config_path
    from jailbee.orchestration.live_query import host_queries
    from jailbee.orchestration.query import ContainerSnapshot
    from tests.conftest import make_config

    root = env.rig.cfg.repo_root
    (root / ".jailbee").mkdir()
    (root / ".jailbee" / "config.yaml").write_text(
        "container_prefix: proj\nagents:\n  models:\n    haiku: {use_for: small fixes}\n"
    )
    global_path = default_global_config_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        "agents:\n  models:\n    haiku: {label: cheap}\n  model_policy: Prefer cheap models.\n"
    )
    # An unrelated caller must not get this repo's overrides.
    other_root = root / "other"
    (other_root / ".jailbee").mkdir(parents=True)
    (other_root / ".jailbee" / "config.yaml").write_text("container_prefix: other\n")
    env.queries.publish_container(
        "unknown", ContainerSnapshot("plain", True, "{}", None, make_config(other_root))
    )
    env.queries.publish_container(
        "proj-former", ContainerSnapshot("plain", True, "{}", None, env.rig.cfg)
    )
    env.queries.live = host_queries(env.rig.incus)
    monkeypatch.setattr("jailbee.config.loader.detect_upstream_remote", lambda *a: None)
    monkeypatch.setattr("jailbee.config.loader.detect_default_branch", lambda *a: "main")

    result = env.call("agent.models", uuid=uuid)
    assert result.ok, result
    assert result.result == {
        "models": {
            "haiku": {"label": "cheap", "use_for": "small fixes", "avoid_for": "", "notes": ""}
        },
        "model_policy": "Prefer cheap models.",
    }
    assert env.call("agent.models", uuid="unknown").result["models"]["haiku"]["use_for"] == ""
    global_path.write_text(
        "agents:\n  models:\n    haiku: {label: updated}\n  model_policy: Updated policy.\n"
    )
    updated = env.call("agent.models", uuid=uuid)
    assert updated.result["models"]["haiku"]["label"] == "updated"
    assert updated.result["model_policy"] == "Updated policy."
    assert env.memory.requests.empty()


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
