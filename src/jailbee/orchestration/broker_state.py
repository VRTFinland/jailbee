"""In-memory broker state and container inventory helpers."""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.config import load_repo_config
from jailbee.orchestration import labels
from jailbee.orchestration.actions import Node
from jailbee.orchestration.child_status import StatusCache

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from types import FrameType

    from jailbee.config import Config
    from jailbee.orchestration.actions import ActionContext
    from jailbee.orchestration.listener import Event, Listener
    from jailbee.orchestration.query import Queries
    from jailbee.orchestration.socket_ops import Waiters
    from jailbee.orchestration.store import BrokerStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChildrenView:
    """An orchestrator's published children list and when it was observed."""

    observed_at: float
    children: list[dict[str, object]]


@dataclass
class BrokerMemory:
    """What the broker remembers between rounds (in memory only)."""

    publish_threads: list[threading.Thread] = field(default_factory=list)
    # Prepared environment caches use instance keys (volatile.uuid, else name).
    env_ensured: set[str] = field(default_factory=set)
    env_roles: dict[str, str | None] = field(default_factory=dict)
    # volatile.uuid -> the role its labels gave in the round's listing
    # (`listed_roles`); the fail-closed source for containers without a context.
    listed_roles: dict[str, str] = field(default_factory=dict)
    # Keys of warnings already logged, so a standing problem is logged once.
    warned: set[str] = field(default_factory=set)
    # Host spool sequence: orders the items emitted within one second.
    seq: Iterator[int] = field(default_factory=itertools.count)
    # Probe cadence and CPU readings behind the published child status (#64).
    status: StatusCache = field(default_factory=StatusCache)
    # Orchestrator full name -> its children as last published by sync_children.
    children: dict[str, ChildrenView] = field(default_factory=dict)
    # Container key (volatile.uuid) -> the context built for it in the last round.
    contexts: dict[str, ActionContext] = field(default_factory=dict)
    # Socket transport (None in tests that do not exercise it).
    listener: Listener | None = None
    store: BrokerStore | None = None
    queries: Queries | None = None
    requests: queue.Queue[Event] = field(default_factory=queue.Queue)
    # Container full name -> volatile.uuid whose socket device we attached.
    attached: dict[str, str] = field(default_factory=dict)
    # volatile.uuids whose sock dir this process created: the device must be
    # re-asserted (remove + add) until that has succeeded once.
    reassert: set[str] = field(default_factory=set)
    # Parked `next` long-polls, answered by the executor.
    waiters: Waiters = field(default_factory=lambda: _new_waiters())
    # `monotonic()` of the last retention purge; None until the first round.
    last_purge: float | None = None
    # Store uuids the previous purge did not find in the listing.
    absent_uuids: set[str] = field(default_factory=set)
    # volatile.uuid values two or more listed instances share (no socket for them).
    shared_uuids: set[str] = field(default_factory=set)
    # Generations exclusively claimed by their owner in this round's full inventory.
    reusable_socket_dirs: set[str] = field(default_factory=set)
    # Never serve a directory a foreign device named, even if cleanup failed.
    unsafe_socket_dirs: set[str] = field(default_factory=set)

    # Per-container failure key -> signature of the failure last logged for it.
    failures: dict[str, str] = field(default_factory=dict)

    def forget_vanished(self, names: set[str], keys: set[str]) -> None:
        """Prune instance caches even when the round has no work or config fails."""
        self.env_ensured.intersection_update(keys)
        for key, ctx in list(self.contexts.items()):
            if key not in keys:
                self.children.pop(ctx.sender.full, None)
        for cache in (self.env_roles, self.contexts):
            for key in cache.keys() - keys:
                del cache[key]
        for full in self.children.keys() - names:
            del self.children[full]
        self.status.prune(names, names)
        self.reassert.intersection_update(keys)
        # Repo warnings and security tombstones describe host resources, not instances.
        instance_warnings = {
            "env",
            "socket",
            "socket-off",
            "socket-gone",
            "socket-shared",
            "socket-rotate",
            "no-config",
            "bad-grant",
        }
        for key in list(self.warned):
            prefix, _, owner = key.partition(":")
            if prefix in instance_warnings and owner not in names | keys:
                self.warned.remove(key)
        for key in list(self.failures):
            if key.partition(":")[2] not in names:
                del self.failures[key]

    def warn_once(self, key: str, msg: str, *args: object) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(msg, *args)

    def report_failure(self, key: str, exc: Exception, msg: str, *args: object) -> None:
        """Log a per-tick failure once per episode instead of once per round.

        A busy instance (an operation in progress) is transient: one WARNING
        without a traceback. Any other error logs once per distinct message,
        with the traceback on that first line. `settle_failures` ends episodes.
        """
        text = str(exc)
        busy = "instance is busy" in text.lower()
        signature = "busy" if busy else f"{type(exc).__name__}: {text}"
        if self.failures.get(key) == signature:
            return
        self.failures[key] = signature
        if busy:
            log.warning("%s (instance busy, retrying next round): %s", msg % args, text)
        else:
            log.error(msg, *args, exc_info=exc)

    def settle_failures(self, prefix: str, failed: set[str]) -> None:
        """End the episodes under `prefix` that did not fail this round."""
        for key in [k for k in self.failures if k.startswith(prefix) and k not in failed]:
            del self.failures[key]


