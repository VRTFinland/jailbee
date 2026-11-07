"""Detached broker refresh execution."""

import fcntl
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlmodel import Session

from jailbee import background, db, docker_daemon
from jailbee.db.models import JOB_REFRESH
from jailbee.orchestration import actions, agent_watch, labels, outbox, state, watch
from jailbee.orchestration.grant import Grant, effective_grant
from jailbee.orchestration.profiles import repo_cap
from jailbee.orchestration.protocol import InboxStatus, _checked_id, new_host_id
from jailbee.services import elevated

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def _deliver(
    cfg: "Config",
    incus: "Incus",
    path: Path,
    owner: str,
    owner_uuid: str,
    ref: str,
    *,
    ok: bool,
    detail: str,
    item: InboxStatus | None = None,
) -> int:
    try:
        try:
            recipient = next((r for r in incus.list_containers() if r.get("name") == owner), {})
        except Exception:
            recipient = {}
        config = recipient.get("config")
        uuid = config.get("volatile.uuid") if isinstance(config, dict) else None
        if not isinstance(uuid, str) or not uuid:
            raise ValueError("recipient instance identity unavailable; retry notification")
        if uuid != owner_uuid:
            path.rename(path.with_suffix(".quarantined"))
            raise ValueError("recipient instance identity changed; refresh job quarantined")
        item = item or InboxStatus(
            version=1,
            id=new_host_id(),
            type="status",
            ref=ref,
            state="done" if ok else "failed",
            detail=detail,
        )
        outbox.deliver(
            incus,
            state.state_root(),
            owner,
            item,
            running=recipient.get("status") == "Running",
            uid=cfg.container_user.uid,
            now=datetime.now(UTC),
            recipient_uuid=owner_uuid,
        )
        path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"could not deliver refresh status: {exc}")
        return 1
    return 0 if ok else 1


