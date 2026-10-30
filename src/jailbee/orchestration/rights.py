"""Runtime rights transactions, using the same host mutations as the CLI."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from jailbee import egress_scope, lifecycle, mounts, net_switch
from jailbee.orchestration import labels, profiles
from jailbee.orchestration.actions import Outcome, done, failed, refused
from jailbee.orchestration.grant import ChildRights, Grant, check_rights
from jailbee.orchestration.protocol import MessageManifest, RightsManifest

if TYPE_CHECKING:
    from jailbee.orchestration.actions import ActionContext, Node

log = logging.getLogger(__name__)


def _deadline(raw: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
    return value if value is not None and value.utcoffset() is not None else None


def _timed(ctx: ActionContext, child: Node) -> dict[str, str]:
    raw = ctx.incus.config_get(child.full, labels.TIMED)
    parsed = json.loads(raw) if raw else {}
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _deadline(v) is not None
        for k, v in parsed.items()
    ):
        raise ValueError("invalid orchestration timed rights label")
    return parsed


def _label_items(rights: ChildRights) -> set[str]:
    return {
        *(("loose",) if rights.loose else ()),
        *(f"mount:{m}" for m in rights.mounts),
        *(f"egress:{e}" for e in rights.egress),
    }


def _save(ctx: ActionContext, child: Node, owned: set[str], timed: dict[str, str]) -> None:
    rights = ChildRights(
        loose="loose" in owned,
        mounts=tuple(k.removeprefix("mount:") for k in owned if k.startswith("mount:")),
        egress=tuple(k.removeprefix("egress:") for k in owned if k.startswith("egress:")),
    )
    ctx.incus.config_set(child.full, labels.RIGHTS, rights.to_label())
    if timed:
        ctx.incus.config_set(child.full, labels.TIMED, json.dumps(timed, sort_keys=True))
    else:
        ctx.incus.config_unset_checked(child.full, labels.TIMED)


@dataclass(frozen=True)
class Held:
    present: bool
    until: datetime | None


def _held(ctx: ActionContext, child: Node, item: str) -> Held:
    name = child.full
    if item == "loose":
        return Held(
            lifecycle.current_network_mode(ctx.cfg, ctx.incus, name) == "loose",
            _deadline(ctx.incus.config_get(name, "user.jailbee.loose_until")),
        )
    kind, value = item.split(":", 1)
    if kind == "mount":
        return Held(
            mounts.is_attached(ctx.incus, name, value),
            _deadline(ctx.incus.config_get(name, mounts.until_key(value))),
        )
    return Held(
        value in egress_scope.container_extras(ctx.incus, name)
        or value in ctx.cfg.effective_egress_allow(),
        _deadline(egress_scope.egress_deadlines(ctx.incus, name).get(value)),
    )


def _apply(ctx: ActionContext, child: Node, item: str, held: Held) -> None:
    if item == "loose":
        net_switch.switch_mode(
            ctx.cfg, ctx.incus, child.full, "loose" if held.present else "strict", until=held.until
        )
        return
    kind, value = item.split(":", 1)
    if kind == "mount":
        if held.present:
            mounts.attach(ctx.cfg, ctx.incus, child.full, value, until=held.until)
        elif mounts.is_attached(ctx.incus, child.full, value):
            mounts.remove_optional_mount(ctx.cfg, ctx.incus, child.full, value)
    elif held.present:
        egress_scope.add_container_entry(ctx.cfg, ctx.incus, child.full, value, until=held.until)
    else:
        egress_scope.remove_container_entry(ctx.cfg, ctx.incus, child.full, value)


def handle_rights(ctx: ActionContext, m: RightsManifest) -> Outcome:
    from jailbee.config import parse_ttl
    from jailbee.egress import is_wildcard_entry
    from jailbee.network_generation import generation_of

    if ctx.role() == "child":
        if "grant" in m.model_fields_set or m.child not in (None, ctx.sender.short):
            return refused("children may only revoke their own rights")
        child = ctx.sender
    else:
        if m.child is None:
            return refused("`child` is required for rights from an orchestrator")
        target = ctx.child(m.child)
        if isinstance(target, Outcome):
            return target
        child = target
    grants = m.grant.items() if m.grant else ()
    revokes = m.revoke.items() if m.revoke else ()
    if m.grant is not None:
        # The listing gives role/lineage; permission is reread at application time.
        raw = ctx.incus.config_get(ctx.sender.full, labels.GRANT)
        if not raw:
            return refused("orchestration is not allowed in this repo")
        grant = Grant.from_label(raw)
        if "rights" not in grant.operations:
            return refused("operation `rights` is not in this container's grant")
        problems = check_rights(
            grant,
            profiles.repo_cap(ctx.cfg),
            ChildRights(loose=m.grant.loose, mounts=m.grant.mounts, egress=m.grant.egress),
            ctx.cfg.optional_mounts.keys(),
        )
        if problems:
            return refused("; ".join(problems))
        try:
            for entry in m.grant.egress:
                if is_wildcard_entry(entry):
                    raw_child = next(
                        (r for r in ctx.incus.list_containers() if r.get("name") == child.full), {}
                    )
                    if generation_of(ctx.cfg, raw_child) != "work":
                        return refused(
                            "container-scope wildcards need the work network "
                            "(run `jailbee net migrate`), or add it with --repo"
                        )
                else:
                    egress_scope.resolve_entries([entry])
        except ValueError as exc:
            return refused(str(exc))
    try:
        raw_rights = ctx.incus.config_get(child.full, labels.RIGHTS)
        rights = ChildRights.from_label(raw_rights) if raw_rights else ChildRights()
        owned = _label_items(rights)
        timed = _timed(ctx, child)
        old_owned, old_timed = owned.copy(), timed.copy()
        before = {item: _held(ctx, child, item) for item in (*revokes, *grants)}
        for item, held in before.items():
            deadline = _deadline(timed.get(item))
            if deadline is not None and held.until != deadline:
                # The live expiry moved (ended, or a human re-added or changed
                # the right); what is held now is not ours.
                timed.pop(item, None)
    except Exception as exc:
        return failed(str(exc))
    changes: list[str] = []
    undo: list[str] = []
    gave_up: list[str] = []
    try:
        for item in revokes:
            pretty = item.replace(":", " ", 1)
            if item not in owned and item not in timed:
                value = item.split(":", 1)[-1]
                changes.append(f"{value} was not given by orchestration; left as is")
                continue
            undo.append(item)
            # Absent rights are successful no-ops, including already-expired mounts.
            if before[item].present:
                _apply(ctx, child, item, Held(False, None))
            owned.discard(item)
            timed.pop(item, None)
            changes.append(f"revoked {pretty}")
            gave_up.append(pretty)
        duration = parse_ttl(m.grant.for_) if m.grant else None
        requested = ctx.now + duration if duration is not None else None
        for item in grants:
            assert m.grant is not None
            held = before[item]
            pretty = item.replace(":", " ", 1)
            if held.present and item not in owned and item not in timed:
                changes.append(f"{item.split(':', 1)[-1]} is held by hand; left as is")
                continue
            if item in owned or (held.present and held.until is None):
                changes.append(f"{pretty} already held permanently")
                continue
            existing = [d for d in (held.until, _deadline(timed.get(item))) if d is not None]
            until = max([requested, *existing]) if requested is not None else None
            undo.append(item)
            _apply(ctx, child, item, Held(True, until))
            if until is None:
                owned.add(item)
                timed.pop(item, None)
            else:
                timed[item] = until.isoformat()
            changes.append(f"{pretty} ({'never' if until is None else 'for ' + m.grant.for_})")
        _save(ctx, child, owned, timed)
    except Exception as exc:
        for item in reversed(undo):
            try:
                _apply(ctx, child, item, before[item])
            except Exception as rollback:
                log.warning("rights rollback %s/%s failed: %s", child.full, item, rollback)
        try:
            _save(ctx, child, old_owned, old_timed)
        except Exception as rollback:
            log.warning("rights provenance rollback %s failed: %s", child.full, rollback)
        return failed(str(exc))
    if ctx.role() == "child" and gave_up:
        from jailbee.orchestration.messages import _orchestrator_of, _relay

        notice = MessageManifest(
            version=1,
            type="message",
            id=m.id,
            kind="note",
            body=f"{child.short} gave up: {', '.join(gave_up)}",
        )
        _relay(ctx, _orchestrator_of(ctx), notice)
    return done(f"{child.short}: {'; '.join(changes)}")
