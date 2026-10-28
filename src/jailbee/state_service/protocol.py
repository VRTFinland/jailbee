"""Wire protocol between the state service and the dashboards.

Newline-delimited JSON over a unix socket: one object per line, its ``type``
field naming the message. Snapshots carry the dashboards' own `RepoGroup` /
`ContainerInfo` dataclasses, encoded by pydantic straight from their type
annotations — there is no second schema to keep in step with them.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from jailbee.accounts.models import ActivityEvent, AgentActivity
from jailbee.agent_status import AgentSummary
from jailbee.dashboard.model import AppMenuEntry, RepoAdvisory, RepoGroup
from jailbee.dashboard.system import PoolStatus, SystemStatus
from jailbee.git_status import GitStatus, SubmoduleChange
from jailbee.lifecycle import ContainerInfo
from jailbee.orchestration.view import ChildView, GrantView
from jailbee.procstat import ProcessActivity, ProcessMemory
from jailbee.sysstat import DiskStatus, LoadStatus, MemoryStatus
from jailbee.update_check import UpdateNotice

# Bumped on any change a client of another version could misread. 8 added
# RepoGroup.advisories, Snapshot.system and each container's top_cpu, top_mem
# and root_pool. 9 added Refresh.settle. 10 added RepoGroup.memory_default
# and ContainerInfo.memory_override, needed for safe memory-limit editing.
# 11 added the orchestration fields: ContainerInfo grant/child/children counts,
# GitStatus inbox/outbox counts, RepoGroup.orchestration_profiles. 12 added
# ChildView.runner. 13 adds default runner and profile fields to the dashboard.
# 14 added SystemStatus.load.
PROTOCOL = 14


class ProtocolError(ValueError):
    """A line that is not a valid message."""


@dataclass(frozen=True)
class Hello:
    """First message each way. A client sends its ``cwd_root``; the server, None."""

    protocol: int
    version: str
    cwd_root: str | None = None


@dataclass(frozen=True)
class Active:
    """The client is (not) on screen; the server gathers only for active clients."""

    value: bool


@dataclass(frozen=True)
class Refresh:
    """Gather now, git tier included.

    ``settle`` seconds: then keep gathering at the fast cadence, for an
    action whose effect takes a while to show (a container still booting).
    """

    settle: float = 0.0


@dataclass(frozen=True)
class Shutdown:
    """Exit: sent by a client of another version before it starts its own server."""


@dataclass(frozen=True)
class Snapshot:
    """One complete gather. ``git_enabled`` is the server's cadence setting.

    ``update`` is a newer jailbee release to advertise, or None. ``system`` is
    the host's memory and storage status, or None when it could not be read.
    """

    seq: int
    gathered_at: datetime
    git_enabled: bool
    groups: list[RepoGroup]
    update: UpdateNotice | None = None
    system: SystemStatus | None = None


@dataclass(frozen=True)
class GatherError:
    """The last gather failed; the previous snapshot is still the latest."""

    message: str


Message = Hello | Active | Refresh | Shutdown | Snapshot | GatherError

_NAMES: dict[type[Any], str] = {
    Hello: "hello",
    Active: "active",
    Refresh: "refresh",
    Shutdown: "shutdown",
    Snapshot: "snapshot",
    GatherError: "error",
}
_TYPES: dict[str, type[Any]] = {name: cls for cls, name in _NAMES.items()}

# The dataclasses' own modules import these under TYPE_CHECKING only, so
# their (string) annotations cannot be resolved without being handed them.
_NAMESPACE: dict[str, Any] = {
    cls.__name__: cls
    for cls in (
        ActivityEvent,
        AgentActivity,
        AgentSummary,
        AppMenuEntry,
        ChildView,
        ContainerInfo,
        DiskStatus,
        GitStatus,
        GrantView,
        LoadStatus,
        MemoryStatus,
        Path,
        PoolStatus,
        ProcessActivity,
        ProcessMemory,
        RepoAdvisory,
        RepoGroup,
        SubmoduleChange,
        SystemStatus,
        UpdateNotice,
        datetime,
    )
}


@functools.lru_cache(maxsize=8)
def _adapter(cls: type[Any]) -> TypeAdapter[Any]:
    adapter: TypeAdapter[Any] = TypeAdapter(cls)
    adapter.rebuild(_types_namespace=_NAMESPACE)
    return adapter


def encode(message: Message) -> bytes:
    """``message`` as one wire line, newline included."""
    # type(message) is one of the Message union types, but mypy sees it as type[Any]
    body = _adapter(type(message)).dump_python(message, mode="json")  # type: ignore[arg-type]
    msg_dict = {"type": _NAMES[type(message)], **body}
    return json.dumps(msg_dict, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> Message:
    """The message on ``line``; `ProtocolError` for anything else."""
    try:
        raw = json.loads(line)
    except ValueError as exc:
        raise ProtocolError(f"not a JSON line: {line[:80]!r}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError(f"unknown message: {line[:80]!r}")
    msg_type = raw.get("type")
    if not isinstance(msg_type, str) or msg_type not in _TYPES:
        raise ProtocolError(f"unknown message: {line[:80]!r}")
    cls = _TYPES[raw.pop("type")]
    try:
        # cls is one of the Message union types, but mypy sees it as type[Any]
        message: Message = _adapter(cls).validate_python(raw)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise ProtocolError(f"invalid {_NAMES[cls]} message: {exc}") from exc
    return message
