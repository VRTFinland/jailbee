"""Eligibility and label changes for adopting existing containers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from jailbee.incus import IncusError
from jailbee.lifecycle import short_name
from jailbee.orchestration import commands, labels, outbox, rights, state, watch
from jailbee.orchestration.grant import GrantError, effective_grant
from jailbee.orchestration.profiles import repo_cap
from jailbee.orchestration.protocol import InboxMessage, new_host_id

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.incus import Incus

CHILD_STATE_LABELS = (
    labels.RIGHTS,
    labels.TIMED,
    labels.RUNNER,
    labels.STATE,
    labels.DONE_SHA,
    labels.LAST_IMPORT,
    labels.STOPPED_HEAD,
    labels.ON_DONE,
    labels.PENDING_DESTROY,
    labels.STOPPED_DIRTY,
)


ADOPTED_CHILD_BODY = (
    "You are now a child container of `{orchestrator}` (adopted by the host user). "
    "Follow the jailbee-orchestration-child skill: report with "
    "`jailbee inbox send message --kind done|question|blocked` and keep a background "
    "`jailbee inbox wait` running."
)
ADOPTED_ORCHESTRATOR_BODY = (
    "`{child}` is now your child (adopted by the host user). It has no import baseline, "
    "so `destroy` refuses until you import it or pass --force. Its agent is not supervised "
    "until you ask the user to close the hand-started agent and relaunch it with "
    "`send restart --child {child} --agent-only`."
)
RELEASED_ORCHESTRATOR_BODY = "`{child}` was released by the host user and is no longer your child."


class AdoptError(ValueError):
    """Adoption was refused."""


@dataclass(frozen=True)
class AdoptResult:
    adopted: tuple[str, ...]
    already: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseResult:
    orchestrator: str
    revoked: tuple[str, ...]
    warnings: tuple[str, ...]


def _notify(
    cfg: Config,
    incus: Incus,
    root: Path,
    rows: dict[str, dict[str, object]],
    recipient: str,
    sender: str,
    kind: str,
    body: str,
    now: datetime,
    seq: int,
) -> str | None:
    item = InboxMessage.model_validate(
        {
            "version": 1,
            "id": new_host_id(),
            "type": "message",
            "from": short_name(cfg, sender),
            "kind": kind,
            "body": body,
        }
    )
    try:
        outbox.deliver(
            incus,
            root,
            recipient,
            item,
            running=rows.get(recipient, {}).get("status") == "Running",
            uid=cfg.container_user.uid,
            now=now,
            seq=seq,
        )
    except (OSError, IncusError) as exc:
        return f"could not notify {short_name(cfg, recipient)}: {exc}"
    return None


def _config(raw: dict[str, object]) -> dict[str, object]:
    value = raw.get("config")
    return value if isinstance(value, dict) else {}


def _child_problem(
    cfg: Config, raw_by_name: dict[str, dict[str, object]], orchestrator_full: str, full: str
) -> str | None:
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
    return {
        str(row["name"]): row for row in incus.list_containers() if isinstance(row.get("name"), str)
    }


def candidates(cfg: Config, incus: Incus, orchestrator_full: str) -> list[str]:
    rows = _rows(incus)
    return sorted(
        (
            name
            for name, raw in rows.items()
            if not _config(raw).get(labels.SPAWNED_BY)
            and _child_problem(cfg, rows, orchestrator_full, name) is None
        ),
        key=lambda name: short_name(cfg, name),
    )


def children_of_repo(cfg: Config, incus: Incus) -> list[str]:
    """Return this repo's children in short-name order."""
    return sorted(
        (
            name
            for name, raw in _rows(incus).items()
            if commands._is_own(cfg, raw) and _config(raw).get(labels.SPAWNED_BY)
        ),
        key=lambda name: short_name(cfg, name),
    )


def adopt(
    cfg: Config,
    incus: Incus,
    orchestrator_full: str,
    children: Sequence[str],
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> AdoptResult:
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
    already = tuple(
        name
        for name in unique
        if name in rows and _config(rows[name]).get(labels.SPAWNED_BY) == orchestrator_full
    )
    pending = tuple(name for name in unique if name not in already)
    for name in unique:
        problem = _child_problem(cfg, rows, orchestrator_full, name)
        if problem:
            problems.append(problem)
    if effective is not None:
        from jailbee.orchestration.actions import slot_breakdown

        root = root if root is not None else state.state_root()
        mine = {
            name
            for name, raw in rows.items()
            if _config(raw).get(labels.SPAWNED_BY) == orchestrator_full
        }
        running_names = {
            name
            for name in mine | set(pending)
            if name in rows and rows[name].get("status") == "Running"
        }
        running = slot_breakdown(
            cfg.container_prefix, root, orchestrator_full, mine, running_names, set(rows)
        )[3]
        if running > effective.max_running_children:
            problems.append(
                f"limit of {effective.max_running_children} running children exceeded; "
                "use `jb orchestrate grant --children`"
            )
    elif grant is not None and not problems:
        problems.append("orchestration grant is not effective")
    if problems:
        raise AdoptError("; ".join(problems))
    root = root if root is not None else state.state_root()
    now = now if now is not None else datetime.now(UTC)
    warnings = []
    for name in pending:
        config = _config(rows[name])
        for key in CHILD_STATE_LABELS:
            if key in config:
                incus.config_unset_checked(name, key)
        incus.config_set(name, labels.ON_DONE, labels.ON_DONE_KEEP)
        incus.config_set(name, labels.SPAWNED_BY, orchestrator_full)
        for recipient, sender, body, seq in (
            (
                name,
                orchestrator_full,
                ADOPTED_CHILD_BODY.format(orchestrator=short_name(cfg, orchestrator_full)),
                0,
            ),
            (
                orchestrator_full,
                name,
                ADOPTED_ORCHESTRATOR_BODY.format(child=short_name(cfg, name)),
                1,
            ),
        ):
            warning = _notify(cfg, incus, root, rows, recipient, sender, "adopted", body, now, seq)
            if warning:
                warnings.append(warning)
    return AdoptResult(adopted=pending, already=already, warnings=tuple(warnings))


def release(
    cfg: Config,
    incus: Incus,
    child_full: str,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> ReleaseResult:
    rows = _rows(incus)
    config = _config(rows.get(child_full, {}))
    parent = config.get(labels.SPAWNED_BY)
    if not isinstance(parent, str) or not parent:
        raise AdoptError(f"'{short_name(cfg, child_full)}' is not a child")
    root = root if root is not None else state.state_root()
    now = now if now is not None else datetime.now(UTC)
    revoked = rights.revoke_owned(cfg, incus, child_full)
    incus.config_unset_checked(child_full, labels.SPAWNED_BY)
    try:
        for key in CHILD_STATE_LABELS:
            if key in config:
                incus.config_unset_checked(child_full, key)
        watch.cancel(root, parent, child_full)
        state.purge_pending(root, child_full)
    except Exception:
        incus.config_set(child_full, labels.SPAWNED_BY, parent)
        raise
    warning = _notify(
        cfg,
        incus,
        root,
        rows,
        parent,
        child_full,
        "released",
        RELEASED_ORCHESTRATOR_BODY.format(child=short_name(cfg, child_full)),
        now,
        0,
    )
    return ReleaseResult(
        orchestrator=parent, revoked=tuple(revoked), warnings=(warning,) if warning else ()
    )
