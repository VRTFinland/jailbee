"""Wire protocol between the state service and the dashboards.

Newline-delimited JSON over a unix socket: one object per line, its ``type``
field naming the message. Snapshots carry the dashboards' own `RepoGroup` /
`ContainerInfo` dataclasses, encoded by pydantic straight from their type
annotations — there is no second schema to keep in step with them.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from jailbee.accounts.models import ActivityEvent, AgentActivity, AgentApiError
from jailbee.agent_status import AgentSummary, SessionMark
from jailbee.dashboard.model import AppMenuEntry, RepoAdvisory, RepoGroup
from jailbee.dashboard.system import PoolStatus, SystemStatus
from jailbee.git_status import GitStatus, OtherBranch, SubmoduleChange
from jailbee.host_target import SyncTarget
from jailbee.lifecycle import ContainerInfo, ServiceState
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
# 15 adds repo autostart choices/default and container autostart profile.
# 16 adds the per-container autostart failed-step count.
# 17 adds gathered job liveness.
# 18 adds profile-aware autostart re-run eligibility.
# 19 combines these with service choices and built-in autostart profiles.
# 20 adds the outbox Sent record counts: ContainerInfo and RepoGroup.sent_count.
# 21 adds ContainerInfo.uuid: Sent records are keyed by the instance uuid.
# 22 adds MemoryStatus.swap_cached and .zswapped: swap use leaves them out.
# 23 adds immutable board-scoped GrantView.publish_projects.
# 24 adds RepoGroup.default_project for the board picker.
# 25 adds AgentSummary.session_ids for the web dashboard's session attribution.
# 26 adds GitStatus.sync_target (the resolved comparison target), the
# host-branch ↑/↓ host_ahead_count/host_behind_count and host_branch.
# 27 adds RepoGroup.agent_label, the repo's effective agent as `profile/model`.
# 28 adds ContainerInfo.agent_runner and .agent_source, the agent the next session launches.
# 29 adds RepoGroup.service_descriptions, the repo services' optional descriptions.
# 30 adds SyncTarget.autofetch, .fetched_at, .fetch_error and .fetch_error_at.
# 31 adds GrantView.publish_pr_comment_edits, the PR comment-edit publish right.
# 32 adds GitStatus.other_branches and OtherBranch, local branches with unimported commits.
# 33 adds AgentSummary.sessions, one SessionMark per live session.
# 34 adds ContainerInfo.pr_slug, the repo's GitHub slug for PR hyperlinks.
# 35 adds ContainerInfo.services (ServiceState) and .services_unknown.
# 36 adds SyncTarget kind "parent" and .parent, .ahead_sha, .seen_at: children and
# forks compare with their parent container.
# 37 adds SyncTarget.parent_unknown: the parent's head is unknown (no gather pass).
# 38 makes ContainerInfo.job_live nullable: None means no gathered observation.
# 39 adds AgentActivity.api_error (AgentApiError): the API error a session is stuck on.
# 40 adds ContainerInfo.branch, the branch the container was created on.
# 41 adds Snapshot.resumable_repos for scoped Resume menu visibility.
# 42 adds None entries for local-only config/journal error recovery in resumable_repos.
# 43 adds ContainerInfo.start_identity for precise host-side boot recovery.
# 44 distinguishes registered repos with unloadable configs from true orphans.
# 45 adds action hosting messages. 46 adds action scope and dismissal.
PROTOCOL = 46

# Allows escaped output tails and ordinary snapshots while bounding incoming lines.
MAX_WIRE_LINE_BYTES = 16 * 1024 * 1024


class ProtocolError(ValueError):
    """A line that is not a valid message."""


@dataclass(frozen=True)
class Hello:
    """First message each way. A client sends its ``cwd_root``; the server, None."""

    protocol: int
    version: str
    cwd_root: str | None = None
    dashboard_id: str | None = None
    excluded_prefixes: list[str] = field(default_factory=list)


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
    # Shared host-wide snapshot: consumers MUST filter excluded prefixes by
    # their caller's repo scope. None means local-only config/journal error recovery.
    resumable_repos: list[str | None] = field(default_factory=list)


@dataclass(frozen=True)
class GatherError:
    """The last gather failed; the previous snapshot is still the latest."""

    message: str


@dataclass(frozen=True)
class RunAction:
    entry_id: int
    argv: list[str]
    cwd: str
    target: str
    critical: bool
    origin: str
    transport: Literal["none", "ssh", "web"]
    repo_root: str
    prefix: str
    address_flags: list[str] = field(default_factory=list)
    cwd_root: str | None = None
    excluded_prefixes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ActionEntry:
    entry_id: int
    argv: list[str]
    cwd: str
    target: str
    critical: bool
    origin: str
    transport: Literal["none", "ssh", "web"]
    repo_root: str
    prefix: str
    output: str = ""
    status: int | None = None
    finished: bool = False
    refused: str | None = None
    error: str | None = None
    cancelled: bool = False
    declined: bool = False
    state: Literal["running", "awaiting", "done", "failed", "interrupted"] = "running"
    cwd_root: str | None = None
    excluded_prefixes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ActionOutput:
    entry_id: int
    chunk: str


@dataclass(frozen=True)
class DismissAction:
    entry_id: int


@dataclass(frozen=True)
class ActionDismissed:
    entry_id: int


@dataclass(frozen=True)
class ActionRejected:
    entry_id: int
    reason: str


@dataclass(frozen=True)
class ActionFinish:
    entry_id: int
    status: int | None = None
    refused: str | None = None
    error: str | None = None
    cancelled: bool = False
    declined: bool = False


Message = (
    Hello
    | Active
    | Refresh
    | Shutdown
    | Snapshot
    | GatherError
    | RunAction
    | ActionEntry
    | ActionOutput
    | ActionFinish
    | ActionRejected
    | DismissAction
    | ActionDismissed
)

_NAMES: dict[type[Any], str] = {
    RunAction: "run_action",
    ActionEntry: "action_entry",
    ActionOutput: "action_output",
    ActionFinish: "action_finish",
    ActionRejected: "action_rejected",
    DismissAction: "dismiss_action",
    ActionDismissed: "action_dismissed",
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
        AgentApiError,
        AgentSummary,
        AppMenuEntry,
        ChildView,
        ContainerInfo,
        DiskStatus,
        GitStatus,
        GrantView,
        LoadStatus,
        MemoryStatus,
        OtherBranch,
        Path,
        PoolStatus,
        ProcessActivity,
        ProcessMemory,
        RepoAdvisory,
        RepoGroup,
        ServiceState,
        SessionMark,
        SubmoduleChange,
        SyncTarget,
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


def wire_schema() -> dict[str, Any]:
    """The JSON schema of each message, keyed by its wire ``type`` name.

    Docstring descriptions and class titles are left out: neither crosses the
    wire, so only a change a client could misread changes the schema.
    """
    # cls is one of the Message union types, but mypy sees it as type[Any]
    return {
        name: _wire_only(_adapter(cls).json_schema())  # type: ignore[arg-type]
        for cls, name in _NAMES.items()
    }


def _wire_only(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _wire_only(value)
            for key, value in node.items()
            if not (key in ("title", "description") and isinstance(value, str))
        }
    if isinstance(node, list):
        return [_wire_only(item) for item in node]
    return node


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
