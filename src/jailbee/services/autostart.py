"""Execute container-defined service preparation and startup during autostart."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from jailbee import autostart, container_app
from jailbee.autostart_profiles import PREPARED_KEY, SERVICES_KEY, decode_names, encode_names, resolve_profile
from jailbee.config import CONTAINER_USERNAME, AutostartChain, AutostartStage, AutostartStep
from jailbee.tui import warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def selected_services(cfg: Config, incus: Incus, container: str) -> list[str] | None:
    names = decode_names(incus.config_get(container, SERVICES_KEY))
    if names is not None:
        return names
    profile = resolve_profile(cfg, incus, container)
    if profile is not None:
        selected = cfg.autostart.profiles[profile]
        if selected != "all":
            return selected.services
    return None


def services_stage_wanted(cfg: Config, incus: Incus, container: str) -> bool:
    names = decode_names(incus.config_get(container, SERVICES_KEY))
    return bool(cfg.services) if names is None else bool(names)


def run_services_stage(
    cfg: Config, incus: Incus, container: str, repo_dir: str, *,
    mirror_endpoint: tuple[str, int] | None,
    on_progress: Callable[[str, str, str], None] | None,
    cas_restore: bool,
) -> None:
    names = selected_services(cfg, incus, container)
    if names == []:
        return
    try:
        result = container_app.run_in_container(
            cfg, incus, container,
            ["service", "--repo", repo_dir, "plan", "--op", "start", "--json", *(names or [])],
            uid=cfg.container_user.uid, cwd=repo_dir,
            env={"HOME": f"/home/{CONTAINER_USERNAME}"}, init_groups=True,
        )
    except container_app.ContainerJailbeeError as exc:
        warn(f"services not started: {exc}")
        return
    if result.returncode:
        reason = result.stderr.decode(errors="replace").strip().splitlines()
        raise autostart.AutostartStepError(container=container, step_name="services", reason=reason[0] if reason else "container service plan failed", exit_code=result.returncode)
    payload = json.loads(result.stdout)
    order = payload["order"]
    if not order:
        return
    services = {item["name"]: item for item in payload["services"]}
    prepared = decode_names(incus.config_get(container, PREPARED_KEY)) or []
    for name in order:
        service = services[name]
        if not service["prepare"] or name in prepared:
            continue
        elevation = service["elevation"]
        for mount in elevation["mounts"]:
            if mount not in cfg.optional_mounts:
                raise autostart.AutostartStepError(container=container, step_name=f"prepare-{name}", reason=f"service '{name}' prepare needs optional mount '{mount}', which this host does not define")
        steps = [AutostartStep(**step) for step in service["prepare"]]
        stage = AutostartStage.model_construct(stage=f"prepare-{name}", network=elevation["network"], mounts=list(elevation["mounts"]), detach=False, chains=[AutostartChain(name="main", steps=steps)], steps=[])
        try:
            autostart.run_stage(cfg, incus, container, stage, repo_dir, mirror_endpoint=mirror_endpoint, on_progress=on_progress, cas_restore=cas_restore)
        except autostart.AutostartStepError as exc:
            raise autostart.AutostartStepError(container=container, step_name=f"prepare-{name}", reason=exc.reason, exit_code=exc.exit_code, original=exc.original) from exc
        prepared.append(name)
        incus.config_set(container, PREPARED_KEY, encode_names(prepared))
    command = shlex.join(["jailbee", "--expect-version", container_app.expected_version(), "service", "--repo", repo_dir, "start", *order])
    step = AutostartStep(name="services-start", run=command, timeout=sum(s["ready_timeout"] for s in payload["services"]) + 30)
    stage = AutostartStage.model_construct(stage="services", network=None, mounts=[], detach=False, chains=[AutostartChain(name="main", steps=[step])], steps=[])
    autostart.run_stage(cfg, incus, container, stage, repo_dir, mirror_endpoint=mirror_endpoint, on_progress=on_progress, cas_restore=cas_restore)
