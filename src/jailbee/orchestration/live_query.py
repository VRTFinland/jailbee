"""On-demand broker queries and their per-field visibility policy.

Readers run on dedicated bounded live-query workers, never on the listener or round loop.
They receive the caller's socket identity and immutable view, not broker memory.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

from jailbee import sysstat
from jailbee.incus import IncusError
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

    lock = threading.Lock()
    expires = 0.0
    inventory: list[dict[str, Any]] = []
    inventory_error: IncusError | None = None
    pools: dict[str, tuple[float, dict[str, Any] | IncusError]] = {}

    def readings(uuid: str) -> tuple[list[dict[str, Any]], str | None, int | None]:
        nonlocal expires, inventory, inventory_error
        # One refresh at a time across containers; failures share the same short TTL.
        with lock:
            refresh = monotonic() >= expires or (
                inventory_error is None
                and not any(
                    (row.get("config") or {}).get("volatile.uuid") == uuid for row in inventory
                )
            )
            if refresh:
                for pool, (until, _) in list(pools.items()):
                    if monotonic() >= until:
                        del pools[pool]
                inventory_error = None
                try:
                    inventory = incus.list_containers(fast=True, all_projects=True, timeout=10)
                except IncusError as exc:
                    inventory_error = exc
            try:
                if inventory_error is not None:
                    raise inventory_error
                return pool_reading(uuid)
            finally:
                if refresh:
                    expires = monotonic() + 2.0

    def pool_reading(uuid: str) -> tuple[list[dict[str, Any]], str | None, int | None]:
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
            cached = pools.get(pool)
            if cached is None or monotonic() >= cached[0]:
                resources: dict[str, Any] | IncusError
                try:
                    resources = incus.storage_pool_resources(pool, timeout=10)
                except IncusError as exc:
                    resources = exc
                pools[pool] = (monotonic() + 2.0, resources)
            else:
                resources = cached[1]
            if isinstance(resources, IncusError):
                raise resources
            space = resources.get("space") or {}
            total, used = space.get("total"), space.get("used")
            if type(total) is int and type(used) is int and 0 <= used <= total:
                available = total - used
        return inventory, pool if isinstance(pool, str) else None, available

    def status(uuid: str, view: ContainerSnapshot) -> Mapping[str, object]:
        inventory, pool, available = readings(uuid)
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
                and bool((row.get("config") or {}).get("user.jailbee.repo_dir"))
                and any(p.endswith("-base") for p in row.get("profiles") or [])
                for row in inventory
            ),
        }

    model_lock = threading.Lock()
    model_cache: dict[
        Path, tuple[tuple[Path, ...], tuple[tuple[int, int, int] | None, ...], dict[str, Any], str]
    ] = {}

    def fingerprint(paths: tuple[Path, ...]) -> tuple[tuple[int, int, int] | None, ...]:
        stamps: list[tuple[int, int, int] | None] = []
        for path in paths:
            try:
                st = path.stat()
            except FileNotFoundError:
                stamps.append(None)
            else:
                stamps.append((st.st_mtime_ns, st.st_size, st.st_ino))
        return tuple(stamps)

    def models(uuid: str, view: ContainerSnapshot) -> Mapping[str, object]:
        from jailbee.config.loader import load_repo_config_without_git
        from jailbee.config.local_layer import local_config_path
        from jailbee.global_config import default_global_config_path
        from jailbee.paths import REPO_CONFIG_DIRS

        if view.project_config is None:
            raise wire.WireError(wire.REFUSED, "calling container has no repository config")
        root = view.project_config.repo_root
        # Independent of the slow Incus lock; only changed layers are parsed again.
        with model_lock:
            cached = model_cache.get(root)
            if cached is not None and fingerprint(cached[0]) == cached[1]:
                return cached[2]
            paths = (
                default_global_config_path(),
                *(root / directory / "config.yaml" for directory in REPO_CONFIG_DIRS),
                local_config_path(
                    cached[3] if cached is not None else view.project_config.container_prefix
                ),
            )
            before = fingerprint(paths)
            cfg = load_repo_config_without_git(root)
            result = cfg.agent_models().model_dump(mode="json")
            # Never stamp a result with a version that changed while it was read.
            if local_config_path(cfg.container_prefix) == paths[-1]:
                model_cache[root] = (paths, before, result, cfg.container_prefix)
            else:
                # A prefix edit selects a different host-local layer. Re-read once
                # before caching so its stamp also precedes the load.
                paths = (*paths[:-1], local_config_path(cfg.container_prefix))
                before = fingerprint(paths)
                cfg = load_repo_config_without_git(root)
                result = cfg.agent_models().model_dump(mode="json")
                if local_config_path(cfg.container_prefix) == paths[-1]:
                    model_cache[root] = (paths, before, result, cfg.container_prefix)
            return result

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