def run_job(cfg: "Config", incus: "Incus", path: Path) -> int:
    root = state.state_root()
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(os.fsencode(path.absolute())).hexdigest()
    with (root / f"refresh-{digest}.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _run_job_locked(cfg, incus, path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _run_job_locked(cfg: "Config", incus: "Incus", path: Path) -> int:
    try:
        raw = path.read_bytes()
        if len(raw) > 300 * 1024:
            raise ValueError("job too large")
        job = json.loads(raw, object_pairs_hook=elevated._object)
        if not isinstance(job, dict):
            raise ValueError("invalid job schema")
        child, owner, ref = (job[k] for k in ("child_full", "orchestrator_full", "manifest_id"))
        if any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", v)
            for v in (child, owner)
        ):
            raise ValueError("invalid container name")
        if not isinstance(ref, str):
            raise ValueError("invalid manifest id")
        _checked_id(ref)
        owner_uuid = job["orchestrator_uuid"]
        if not isinstance(owner_uuid, str) or not owner_uuid:
            raise ValueError("invalid orchestrator instance identity")
    except Exception as exc:
        print(f"invalid refresh job identity: {exc}")
        return 1
    if "final_status" in job:
        try:
            final = InboxStatus.model_validate(job["final_status"])
            if final.ref != ref or final.state not in ("done", "failed"):
                raise ValueError("invalid final status")
        except Exception as exc:
            print(f"invalid saved refresh status: {exc}")
            return 1
        return _deliver(
            cfg,
            incus,
            path,
            owner,
            owner_uuid,
            ref,
            ok=final.state == "done",
            detail=final.detail,
            item=final,
        )
    if job.get("execution_phase") == "executing":
        print("refresh execution outcome is uncertain; manual recovery required")
        return 1
    try:
        if set(job) != {
            "child_full",
            "orchestrator_full",
            "orchestrator_uuid",
            "manifest_id",
            "elevated",
            "payload",
            "names",
            "force",
        }:
            raise ValueError("invalid job schema")
        names = job["names"]
        if not isinstance(names, list) or not names or len(names) > 32:
            raise ValueError("invalid names")
        if type(job["force"]) is not bool or type(job["elevated"]) is not bool:
            raise ValueError("invalid flags")
        plan = elevated.Plan.from_payload(json.dumps(job["payload"]), names)
        if plan.elevated != job["elevated"]:
            raise ValueError("elevation mismatch")
    except Exception as exc:
        return _deliver(
            cfg, incus, path, owner, owner_uuid, ref, ok=False, detail=f"invalid refresh job: {exc}"
        )

    pid = os.getpid()
    claimed = False
    engine = None
    ok = False
    detail = ""
    try:
        engine = db.get_engine()
        if engine is None:
            raise elevated.RefreshError("job database unavailable")
        with (
            watch.mutation_locked(state.state_root()),
            watch.locked(state.state_root()),
            Session(engine) as session,
        ):
            try:
                if path.read_bytes() != raw:
                    return 1
            except FileNotFoundError:
                return 1
            claimed = background.claim_job(
                session,
                container_name=child,
                container_prefix=cfg.container_prefix,
                pid=pid,
                phase="refresh",
                now=datetime.now(UTC),
                op_kind=JOB_REFRESH,
            )
        if not claimed:
            raise elevated.RefreshError(f"child `{child}` is busy; refresh again when it finishes")
        rows = incus.list_containers()
        owner_row = next((r for r in rows if r.get("name") == owner), None)
        child_row = next((r for r in rows if r.get("name") == child), None)
        if (
            not owner_row
            or owner_row.get("status") != "Running"
            or not child_row
            or child_row.get("status") != "Running"
        ):
            raise elevated.RefreshError("orchestrator or child is not running")
        child_config = child_row.get("config")
        owner_config = owner_row.get("config")
        if (
            not isinstance(child_config, dict)
            or child_config.get(labels.SPAWNED_BY) != owner
            or not isinstance(owner_config, dict)
        ):
            raise elevated.RefreshError("child ownership could not be verified")
        if owner_config.get("volatile.uuid") != owner_uuid:
            raise elevated.RefreshError("orchestrator instance identity changed")
        grant = effective_grant(Grant.from_label(owner_config.get(labels.GRANT, "")), repo_cap(cfg))
        if grant is None or "refresh" not in grant.operations:
            raise elevated.RefreshError("refresh grant revoked")
        problems = elevated.orchestrator_policy(cfg, grant, plan)
        if problems:
            raise elevated.RefreshError("refresh needs " + "; ".join(problems))
        job["execution_phase"] = "executing"
        state.atomic_write_text(path, json.dumps(job))
        if plan.elevated:
            status = agent_watch.probe(incus, child, uid=cfg.container_user.uid)
            if status is None:
                raise elevated.RefreshError(
                    f"could not determine whether an agent is running in {child}; refresh refused"
                )
            foreign = elevated.foreign_agents(cfg, incus, child, status)
            if foreign:
                raise elevated.RefreshError(
                    f"foreign agents running in {child}: {', '.join(foreign)}"
                )
            short = owner.removeprefix(cfg.container_prefix + "-")

            def before_resume() -> None:
                watch.register(
                    state.state_root(),
                    owner,
                    watch.WatchEntry(
                        ref,
                        child,
                        "restart",
                        datetime.now(UTC).isoformat(),
                        supervise=True,
                        agent_only=True,
                    ),
                )
                for key in (
                    labels.STOPPED_HEAD,
                    labels.DONE_SHA,
                    labels.STATE,
                    labels.PENDING_DESTROY,
                    labels.STOPPED_DIRTY,
                ):
                    incus.config_unset_checked(child, key)

            report = elevated.run_refresh(
                cfg,
                incus,
                child,
                plan,
                who=f"requested by orchestrator {short}",
                resume_prompt_prefix=actions.child_preamble(short) + "\n\n",
                before_resume=before_resume,
                recovery_child=child.removeprefix(cfg.container_prefix + "-"),
                mirror_endpoint=docker_daemon.mirror_endpoint_or_none(cfg, incus),
                force=job["force"],
            )
        else:
            report = elevated.run_plain_refresh(cfg, incus, child, plan.named)
        ok, detail = report.ok, report.summary()
    except Exception as exc:
        detail = f"refresh of {', '.join(plan.named)} in {child} failed: {exc}"
    finally:
        try:
            final = InboxStatus(
                version=1,
                id=new_host_id(),
                type="status",
                ref=ref,
                state="done" if ok else "failed",
                detail=detail,
            )
            pending = (
                final.model_copy(
                    update={
                        "state": "failed",
                        "detail": detail + "; refresh job cleanup unconfirmed",
                    }
                )
                if claimed
                else final
            )
            job["final_status"] = pending.model_dump(mode="json")
            state.atomic_write_text(path, json.dumps(job))
        except Exception as exc:
            print(f"could not persist refresh status: {exc}")
            if claimed and engine is not None:
                try:
                    with Session(engine) as session:
                        background.release_job(session, child, pid)
                except Exception as release_exc:
                    print(f"could not release refresh job: {release_exc}")
            return 1
        if claimed and engine is not None:
            try:
                with Session(engine) as session:
                    background.release_job(session, child, pid)
            except Exception as exc:
                ok = False
                detail += f"; could not release refresh job: {exc}"
                final = final.model_copy(update={"state": "failed", "detail": detail})
                job["final_status"] = final.model_dump(mode="json")
                try:
                    state.atomic_write_text(path, json.dumps(job))
                except Exception as persist_exc:
                    print(f"could not persist cleanup failure: {persist_exc}")
                    return 1
            else:
                job["final_status"] = final.model_dump(mode="json")
                try:
                    state.atomic_write_text(path, json.dumps(job))
                except Exception as persist_exc:
                    print(f"could not persist completed cleanup: {persist_exc}")
                    return 1
    return _deliver(cfg, incus, path, owner, owner_uuid, ref, ok=ok, detail=detail, item=final)
