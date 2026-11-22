"""Gather each running container's service states, for the state service.

One `service ls --json` per running container, at most once per ``interval``
(slower than the base tier: an `incus exec` per container is not free). The
probe never blocks the gather, like `PrHeadAutofetcher`: ``annotate`` starts a
probe on a daemon thread, marks the container in flight, and attaches the
result on a later tick. A container with a probe in flight is never submitted
again; one whose probe outlives ``timeout`` reads unknown. A failed probe marks
the container's services unknown (`services_unknown`); a container without an
in-container jailbee shows no services at all and is re-probed only after
``ABSENT_INTERVAL``. Between probes the cached answer is reused, so every
snapshot carries it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.lifecycle import ServiceState

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup
    from jailbee.incus import Incus

log = logging.getLogger(__name__)

SERVICE_INTERVAL = 30.0
"""Seconds a container's service listing is reused before it is probed again."""

ABSENT_INTERVAL = 600.0
"""Seconds before a container without an in-container jailbee is probed again."""

PROBE_TIMEOUT = 15.0
"""Seconds a probe may stay in flight before its container reads unknown."""

Fetch = Callable[[Path, str], Sequence[dict[str, Any]]]
"""``(repo root, container) -> the rows of `service ls --json```; raises on failure."""


@dataclass
class _Entry:
    at: float  # monotonic
    services: tuple[ServiceState, ...] | None  # None = unknown (or absent)
    absent: bool = False  # no in-container jailbee: show nothing


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="svc-probe", daemon=True).start()


def live_fetch(incus: Incus) -> Fetch:
    """The real listing: the repo's config, then the container's own `service ls`."""

    def fetch(root: Path, full: str) -> Sequence[dict[str, Any]]:
        from jailbee.config.loader import load_repo_config
        from jailbee.services.host import service_rows

        return service_rows(load_repo_config(root), incus, full, probe=True)

    return fetch


def _parse_services(data: Sequence[dict[str, Any]]) -> tuple[ServiceState, ...]:
    """The listing's rows; a malformed row raises, so the container reads unknown."""
    return tuple(ServiceState(str(item["name"]), str(item["state"])) for item in data)


class ServiceProbe:
    def __init__(
        self,
        fetch: Fetch,
        *,
        interval: float = SERVICE_INTERVAL,
        timeout: float = PROBE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        submit: Callable[[Callable[[], None]], None] = _in_thread,
    ) -> None:
        self._fetch = fetch
        self._interval = interval
        self._timeout = timeout
        self._clock = clock
        self._submit = submit
        self._lock = threading.Lock()
        self._closed = False
        self._cache: dict[str, _Entry] = {}
        self._in_flight: dict[str, float] = {}  # container -> start time

    def shutdown(self) -> None:
        """Stop starting probes; results of those in flight are dropped."""
        with self._lock:
            self._closed = True

    def _job(self, root: Path, name: str) -> Callable[[], None]:
        def job() -> None:
            from jailbee.container_app import JailbeeAbsentError

            absent = False
            try:
                services: tuple[ServiceState, ...] | None = _parse_services(self._fetch(root, name))
            except JailbeeAbsentError:
                services, absent = None, True
            except Exception:
                log.debug("service probe of %s failed", name, exc_info=True)
                services = None
            with self._lock:
                if self._closed:
                    return
                self._in_flight.pop(name, None)
                self._cache[name] = _Entry(self._clock(), services, absent)

        return job

    def _due(self, name: str, now: float) -> bool:
        """Whether ``name`` needs a probe now. Lock held."""
        if name in self._in_flight:
            return False
        entry = self._cache.get(name)
        if entry is None:
            return True
        return now - entry.at >= (ABSENT_INTERVAL if entry.absent else self._interval)

    def annotate(self, groups: Sequence[RepoGroup]) -> None:
        """Start the probes that are due and write what is known onto every container."""
        now = self._clock()
        start: list[tuple[Path, str]] = []
        with self._lock:
            if self._closed:
                return
            running: set[str] = set()
            for group in groups:
                if group.repo_root is None:
                    continue
                for c in group.containers:
                    if c.state != "Running":
                        continue
                    running.add(c.name)
                    if self._due(c.name, now):
                        self._in_flight[c.name] = now
                        start.append((Path(group.repo_root), c.name))
            for gone in set(self._cache) - running:
                del self._cache[gone]
            for gone in set(self._in_flight) - running:
                del self._in_flight[gone]
        for root, name in start:
            self._submit(self._job(root, name))
        with self._lock:
            for group in groups:
                for c in group.containers:
                    self._write(c, now)

    def _write(self, c: Any, now: float) -> None:
        """Set ``services`` / ``services_unknown`` on ``c``. Lock held."""
        entry = self._cache.get(c.name) if c.state == "Running" else None
        started = self._in_flight.get(c.name)
        if started is not None and now - started >= self._timeout:
            c.services, c.services_unknown = None, True  # probe overdue
        elif entry is None or entry.absent:
            c.services, c.services_unknown = None, False
        else:
            c.services = entry.services
            c.services_unknown = entry.services is None
