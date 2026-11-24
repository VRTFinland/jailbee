"""On-demand broker queries and their per-field visibility policy.

Readers run on the bounded query workers, never on the listener or round loop.
They receive the caller's socket identity and immutable view, not broker memory.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jailbee import sysstat
from jailbee.orchestration import wire

if TYPE_CHECKING:
    from jailbee.incus import Incus
    from jailbee.orchestration.query import ContainerSnapshot

HOST_STATUS = "host.status"
AGENT_MODELS = "agent.models"
LIVE_OPS = frozenset({HOST_STATUS, AGENT_MODELS})
Visibility = Literal["all", "orchestrator"]


@dataclass(frozen=True)
class LiveQuery:
    """One reader with an explicit allowlist: undeclared fields never leave the host."""

    read: Callable[[str, ContainerSnapshot], Mapping[str, object]]
    fields: Mapping[str, Visibility]

    def answer(self, uuid: str, view: ContainerSnapshot) -> dict[str, object]:
        values = self.read(uuid, view)
        return {
            name: values[name]
            for name, visibility in self.fields.items()
            if name in values
            and (
                visibility == "all"
                or (visibility == "orchestrator" and view.role == "orchestrator")
            )
        }


def host_queries(incus: Incus) -> dict[str, LiveQuery]:
    """The production registry; later queries use this same transport and policy."""

    def status(uuid: str, view: ContainerSnapshot) -> Mapping[str, object]:
        inventory = incus.list_containers(fast=True, all_projects=True, timeout=10)
        caller = next(
            (row for row in inventory if (row.get("config") or {}).get("volatile.uuid") == uuid),
            None,
        )
        if caller is None:
            raise wire.WireError(wire.STORE, "calling container is no longer listed; retry shortly")
        devices = caller.get("expanded_devices") or {}
        pool = next(
            (
                device.get("pool")
                for device in devices.values()
                if device.get("type") == "disk" and device.get("path") == "/"
            ),
            None,
        )
        available = None
        if isinstance(pool, str) and pool:
            space = incus.storage_pool_resources(pool, timeout=10).get("space") or {}
            total, used = space.get("total"), space.get("used")
            if type(total) is int and type(used) is int and 0 <= used <= total:
                available = total - used
        load = sysstat.read_load()
        memory = sysstat.read_memory()
        return {
            "load1": load.load1 if load is not None else None,
            "cpus": load.cpus if load is not None else (os.cpu_count() or 1),
            "memory_total": memory.total if memory is not None else None,
            "memory_available": memory.available if memory is not None else None,
            "storage_pool": pool,
            "disk_available": available,
            "running_containers": sum(
                row.get("status") == "Running"
                and any(p.endswith("-base") for p in row.get("profiles") or [])
                for row in inventory
            ),
        }

    def models(uuid: str, view: ContainerSnapshot) -> Mapping[str, object]:
        from jailbee.config import load_repo_config

        if view.project_config is None:
            raise wire.WireError(wire.STORE, "calling container has no repository config")
        # The snapshot identifies the repo only: never return its cached settings.
        cfg = load_repo_config(view.project_config.repo_root)
        return cfg.agent_models().model_dump(mode="json")

    return {
        AGENT_MODELS: LiveQuery(models, {"models": "all", "model_policy": "all"}),
        HOST_STATUS: LiveQuery(
            status,
            {
                "load1": "all",
                "cpus": "all",
                "memory_total": "all",
                "memory_available": "all",
                "storage_pool": "all",
                "disk_available": "all",
                "running_containers": "all",
            },
        ),
    }
