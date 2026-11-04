"""Execute selected host-configured autostart stages in a running container."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from jailbee.autostart_plan import (
    TRIGGER_ORDER,
    AutostartTrigger,
    RerunTarget,
    rerun_block,
    rerunnable_stages,
)

if TYPE_CHECKING:
    from jailbee.config import Autostart, Config
    from jailbee.incus import Incus


def failed_targets(cfg: Config, container: str) -> list[RerunTarget]:
    """Return current failed steps; callers must supply an already-profiled config."""
    from jailbee.autostart_outcomes import failed_steps, read

    failed = set(failed_steps(read(container)))
    return [
        RerunTarget(trigger, stage.stage, step.name)
        for trigger in TRIGGER_ORDER
        for stage in rerunnable_stages(cfg.autostart, trigger)
        for chain in stage.all_chains()
        for step in chain.steps
        if (trigger.value, stage.stage, step.name) in failed
    ]


def effective_config(cfg: Config, incus: Incus, container: str) -> Config:
    """Resolve the stored host profile before selecting explicit or failed targets."""
    from jailbee.autostart_profiles import profiled_config, resolve_profile

    return profiled_config(cfg, resolve_profile(cfg, incus, container))


def prepare(
    cfg: Config, incus: Incus, container: str, targets: Sequence[RerunTarget]
) -> Autostart:
    from jailbee.autostart_status import refuse_while_running

    refuse_while_running(cfg, container)
    if not any(
        row.get("name") == container and row.get("status") == "Running"
        for row in incus.list_containers()
    ):
        raise RuntimeError(f"Container '{container}' must be running to re-run autostart")
    effective = effective_config(cfg, incus, container)
    return rerun_block(effective.autostart, targets)


def run_foreground(
    cfg: Config,
    incus: Incus,
    container: str,
    block: Autostart,
    *,
    on_progress: Callable[[str, str, str], None] | None = None,
) -> None:
    from jailbee.autostart import run_autostart
    from jailbee.autostart_status import refuse_while_running
    from jailbee.docker_daemon import mirror_endpoint_or_none
    from jailbee.lifecycle import container_repo_dir

    refuse_while_running(cfg, container)
    agents = {
        name: agent.model_copy(update={"autostart": False})
        for name, agent in cfg.agents.items()
    }
    run_cfg = cfg.model_copy(update={"autostart": block, "agents": agents})
    repo_dir = container_repo_dir(cfg, incus, container)
    mirror_endpoint = mirror_endpoint_or_none(cfg, incus)
    for trigger in TRIGGER_ORDER:
        if getattr(block, trigger.value):
            run_autostart(
                run_cfg,
                incus,
                container,
                trigger,
                repo_dir,
                override="wait",
                skip_services=True,
                skip_agents=True,
                record_on_create=bool(cfg.autostart.profiles),
                on_progress=on_progress,
                mirror_endpoint=mirror_endpoint,
            )


def first_trigger(block: Autostart) -> AutostartTrigger:
    for trigger in TRIGGER_ORDER:
        if getattr(block, trigger.value):
            return trigger
    raise ValueError("No autostart stages selected for re-run")
