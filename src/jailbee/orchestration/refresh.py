"""Accept service refresh requests and persist their validated plan."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from jailbee import container_app, lifecycle
from jailbee.config import CONTAINER_USERNAME
from jailbee.orchestration import actions, agent_watch
from jailbee.orchestration.protocol import RefreshManifest
from jailbee.services.elevated import Plan, RefreshError, foreign_agents, orchestrator_policy

if TYPE_CHECKING:
    from jailbee.orchestration.protocol import Manifest


def handle_refresh(ctx: actions.ActionContext, m: Manifest) -> actions.Outcome:
    if not isinstance(m, RefreshManifest):
        return actions.refused("not a refresh manifest")
    child = ctx.child(m.child)
    if isinstance(child, actions.Outcome):
        return child
    if not child.running:
        return actions.refused(f"child `{child.short}` is not running")
    kind = actions.live_job_kind(child.full)
    if kind is not None:
        return actions.refused(
            f"child `{child.short}` is busy ({kind} running); refresh again when it finishes"
        )
    try:
        bounded = ctx.incus.with_exec_timeout(actions.CHILD_EXEC_TIMEOUT_S)
        repo = lifecycle.container_repo_dir(ctx.cfg, bounded, child.full)
        result = container_app.run_in_container(
            ctx.cfg,
            bounded,
            child.full,
            ["service", "--repo", repo, "plan", "--op", "refresh", "--json", *m.services],
            uid=ctx.cfg.container_user.uid,
            cwd=repo,
            env={"HOME": f"/home/{CONTAINER_USERNAME}"},
            init_groups=True,
        )
        if result.returncode:
            return actions.refused(
                result.stderr.decode(errors="replace").strip() or "container service plan failed"
            )
        plan = Plan.from_payload(result.stdout, m.services)
        grant = ctx.grant()
        if grant is None:
            return actions.refused(actions.NOT_ALLOWED)
        problems = orchestrator_policy(ctx.cfg, grant, plan)
        if problems:
            return actions.refused("refresh needs " + "; ".join(problems))
        if plan.elevated:
            status = agent_watch.probe(bounded, child.full, uid=ctx.cfg.container_user.uid)
            if status is None:
                return actions.refused(
                    f"could not determine whether an agent is running in {child.full}; "
                    "refresh refused"
                )
            foreign = foreign_agents(ctx.cfg, bounded, child.full, status)
            if foreign:
                return actions.refused(
                    f"agents not started by the broker are running in {child.full} "
                    f"({', '.join(foreign)}); ask the user to close them, then refresh again"
                )
    except (container_app.ContainerJailbeeError, RefreshError) as exc:
        return actions.refused(str(exc))
    import os
    import tempfile
    from pathlib import Path

    from jailbee.create_flow import launch_refresh_background
    from jailbee.db import state_dir

    path: Path | None = None
    try:
        directory = state_dir() / "refresh-jobs"
        directory.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix="refresh-", suffix=".json", dir=directory)
        path = Path(filename)
        with os.fdopen(fd, "w", encoding="utf-8") as job_file:
            job_file.write(
                json.dumps(
                    {
                        "child_full": child.full,
                        "orchestrator_full": ctx.sender.full,
                        "manifest_id": m.id,
                        "elevated": plan.elevated,
                        "payload": plan.to_json(),
                        "names": list(plan.named),
                        "force": m.force,
                    }
                ),
            )
        worker = launch_refresh_background(ctx.cfg, container_name=child.full, job_path=path)
    except Exception as exc:
        if path is not None:
            path.unlink(missing_ok=True)
        return actions.failed(f"could not launch refresh: {exc}")
    return actions.Outcome(
        "started",
        f"refresh of {', '.join(plan.named)} in `{child.short}` started; log: {worker.log_path}",
    )
