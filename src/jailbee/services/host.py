"""Host-side service operations; definitions always come from the container."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from jailbee.config import CONTAINER_USERNAME

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def _metadata(cfg: Config, incus: Incus, full: str, args: Sequence[str]) -> Any:
    from jailbee.container_app import ContainerJailbeeError, run_in_container
    from jailbee.lifecycle import container_repo_dir

    repo = container_repo_dir(cfg, incus, full)
    result = run_in_container(
        cfg,
        incus,
        full,
        ["service", "--repo", repo, *args],
        uid=cfg.container_user.uid,
        cwd=repo,
        env={"HOME": f"/home/{CONTAINER_USERNAME}"},
        init_groups=True,
    )
    if result.returncode:
        raise ContainerJailbeeError(
            result.stderr.decode(errors="replace").strip() or "container service command failed"
        )
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ContainerJailbeeError("invalid service metadata from container") from exc


def service_names(cfg: Config, incus: Incus, full: str) -> list[str]:
    return [str(item["name"]) for item in _metadata(cfg, incus, full, ["ls", "--json"])]


def refresh(
    cfg: Config,
    incus: Incus,
    full: str,
    names: Sequence[str],
    *,
    assume_yes: bool,
    force: bool = False,
) -> int:
    import os
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background, container_app, db, docker_daemon, prompting
    from jailbee.db.models import JOB_REFRESH, BackgroundJob
    from jailbee.lifecycle import container_repo_dir
    from jailbee.orchestration import actions, agent_watch, labels, state, watch
    from jailbee.orchestration.protocol import new_host_id
    from jailbee.services import elevated
    from jailbee.tui import error_plain, info_plain

    claimed = False
    engine = None
    pid = os.getpid()
    try:
        repo = container_repo_dir(cfg, incus, full)
        result = container_app.run_in_container(
            cfg,
            incus,
            full,
            ["service", "--repo", repo, "plan", "--op", "refresh", "--json", *names],
            uid=cfg.container_user.uid,
            cwd=repo,
            env={"HOME": f"/home/{CONTAINER_USERNAME}"},
            init_groups=True,
        )
        if result.returncode:
            raise elevated.RefreshError(
                result.stderr.decode(errors="replace").strip() or "container service plan failed"
            )
        plan = elevated.Plan.from_payload(result.stdout, names)
        problems = elevated.host_policy(cfg, plan)
        if problems:
            raise elevated.RefreshError("; ".join(problems))
        if not plan.elevated:
            return run_op(cfg, incus, full, "refresh", names)
        engine = db.get_engine()
        if engine is None:
            raise elevated.RefreshError("job database unavailable")
        with (
            watch.mutation_locked(state.state_root()),
            watch.locked(state.state_root()),
            Session(engine) as session,
        ):
            claimed = background.claim_job(
                session,
                container_name=full,
                container_prefix=cfg.container_prefix,
                pid=pid,
                phase="refresh",
                now=datetime.now(UTC),
                op_kind=JOB_REFRESH,
            )
            if not claimed:
                row = session.get(BackgroundJob, full)
                kind = row.op_kind if row is not None else "another operation"
                raise elevated.RefreshError(
                    f"child `{full}` is busy ({kind} running); refresh again when it finishes"
                )
        status = agent_watch.probe(incus, full, uid=cfg.container_user.uid)
        if status is None:
            raise elevated.RefreshError(
                f"could not determine whether an agent is running in {full}; refresh refused"
            )
        foreign = elevated.foreign_agents(cfg, incus, full, status)
        if foreign and not assume_yes:
            message = (
                "Agents you started by hand keep running during the elevated window: "
                f"{', '.join(foreign)}. Continue?"
            )
            if not prompting.is_interactive() or not prompting.confirm(message, default=False):
                raise elevated.RefreshError(message + " Refresh refused.")
        owner = incus.config_get_checked(full, labels.SPAWNED_BY)
        prefix = ""
        before_resume = None
        if owner:
            short = owner.removeprefix(cfg.container_prefix + "-")
            prefix = actions.child_preamble(short) + "\n\n"

            def register_supervision() -> None:
                watch.register(
                    state.state_root(),
                    owner,
                    watch.WatchEntry(
                        new_host_id(),
                        full,
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
                    incus.config_unset_checked(full, key)

            before_resume = register_supervision
        report = elevated.run_refresh(
            cfg,
            incus,
            full,
            plan,
            who="requested by the user on the host",
            before_resume=before_resume,
            resume_prompt_prefix=prefix,
            recovery_child=full.removeprefix(cfg.container_prefix + "-") if owner else None,
            mirror_endpoint=docker_daemon.mirror_endpoint_or_none(cfg, incus),
            force=force,
            on_progress=lambda name, stage, status: info_plain(f"{name}: {stage}: {status}"),
        )
        print(report.summary())
        return 0 if report.ok else 1
    except (container_app.ContainerJailbeeError, elevated.RefreshError) as exc:
        error_plain(str(exc))
        return 1
    except Exception as exc:
        error_plain(f"refresh failed: {exc}")
        return 1
    finally:
        if claimed and engine is not None:
            try:
                with Session(engine) as session:
                    background.release_job(session, full, pid)
            except Exception as exc:
                error_plain(f"could not release refresh job: {exc}")
                return 1


def run_op(
    cfg: Config,
    incus: Incus,
    full: str,
    op: str,
    names: Sequence[str],
    *,
    extra: Sequence[str] = (),
) -> int:
    from jailbee.container_app import run_in_container_interactive
    from jailbee.lifecycle import container_repo_dir

    repo = container_repo_dir(cfg, incus, full)
    return run_in_container_interactive(
        cfg,
        incus,
        full,
        ["service", "--repo", repo, op, *names, *extra],
        uid=cfg.container_user.uid,
        cwd=repo,
    )
