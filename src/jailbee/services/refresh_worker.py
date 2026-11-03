"""Detached broker refresh execution."""

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
from jailbee.orchestration.protocol import InboxStatus, _checked_id, new_host_id
from jailbee.orchestration.profiles import repo_cap
from jailbee.services import elevated

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def run_job(cfg: "Config", incus: "Incus", path: Path) -> int:
    try:
        raw = path.read_bytes()
        if len(raw) > 300 * 1024:
            raise ValueError("job too large")
        job = json.loads(raw, object_pairs_hook=elevated._object)
        if not isinstance(job, dict) or set(job) != {"child_full", "orchestrator_full", "manifest_id", "elevated", "payload", "names", "force"}:
            raise ValueError("invalid job schema")
        child, owner, ref = (job[k] for k in ("child_full", "orchestrator_full", "manifest_id"))
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", v) for v in (child, owner)):
            raise ValueError("invalid container name")
        if not isinstance(ref, str):
            raise ValueError("invalid manifest id")
        _checked_id(ref)
        names = job["names"]
        if not isinstance(names, list) or not names or len(names) > 32:
            raise ValueError("invalid names")
        if type(job["force"]) is not bool or type(job["elevated"]) is not bool:
            raise ValueError("invalid flags")
        plan = elevated.Plan.from_payload(json.dumps(job["payload"]), names)
        if plan.elevated != job["elevated"]:
            raise ValueError("elevation mismatch")
    except Exception as exc:
        print(f"invalid refresh job: {exc}")
        return 1

    pid = os.getpid()
    claimed = False
    engine = None
    ok = False
    detail = ""
    try:
        engine = db.get_engine()
        if engine is None:
            raise elevated.RefreshError("job database unavailable")
        with Session(engine) as session:
            claimed = background.claim_job(session, container_name=child, container_prefix=cfg.container_prefix, pid=pid, phase="refresh", now=datetime.now(UTC), op_kind=JOB_REFRESH)
        if not claimed:
            raise elevated.RefreshError(f"child `{child}` is busy; refresh again when it finishes")
        rows = incus.list_containers()
        owner_row = next((r for r in rows if r.get("name") == owner), None)
        child_row = next((r for r in rows if r.get("name") == child), None)
        if not owner_row or owner_row.get("status") != "Running" or not child_row or child_row.get("status") != "Running":
            raise elevated.RefreshError("orchestrator or child is not running")
        child_config = child_row.get("config")
        owner_config = owner_row.get("config")
        if not isinstance(child_config, dict) or child_config.get(labels.SPAWNED_BY) != owner or not isinstance(owner_config, dict):
            raise elevated.RefreshError("child ownership could not be verified")
        grant = effective_grant(Grant.from_label(owner_config.get(labels.GRANT, "")), repo_cap(cfg))
        if grant is None or "refresh" not in grant.operations:
            raise elevated.RefreshError("refresh grant revoked")
        problems = elevated.orchestrator_policy(cfg, grant, plan)
        if problems:
            raise elevated.RefreshError("refresh needs " + "; ".join(problems))
        if plan.elevated:
            status = agent_watch.probe(incus, child, uid=cfg.container_user.uid)
            if status is None:
                raise elevated.RefreshError(f"could not determine whether an agent is running in {child}; refresh refused")
            foreign = elevated.foreign_agents(cfg, incus, child, status)
            if foreign:
                raise elevated.RefreshError(f"foreign agents running in {child}: {', '.join(foreign)}")
            short = owner.removeprefix(cfg.container_prefix + "-")

            def before_resume() -> None:
                watch.register(state.state_root(), owner, watch.WatchEntry(ref, child, "restart", datetime.now(UTC).isoformat(), supervise=True, agent_only=True))
                for key in (labels.STOPPED_HEAD, labels.DONE_SHA, labels.STATE, labels.PENDING_DESTROY, labels.STOPPED_DIRTY):
                    incus.config_unset_checked(child, key)

            report = elevated.run_refresh(cfg, incus, child, plan, who=f"requested by orchestrator {short}", resume_prompt_prefix=actions.child_preamble(short) + "\n\n", before_resume=before_resume, mirror_endpoint=docker_daemon.mirror_endpoint_or_none(cfg, incus), force=job["force"])
        else:
            report = elevated.run_plain_refresh(cfg, incus, child, plan.named)
        ok, detail = report.ok, report.summary()
    except Exception as exc:
        detail = f"refresh of {', '.join(plan.named)} in {child} failed: {exc}"
    finally:
        if claimed and engine is not None:
            try:
                with Session(engine) as session:
                    background.release_job(session, child, pid)
            except Exception as exc:
                ok = False
                detail += f"; could not release refresh job: {exc}"
    try:
        try:
            running = any(r.get("name") == owner and r.get("status") == "Running" for r in incus.list_containers())
        except Exception:
            running = False
        item = InboxStatus(version=1, id=new_host_id(), type="status", ref=ref, state="done" if ok else "failed", detail=detail)
        outbox.deliver(incus, state.state_root(), owner, item, running=running, uid=cfg.container_user.uid, now=datetime.now(UTC))
        path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"could not deliver refresh status: {exc}")
        return 1
    return 0 if ok else 1
