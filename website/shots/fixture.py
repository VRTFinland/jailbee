"""Load a screenshot fixture: a YAML description of a fictional dashboard world.

Containers are built from ``ContainerInfo``'s own defaults and only the fields
the fixture names are overridden, so the model stays the one place that knows
what a row is. Every unknown key fails with the key named.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from jailbee.agent_status import AgentSummary
from jailbee.dashboard.model import RepoGroup
from jailbee.git_status import GitStatus
from jailbee.lifecycle import ContainerInfo
from jailbee.orchestration.view import ChildView, GrantView
from jailbee.procstat import ProcessActivity, ProcessMemory

DEFAULT_SIZE = (200, 45)
TOP_KEYS = {"size", "now", "repos", "steps", "outbox", "accounts", "view", "queue"}
VIEW_KEYS = {"columns", "show_details", "show_system", "sort_field", "sort_desc", "details_rows"}
STEP_KEYS = {"shot", "key", "keys", "select", "menu", "queue"}
OUTBOX_KEYS = {"id", "kind", "action", "repo", "target", "title", "body", "state"}
QUEUE_KEYS = {"command", "target", "state", "status", "output"}
QUEUE_STATES = ("running", "done", "failed")
ACCOUNT_KEYS = ("agent", "group", "account", "state", "repos", "containers")
_DATETIME_FIELDS = {"created_at", "loose_until"}


class FixtureError(ValueError):
    """The fixture is not valid; the message names the offending key."""


@dataclass
class World:
    size: tuple[int, int]
    groups: list[RepoGroup]
    steps: list[tuple[str, Any]]
    now: datetime | None = None
    outbox: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    accounts: list[dict[str, Any]] = field(default_factory=list)
    view: dict[str, Any] = field(default_factory=dict)
    queue: list[dict[str, Any]] = field(default_factory=list)


def _check(keys: Any, allowed: set[str], where: str) -> None:
    unknown = sorted(set(keys) - allowed)
    if unknown:
        raise FixtureError(f"unknown key {unknown[0]!r} in {where}")


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FixtureError(f"{where} must be a mapping")
    return value


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _build(cls: type, raw: Any, where: str, fill: Any = None) -> Any:
    """``cls(**raw)`` with an unknown key named; datetimes parsed.

    A required field the fixture leaves out takes ``fill``.
    """
    raw = dict(_mapping(raw, where))
    names = {f.name for f in dataclasses.fields(cls)}
    _check(raw, names, where)
    for name in ("since", "modified_at"):
        if name in raw:
            raw[name] = _dt(raw[name])
    for f in dataclasses.fields(cls):
        if (
            f.name not in raw
            and f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ):
            raw[f.name] = fill
    return cls(**raw)


def _container(prefix: str, raw: Any) -> ContainerInfo:
    raw = dict(_mapping(raw, f"a container of {prefix!r}"))
    if "name" not in raw:
        raise FixtureError(f"a container of {prefix!r} has no 'name'")
    where = f"container {raw['name']!r}"
    fields = {f.name for f in dataclasses.fields(ContainerInfo)}
    _check(raw, fields, where)
    short = str(raw.pop("name"))
    full = short if short.startswith(f"{prefix}-") else f"{prefix}-{short}"
    over: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _DATETIME_FIELDS:
            over[key] = _dt(value)
        elif key == "git_status":
            over[key] = _build(GitStatus, value, f"git_status of {full!r}", "—")
        elif key == "agent_status":
            over[key] = tuple(_build(AgentSummary, v, f"agent_status of {full!r}") for v in value)
        elif key in ("activity", "top_cpu"):
            over[key] = tuple(_build(ProcessActivity, v, f"{key} of {full!r}") for v in value)
        elif key == "top_mem":
            over[key] = tuple(_build(ProcessMemory, v, f"top_mem of {full!r}") for v in value)
        elif key == "grant":
            over[key] = _grant(value, full)
        elif key == "child":
            over[key] = _child(prefix, value, full)
        elif key in ("optional_mounts", "configured_mounts", "egress_extra"):
            over[key] = tuple(value)
        else:
            over[key] = value
    base = ContainerInfo(
        name=full,
        state="Running",
        network="strict",
        ip=None,
        memory_limit="8GiB",
        repo=prefix,
    )
    return dataclasses.replace(base, **over)


def _grant(raw: Any, full: str) -> GrantView:
    raw = dict(_mapping(raw, f"grant of {full!r}"))
    _check(raw, {"limit"} | {f.name for f in dataclasses.fields(GrantView)}, f"grant of {full!r}")
    defaults: dict[str, Any] = {"operations": (), "mounts": (), "loose": False, "egress": ()}
    limit = raw.pop("limit", 4)
    return GrantView(**{"limit": limit, **defaults, **raw})


def _child(prefix: str, raw: Any, full: str) -> ChildView:
    raw = dict(_mapping(raw, f"child of {full!r}"))
    _check(raw, {f.name for f in dataclasses.fields(ChildView)}, f"child of {full!r}")
    if "orchestrator" not in raw:
        raise FixtureError(f"child of {full!r} has no 'orchestrator'")
    orch = str(raw["orchestrator"])
    raw["orchestrator"] = orch if orch.startswith(f"{prefix}-") else f"{prefix}-{orch}"
    defaults: dict[str, Any] = {"state": "working", "mounts": (), "loose": False, "egress": ()}
    return ChildView(**{**defaults, **raw})


_REPO_FIELDS = {f.name for f in dataclasses.fields(RepoGroup)} - {"containers"}


def _group(raw: Any) -> RepoGroup:
    raw = dict(_mapping(raw, "a repo"))
    if "prefix" not in raw:
        raise FixtureError("a repo has no 'prefix'")
    prefix = str(raw["prefix"])
    _check(raw, _REPO_FIELDS | {"containers"}, f"repo {prefix!r}")
    containers = [_container(prefix, c) for c in raw.pop("containers", None) or []]
    names = {c.name for c in containers}
    for c in containers:
        if c.child is not None and c.child.orchestrator not in names:
            short = c.child.orchestrator.removeprefix(f"{prefix}-")
            raise FixtureError(
                f"{c.name!r} is a child of {short!r}, which is not in repo {prefix!r}"
            )
    raw.setdefault("repo_root", f"/repos/{prefix}")
    raw.setdefault("config_path", None)
    for key in ("agent_homes", "agent_config_homes", "optional_mounts"):
        if key in raw:
            raw[key] = tuple(raw[key])
    return RepoGroup(containers=containers, **raw)


def _step(raw: Any) -> tuple[str, Any]:
    if isinstance(raw, str):
        return ("key", raw)
    raw = _mapping(raw, "a step")
    _check(raw, STEP_KEYS, "a step")
    if len(raw) != 1:
        raise FixtureError(f"a step takes exactly one key, got {sorted(raw)}")
    ((kind, value),) = raw.items()
    if kind == "keys":
        return ("keys", [str(v) for v in value])
    return (kind, value)


def outbox_listing(world: World, container: str) -> str:
    """``jailbee outbox ls -o json`` for ``container``, built from the fixture's proposals."""
    proposals = []
    for index, p in enumerate(world.outbox.get(container, [])):
        kind = p.get("kind", p["id"].split("/")[0])
        action = {
            "index": 0,
            "kind": p.get("action", "create-pr" if kind == "pr" else "create-issue"),
            "repo": p.get("repo", ""),
            "target": p.get("target", ""),
            "title": p.get("title"),
            "body": p.get("body"),
            "state": p.get("state", "pending"),
        }
        proposals.append(
            {
                "id": p["id"],
                "kind": kind,
                "state": p.get("state", "pending"),
                "revision": f"r{index}",
                "raw_text": "",
                "deletable": True,
                "actions": [action],
            }
        )
    entry = {
        "name": container,
        "available": True,
        "error": None,
        "stores": [],
        "proposals": proposals,
    }
    return json.dumps({"schema": 1, "containers": [entry]})


