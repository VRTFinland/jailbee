"""Fakes for the web dashboard suite: clock, PIN generator, snapshot source."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jailbee.dashboard.model import RepoGroup
from jailbee.lifecycle import ContainerInfo
from jailbee.state_service.protocol import Snapshot

START = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    """Wall clock for session TTLs; also the monotonic clock for the rate limit."""

    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return (self.now - START).total_seconds()

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def pin_sequence() -> Callable[[], str]:
    """Deterministic, distinct PINs: 100000, 100001, ..."""
    numbers: Iterator[int] = iter(range(100000, 1000000))
    return lambda: f"{next(numbers):06d}"


@dataclass
class FakeSnapshotSource:
    """Stands in for `StateClient`: the test pushes snapshots by hand."""

    on_update: Callable[[], None]
    started: bool = False
    closed: bool = False
    _latest: Snapshot | None = None
    _status: str | None = None

    def start(self) -> None:
        self.started = True

    def latest(self) -> Snapshot | None:
        return self._latest

    def status(self) -> str | None:
        return self._status

    def close(self) -> None:
        self.closed = True

    def push(self, snapshot: Snapshot) -> None:
        """Deliver from another thread, as `StateClient`'s reader thread does."""
        self._latest = snapshot
        worker = threading.Thread(target=self.on_update)
        worker.start()
        worker.join()


@dataclass
class FakeSourceFactory:
    made: list[FakeSnapshotSource] = field(default_factory=list)
    initial: Snapshot | None = None

    def __call__(self, on_update: Callable[[], None]) -> FakeSnapshotSource:
        source = FakeSnapshotSource(on_update, _latest=self.initial)
        self.made.append(source)
        return source

    @property
    def only(self) -> FakeSnapshotSource:
        assert len(self.made) == 1, f"expected one source, got {len(self.made)}"
        return self.made[0]


def container(name: str, repo: str = "alpha", **fields: Any) -> ContainerInfo:
    info = ContainerInfo(
        name=name, state="Running", network="strict", ip=None, memory_limit=None, repo=repo
    )
    for key, value in fields.items():
        setattr(info, key, value)
    return info


def repo_group(prefix: str, *containers: ContainerInfo) -> RepoGroup:
    group = RepoGroup(prefix, f"/repos/{prefix}", Path(f"/repos/{prefix}/.jailbee/config.yaml"), [])
    group.containers.extend(containers)
    return group


def snapshot_of(*groups: RepoGroup, seq: int = 1) -> Snapshot:
    return Snapshot(seq=seq, gathered_at=START, git_enabled=False, groups=list(groups))


def snapshot(*names: str, seq: int = 1) -> Snapshot:
    group = RepoGroup("alpha", "/repos/alpha", Path("/repos/alpha/.jailbee/config.yaml"), [])
    group.containers.extend(container(n) for n in names)
    return Snapshot(seq=seq, gathered_at=START, git_enabled=False, groups=[group])


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)
