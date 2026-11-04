"""Execute selected host-configured autostart stages in a running container."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
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


def _protected_windows(cfg: Config) -> set[str]:
    from jailbee.services.runtime import WINDOW_PREFIX
    from jailbee.tmux import window_for

    # Configured agents may already be running even when autostart is off.
    return {window_for(name) for name in cfg.agents} | {
        WINDOW_PREFIX + name for name in cfg.services
    }


def validate_windows(cfg: Config, block: Autostart) -> None:
    """Refuse a rerun before an ordinary step can replace an agent/service window."""
    from jailbee.autostart_plan import RerunSelectionError, normalize_stages
    from jailbee.tmux import window_for

    protected = _protected_windows(cfg)
    for trigger in TRIGGER_ORDER:
        for stage in normalize_stages(getattr(block, trigger.value)):
            for chain in stage.all_chains():
                for step in chain.steps:
                    window = window_for(step.name)
                    if window in protected:
                        raise RerunSelectionError(
                            f"Cannot re-run {trigger.value}:{stage.stage}/{step.name}: "
                            f"protected agent/service window '{window}'",
                            1,
                        )


def validate_remote(block: Autostart) -> None:
    """Remote container commands cannot borrow host network/mount authority."""
    from jailbee.autostart_plan import normalize_stages
    from jailbee.remote_ssh.session import is_remote_session

    if not is_remote_session():
        return
    for trigger in TRIGGER_ORDER:
        for stage in normalize_stages(getattr(block, trigger.value)):
            if (
                stage.network is not None
                or stage.mounts
                or any(
                    step.network is not None or step.mounts
                    for chain in stage.all_chains()
                    for step in chain.steps
                )
            ):
                raise RuntimeError(
                    f"A remote autostart re-run cannot change network or mounts "
                    f"({trigger.value}:{stage.stage}); run it on the host"
                )


def eligible_autostart(cfg: Config) -> Autostart:
    """Hide unsafe stages, without changing creation-time config validity."""
    from jailbee.tmux import window_for

    protected = _protected_windows(cfg)
    return cfg.autostart.model_copy(
        update={
            trigger.value: [
                stage
                for stage in rerunnable_stages(cfg.autostart, trigger)
                if not any(
                    window_for(step.name) in protected
                    for chain in stage.all_chains()
                    for step in chain.steps
                )
            ]
            for trigger in TRIGGER_ORDER
        }
    )


def failed_targets(cfg: Config, container: str) -> list[RerunTarget]:
    """Return current failed steps; callers must supply an already-profiled config."""
    from jailbee.autostart_outcomes import failed_steps, read

    failed = set(failed_steps(read(container)))
    return [
        RerunTarget(trigger, stage.stage, step.name)
        for trigger in TRIGGER_ORDER
        for stage in rerunnable_stages(eligible_autostart(cfg), trigger)
        for chain in stage.all_chains()
        for step in chain.steps
        if (trigger.value, stage.stage, step.name) in failed
    ]


def effective_config(cfg: Config, incus: Incus, container: str) -> Config:
    """Resolve the stored host profile before selecting explicit or failed targets."""
    from jailbee.autostart_profiles import profiled_config, resolve_profile

    return profiled_config(cfg, resolve_profile(cfg, incus, container))


def prepare(cfg: Config, incus: Incus, container: str, targets: Sequence[RerunTarget]) -> Autostart:
    from jailbee.autostart_status import refuse_while_running

    refuse_while_running(cfg, container)
    if not any(
        row.get("name") == container and row.get("status") == "Running"
        for row in incus.list_containers()
    ):
        raise RuntimeError(f"Container '{container}' must be running to re-run autostart")
    effective = effective_config(cfg, incus, container)
    block = rerun_block(effective.autostart, targets)
    validate_windows(cfg, block)
    validate_remote(block)
    return block


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

    validate_windows(cfg, block)
    validate_remote(block)
    refuse_while_running(cfg, container)
    agents = {
        name: agent.model_copy(update={"autostart": False}) for name, agent in cfg.agents.items()
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
                on_progress=on_progress,
                mirror_endpoint=mirror_endpoint,
            )


def first_trigger(block: Autostart) -> AutostartTrigger:
    for trigger in TRIGGER_ORDER:
        if getattr(block, trigger.value):
            return trigger
    raise ValueError("No autostart stages selected for re-run")


def list_rows(cfg: Config, container: str) -> list[dict[str, object]]:
    from jailbee.autostart_outcomes import read

    outcomes = read(container)
    rows: list[dict[str, object]] = []
    for trigger in TRIGGER_ORDER:
        for stage in rerunnable_stages(eligible_autostart(cfg), trigger):
            recorded = outcomes.get(trigger.value, {}).get(stage.stage, {})
            steps = []
            for chain in stage.all_chains():
                for step in chain.steps:
                    outcome = recorded.get(step.name)
                    steps.append(
                        {
                            "step": step.name,
                            "state": outcome.state if outcome else None,
                            "at": outcome.at if outcome else None,
                        }
                    )
            present = [recorded[step["step"]] for step in steps if step["step"] in recorded]
            failures = [outcome for outcome in present if outcome.state == "fail"]
            chosen = max(failures or present, key=lambda outcome: outcome.at, default=None)
            rows.append(
                {
                    "trigger": trigger.value,
                    "stage": stage.stage,
                    "state": chosen.state if chosen else None,
                    "at": chosen.at if chosen else None,
                    "steps": steps,
                }
            )
    return rows


def _command(
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    stages: Sequence[str],
    steps: Sequence[str],
    failed: bool,
    trigger: str | None,
    listing: bool,
    json_output: bool,
    wait: bool,
    spawn: Callable[[Config, Autostart], None],
) -> None:
    """Select a union of explicit and failed targets; trigger filters both."""
    import json

    import typer
    from rich.table import Table

    from jailbee import prompting
    from jailbee.autostart_plan import RerunSelectionError, resolve_rerun
    from jailbee.autostart_status import refuse_while_running
    from jailbee.lifecycle import short_name
    from jailbee.tui import console, hint_console, info

    if (listing and (stages or steps or failed or trigger or wait)) or (
        json_output and not listing
    ):
        raise RerunSelectionError(
            "--list excludes selectors, --trigger and --wait; --json requires --list", 2
        )
    selected_trigger = AutostartTrigger(trigger) if trigger else None
    if listing and json_output:
        # Profile fallback can warn; machine-readable stdout must stay JSON-only.
        # Flush captured diagnostics even if resolution raises.
        diagnostics = console.capture()
        try:
            with diagnostics:
                effective = effective_config(cfg, incus, container)
        finally:
            hint_console.print(diagnostics.get(), end="", markup=False, highlight=False)
    else:
        effective = effective_config(cfg, incus, container)
    if listing:
        rows = list_rows(effective, container)
        if json_output:
            typer.echo(json.dumps(rows))
        else:
            table = Table("Trigger", "Stage", "State", "At")
            for row in rows:
                table.add_row(
                    str(row["trigger"]),
                    str(row["stage"]),
                    str(row["state"] or "-"),
                    str(row["at"] or "-"),
                )
            console.print(table)
        return
    refuse_while_running(cfg, container)
    # Foreground lifecycle runs have no job row and hold the literal "1".
    # Unknown/dead claims also fail closed: only their owner may release them.
    from jailbee.incus import IncusError

    try:
        claim = incus.config_get_checked(container, "user.jailbee.autostart_in_progress")
    except IncusError as exc:
        raise RuntimeError(f"Cannot check autostart worker claim: {exc}") from exc
    if claim:
        raise RuntimeError(f"Autostart is already running for '{container}' (claim {claim})")
    targets = resolve_rerun(
        effective.autostart, stages=stages, steps=steps, trigger=selected_trigger
    )
    failures = [
        t
        for t in failed_targets(effective, container)
        if selected_trigger is None or t.trigger == selected_trigger
    ]
    if failed:
        targets.extend(t for t in failures if t not in targets)
    if not stages and not steps and not failed:
        if not prompting.is_interactive():
            raise prompting.MissingValue(
                "autostart selection", reason="pass --stage/--step/--failed or select interactively"
            )
        options: list[prompting.Option[list[RerunTarget]]] = []
        if failures:
            options.append(
                prompting.Option(failures, f"Re-run failed steps ({len(failures)})", "failed")
            )
        for t in TRIGGER_ORDER:
            if selected_trigger is not None and t != selected_trigger:
                continue
            for stage in rerunnable_stages(eligible_autostart(effective), t):
                target = RerunTarget(t, stage.stage, None)
                label = f"{t.value}: {stage.stage}"
                options.append(prompting.Option([target], label, label))
                for chain in stage.all_chains():
                    for step in chain.steps:
                        item = RerunTarget(t, stage.stage, step.name)
                        label = f"{t.value}: {stage.stage}/{step.name}"
                        options.append(prompting.Option([item], label, label))
        targets = prompting.choose_one("autostart selection", options, destructive=True)
    if not targets:
        info(f"no failed autostart steps in '{short_name(cfg, container)}'")
        return
    block = prepare(cfg, incus, container, targets)
    if wait:
        run_foreground(cfg, incus, container, block)
    else:
        import os

        key = "user.jailbee.autostart_in_progress"
        own_claim = f"rerun:{os.getpid()}"
        incus.config_set(container, key, own_claim)
        try:
            spawn(effective, block)
        except BaseException:
            if incus.config_get(container, key) == own_claim:
                incus.config_unset(container, key)
            raise
        info(
            "Re-running in the background — follow with "
            f"`jailbee autostart status {short_name(cfg, container)}`"
        )


@contextmanager
def rerun_locked(container: str) -> Iterator[None]:
    """Serialize reruns, and let lifecycle commands probe the same lock."""
    import fcntl

    from jailbee.db import state_dir

    lock_dir = state_dir() / "autostart-rerun-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / (container + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"An autostart re-run for '{container}' is already in progress"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def refuse_while_rerunning(container: str) -> None:
    """Probe without retaining the lock during a lifecycle command's own run."""
    with rerun_locked(container):
        pass


def command(
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    stages: Sequence[str],
    steps: Sequence[str],
    failed: bool,
    trigger: str | None,
    listing: bool,
    json_output: bool,
    wait: bool,
    spawn: Callable[[Config, Autostart], None],
) -> None:
    """Serialize a rerun through the foreground lifetime or background handoff."""

    def invoke() -> None:
        _command(
            cfg,
            incus,
            container,
            stages=stages,
            steps=steps,
            failed=failed,
            trigger=trigger,
            listing=listing,
            json_output=json_output,
            wait=wait,
            spawn=spawn,
        )

    if listing:
        invoke()
        return
    with rerun_locked(container):
        invoke()
