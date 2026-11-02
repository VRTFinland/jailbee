"""Prepare and restart services without granting elevated access."""

import socket
from collections.abc import Sequence

from jailbee.config import Config
from jailbee.services import graph
from jailbee.services.runtime import RunReport, ServiceRuntime

EXIT_NEEDS_ELEVATION = 3


def elevation_refusal(cfg: Config, names: Sequence[str], hostname: str) -> str | None:
    order = graph.topo_order(cfg.services, names)
    elevated = [name for name in order if graph.elevation(cfg.services[name]).any]
    if not elevated:
        return None
    details = []
    for name in elevated:
        access = graph.elevation(cfg.services[name])
        parts = (["network loose"] if access.network else []) + (["mounts " + ", ".join(access.mounts)] if access.mounts else [])
        details.append(f"{name}: {'; '.join(parts)}")
    short = hostname.removeprefix(cfg.container_prefix + "-")
    selected = " ".join(names)
    return (f"refresh of {', '.join(elevated)} needs elevated access ({'; '.join(details)}) — "
            f"the agent cannot grant that to itself. Ask the orchestrator: `jailbee-inbox send refresh --child {short} --services {selected} --wait`, "
            f"or on the host: `jb service refresh {short} {selected}`")


def run_refresh(rt: ServiceRuntime, cfg: Config, names: Sequence[str]) -> RunReport:
    order = graph.topo_order(cfg.services, names)
    refusal = elevation_refusal(cfg, names, socket.gethostname())
    if refusal:
        raise ValueError(refusal)
    done: list[str] = []
    with rt.locked():
        for name in order:
            service = cfg.services[name]
            for step in service.prepare:
                result = rt.proc.run_shell(step.run, cwd=str(rt.repo_dir / (step.working_dir or service.working_dir)), env={**service.env, **step.env}, timeout=step.timeout or cfg.autostart.step_timeout, capture=False)
                if result and not step.continue_on_error:
                    return RunReport(False, tuple(done), name, "prepare failed")
            done.append(name)
        return rt._restart_unlocked(names)
