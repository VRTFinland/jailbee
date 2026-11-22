"""Gather each running container's service states, for the state service.

One `service ls --json` per running container, at most once per ``interval``
(slower than the base tier: an `incus exec` per container is not free). The
result is written onto `ContainerInfo.services`; a probe that fails marks the
container's services unknown (`services_unknown`) and never fails the gather.
Between probes the cached answer is reused, so every snapshot carries it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.lifecycle import ServiceStatus

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup
    from jailbee.incus import Incus

log = logging.getLogger(__name__)

SERVICE_INTERVAL = 30.0
"""Seconds a container's service listing is reused before it is probed again."""

MAX_WORKERS = 4

PROBE_TIMEOUT = 15.0
"""Seconds one gather waits for the probes; a container that is slower reads unknown."""

Fetch = Callable[[Path, str], Sequence[dict[str, Any]]]
"""``(repo root, container) -> the rows of `service ls --json```; raises on failure."""


@dataclass
class _Entry:
    at: float  # monotonic
    services: tuple[ServiceStatus, ...] | None  # None = the probe failed


def live_fetch(incus: Incus) -> Fetch:
    """The real listing: the repo's config, then the container's own `service ls`."""

    def fetch(root: Path, full: str) -> Sequence[dict[str, Any]]:
        from jailbee.config.loader import load_repo_config
        from jailbee.services.host import service_rows

        return service_rows(load_repo_config(root), incus, full)

    return fetch


def _parse_services(data: Sequence[dict[str, Any]]) -> tuple[ServiceStatus, ...]:
    """The listing's rows; a malformed row raises, so the container reads unknown."""
    return tuple(ServiceStatus(str(item["name"]), str(item["state"])) for item in data)


class ServiceProbe:
    def __init__(
        self,
        fetch: Fetch,
        *,
        interval: float = SERVICE_INTERVAL,
        timeout: float = PROBE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="svc-probe")
        self._fetch = fetch
        self._interval = interval
        self._clock = clock
        self._cache: dict[str, _Entry] = {}

    def _probe(self, root: Path, name: str) -> _Entry:
        try:
            services: tuple[ServiceStatus, ...] | None = _parse_services(self._fetch(root, name))
        except Exception:
            log.debug("service probe of %s failed", name, exc_info=True)
            services = None
        return _Entry(self._clock(), services)

    def annotate(self, groups: Sequence[RepoGroup]) -> None:
        """Write ``services`` / ``services_unknown`` onto every container."""
        now = self._clock()
        due: list[tuple[Path, str]] = []
        running: set[str] = set()
        for group in groups:
            if group.repo_root is None:
                continue
            for c in group.containers:
                if c.state != "Running":
                    continue
                running.add(c.name)
                entry = self._cache.get(c.name)
                if entry is None or now - entry.at >= self._interval:
                    due.append((Path(group.repo_root), c.name))
        for gone in set(self._cache) - running:
            del self._cache[gone]
        if due:
            futures = [(name, self._pool.submit(self._probe, root, name)) for root, name in due]
            deadline = time.monotonic() + self._timeout
            for name, future in futures:
                try:
                    entry = future.result(timeout=max(0.0, deadline - time.monotonic()))
                except TimeoutError:  # still running: unknown now, retried next interval
                    entry = _Entry(self._clock(), None)
                self._cache[name] = entry
        for group in groups:
            for c in group.containers:
                entry = self._cache.get(c.name) if c.state == "Running" else None
                c.services = None if entry is None else entry.services
                c.services_unknown = entry is not None and entry.services is None