def accounts_listing(world: World) -> str:
    """``jailbee account ls -o json`` rows from the fixture's accounts."""
    keys = ACCOUNT_KEYS
    return json.dumps(
        [
            {k: (a.get(k, [] if k in ("repos", "containers") else None)) for k in keys}
            for a in world.accounts
        ]
    )


def load_world(source: str | Path) -> World:
    """Parse fixture YAML (text, or the path of a file) into a `World`."""
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    raw = _mapping(yaml.safe_load(text) or {}, "the fixture")
    _check(raw, TOP_KEYS, "the fixture")
    size = tuple(raw.get("size", DEFAULT_SIZE))
    if len(size) != 2:
        raise FixtureError("size must be [width, height]")
    groups = [_group(g) for g in raw.get("repos") or []]
    steps = [_step(s) for s in raw.get("steps") or []]
    outbox = {str(k): list(v) for k, v in _mapping(raw.get("outbox") or {}, "outbox").items()}
    for container, proposals in outbox.items():
        for p in proposals:
            where = f"an outbox proposal of {container!r}"
            _check(_mapping(p, where), OUTBOX_KEYS, where)
            if "id" not in p:
                raise FixtureError(f"{where} has no 'id'")
    accounts = list(raw.get("accounts") or [])
    for a in accounts:
        _check(_mapping(a, "an account"), set(ACCOUNT_KEYS), "an account")
    view = _mapping(raw.get("view") or {}, "view")
    _check(view, VIEW_KEYS, "view")
    queue = list(raw.get("queue") or [])
    for q in queue:
        _check(_mapping(q, "a queue entry"), QUEUE_KEYS, "a queue entry")
        for need in ("command", "target", "state"):
            if need not in q:
                raise FixtureError(f"a queue entry has no {need!r}")
        if q["state"] not in QUEUE_STATES:
            raise FixtureError(f"queue state {q['state']!r} is not one of {QUEUE_STATES}")
        if "output" in q and not isinstance(q["output"], str):
            raise FixtureError("queue 'output' must be a string")
        if "status" in q and (isinstance(q["status"], bool) or not isinstance(q["status"], int)):
            raise FixtureError("queue 'status' must be an integer")
        if "status" in q and q["state"] == "running":
            raise FixtureError("a running queue entry has no 'status'")
    return World(
        (int(size[0]), int(size[1])),
        groups,
        steps,
        _dt(raw["now"]) if "now" in raw else None,
        outbox,
        accounts,
        view,
        queue,
    )