def _new_waiters() -> Waiters:
    from jailbee.orchestration.socket_ops import Waiters

    return Waiters()


class StopFlag:
    """SIGTERM/SIGINT handler that only records the request; the loop polls it."""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        self.requested = True


def monotonic() -> float:
    return time.monotonic()


def repo_configs(memory: BrokerMemory | None = None) -> dict[str, Config | None]:
    """Registered container prefix -> loaded `Config`, or None when it does not load.

    A failed repo keeps its prefix (as None) so its containers are never
    attributed to a shorter registered prefix.
    """
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    memory = memory if memory is not None else BrokerMemory()
    with Session(get_engine()) as session:
        rows = [(r.container_prefix, r.repo_root) for r in session.exec(select(RegisteredRepo))]
    out: dict[str, Config | None] = {}
    for prefix, repo_root in rows:
        root = Path(repo_root)
        cfg: Config | None = None
        if not root.is_dir():
            memory.warn_once(f"repo-gone:{prefix}", "broker: repo %s: %s is gone", prefix, root)
        else:
            try:
                cfg = load_repo_config(root)
            except Exception as e:
                memory.warn_once(
                    f"repo-load:{prefix}", "broker: repo %s: config load failed: %s", prefix, e
                )
            else:
                if cfg.container_prefix != prefix:
                    memory.warn_once(
                        f"repo-prefix:{prefix}",
                        "broker: repo %s: prefix changed to %s; run `jailbee apply`",
                        prefix,
                        cfg.container_prefix,
                    )
                    cfg = None
        out[prefix] = cfg
    return out


def config_for(full: str, configs: Mapping[str, Config | None]) -> Config | None:
    """The config of the longest prefix `full` starts with (plus `-`); None if it failed."""
    best: str | None = None
    for prefix in configs:
        if full.startswith(prefix + "-") and (best is None or len(prefix) > len(best)):
            best = prefix
    return None if best is None else configs[best]


def is_managed(full: str, configs: Mapping[str, Config | None]) -> bool:
    """Whether ``full`` belongs to a registered repo, even one whose config fails to load."""
    return any(full.startswith(prefix + "-") for prefix in configs)


def owner_config(
    full: str, config: Mapping[str, str], configs: Mapping[str, Config | None]
) -> Config | None:
    """A child resolves through its orchestrator's repo; anything else by its own name."""
    parent = config.get(labels.SPAWNED_BY)
    return config_for(parent or full, configs)


def labelled(config: Mapping[str, str]) -> bool:
    """Whether the labels make this container an orchestrator or a child candidate."""
    return bool(config.get(labels.SPAWNED_BY) or config.get(labels.GRANT))


def listed_roles(raw: list[dict[str, object]]) -> dict[str, str]:
    """volatile.uuid -> child, orchestrator or plain, from each listed instance's labels."""
    roles: dict[str, str] = {}
    for entry in raw:
        config = entry.get("config")
        if not isinstance(config, dict):
            continue
        uuid = config.get("volatile.uuid")
        if not isinstance(uuid, str) or not uuid:
            continue
        if config.get(labels.SPAWNED_BY):
            roles[uuid] = "child"
        elif config.get(labels.GRANT):
            roles[uuid] = "orchestrator"
        else:
            roles[uuid] = "plain"
    return roles


def nodes(raw: list[dict[str, object]], configs: Mapping[str, Config | None]) -> dict[str, Node]:
    from jailbee.lifecycle import short_name

    nodes: dict[str, Node] = {}
    for entry in raw:
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        conf = entry.get("config")
        config = {str(k): str(v) for k, v in conf.items()} if isinstance(conf, dict) else {}
        cfg = owner_config(name, config, configs)
        short = short_name(cfg, name) if cfg is not None else name
        started_at = None
        last_used = entry.get("last_used_at")
        if isinstance(last_used, str):
            try:
                boot = datetime.fromisoformat(last_used)
                if boot.year > 1970 and boot.tzinfo is not None:
                    started_at = boot.timestamp()
            except ValueError:
                pass
        state = entry.get("state")
        state_data: dict[str, object] = state if isinstance(state, dict) else {}
        cpu = state_data.get("cpu")
        memory = state_data.get("memory")
        nodes[name] = Node(
            full=name,
            short=short,
            running=entry.get("status") == "Running",
            config=config,
            started_at=started_at,
            init_pid=_positive_int(state_data.get("pid")),
            cpu_usage_ns=_positive_int(cpu.get("usage") if isinstance(cpu, dict) else None),
            memory_usage=_positive_int(memory.get("usage") if isinstance(memory, dict) else None),
        )
    return nodes


def _positive_int(value: object) -> int | None:
    """An int > 0, else None; bools are ints in Python and are rejected."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
