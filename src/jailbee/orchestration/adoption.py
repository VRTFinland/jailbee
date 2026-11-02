"""Eligibility and label changes for adopting existing containers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from jailbee.lifecycle import short_name
from jailbee.orchestration import commands, labels
from jailbee.orchestration.grant import GrantError, effective_grant
from jailbee.orchestration.profiles import repo_cap

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

CHILD_STATE_LABELS = (labels.RIGHTS, labels.TIMED, labels.RUNNER, labels.STATE, labels.DONE_SHA, labels.LAST_IMPORT, labels.STOPPED_HEAD, labels.ON_DONE, labels.PENDING_DESTROY, labels.STOPPED_DIRTY)


class AdoptError(ValueError):
    """Adoption was refused."""


@dataclass(frozen=True)
class AdoptResult:
    adopted: tuple[str, ...]
    already: tuple[str, ...]


def _config(raw: dict[str, object]) -> dict[str, object]:
    value = raw.get("config")
    return value if isinstance(value, dict) else {}


def _child_problem(cfg: Config, raw_by_name: dict[str, dict[str, object]], orchestrator_full: str, full: str) -> str | None:
    if full == orchestrator_full:
        return f"'{full}' cannot adopt itself"
    raw = raw_by_name.get(full)
    if raw is None or not commands._is_own(cfg, raw):
        return f"'{full}' is not a container of this repo"
    config = _config(raw)
    short = short_name(cfg, full)
    if config.get("user.jailbee.mode") == "mount":
        return f"'{short}' is a mount-mode container"
    if config.get(labels.GRANT) is not None:
        return f"'{short}' holds an orchestration grant"
    if any(_config(other).get(labels.SPAWNED_BY) == full for other in raw_by_name.values()):
        return f"'{short}' has children of its own"
    parent = config.get(labels.SPAWNED_BY)
    if parent and parent != orchestrator_full:
        return f"'{short}' is a child of '{short_name(cfg, str(parent))}'; release it first"
    return None


def _rows(incus: Incus) -> dict[str, dict[str, object]]:
    return {str(row['name']): row for row in incus.list_containers() if isinstance(row.get('name'), str)}


def candidates(cfg: Config, incus: Incus, orchestrator_full: str) -> list[str]:
    rows = _rows(incus)
    return sorted((name for name, raw in rows.items() if not _config(raw).get(labels.SPAWNED_BY) and _child_problem(cfg, rows, orchestrator_full, name) is None), key=lambda name: short_name(cfg, name))


def adopt(cfg: Config, incus: Incus, orchestrator_full: str, children: Sequence[str]) -> AdoptResult:
    rows = _rows(incus)

    class SnapshotIncus:
        def list_containers(self) -> list[dict[str, object]]:
            return list(rows.values())

        def config_get(self, full: str, key: str) -> str | None:
            return incus.config_get(full, key)

    problems = []
    refusal = commands.grant_refusal(cfg, cast("Incus", SnapshotIncus()), orchestrator_full)
    if refusal:
        problems.append(refusal)
    try:
        grant = commands.read_grant(incus, orchestrator_full)
    except GrantError as exc:
        grant = None
        problems.append(str(exc))
    if grant is None and not problems:
        problems.append(f"'{short_name(cfg, orchestrator_full)}' holds no orchestration grant")
    effective = effective_grant(grant, repo_cap(cfg)) if grant else None
    unique = tuple(dict.fromkeys(children))
    already = tuple(name for name in unique if name in rows and _config(rows[name]).get(labels.SPAWNED_BY) == orchestrator_full)
    pending = tuple(name for name in unique if name not in already)
    for name in unique:
        problem = _child_problem(cfg, rows, orchestrator_full, name)
        if problem:
            problems.append(problem)
    if effective is not None:
        running = sum(1 for raw in rows.values() if _config(raw).get(labels.SPAWNED_BY) == orchestrator_full and raw.get('status') == 'Running')
        running += sum(1 for name in pending if name in rows and rows[name].get('status') == 'Running')
        if running > effective.max_running_children:
            problems.append(f"limit of {effective.max_running_children} running children exceeded; use `jb orchestrate grant --children`")
    elif grant is not None and not problems:
        problems.append("orchestration grant is not effective")
    if problems:
        raise AdoptError('; '.join(problems))
    for name in pending:
        config = _config(rows[name])
        for key in CHILD_STATE_LABELS:
            if key in config:
                incus.config_unset_checked(name, key)
        incus.config_set(name, labels.ON_DONE, labels.ON_DONE_KEEP)
        incus.config_set(name, labels.SPAWNED_BY, orchestrator_full)
    return AdoptResult(adopted=pending, already=already)
