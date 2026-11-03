"""Execute container-defined service preparation and startup during autostart."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from jailbee import autostart, container_app
from jailbee.autostart_profiles import (
    PREPARED_KEY,
    SERVICES_KEY,
    decode_names,
    encode_names,
    resolve_profile,
)
from jailbee.config import CONTAINER_USERNAME, AutostartChain, AutostartStage, AutostartStep
from jailbee.tui import warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


ENVELOPE_KEY = "user.jailbee.services_approval"


def approval_envelope(cfg: Config, names: list[str] | None = None) -> str:
    from jailbee.services.graph import elevation, with_dependencies

    selected = with_dependencies(
        cfg.services,
        cfg.services if names is None else [name for name in names if name in cfg.services],
    )
    grants = {}
    for name in sorted(selected):
        access = elevation(cfg.services[name])
        grants[name] = {"network": access.network, "mounts": list(access.mounts)}
    return json.dumps({"version": 1, "services": grants}, separators=(",", ":"))


def _approved(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("services"), dict)
    ):
        raise ValueError("invalid service approval envelope")
    for name, grant in data["services"].items():
        if (
            not isinstance(name, str)
            or not isinstance(grant, dict)
            or grant.get("network") not in (None, "loose")
            or not isinstance(grant.get("mounts"), list)
            or not all(isinstance(mount, str) for mount in grant["mounts"])
        ):
            raise ValueError("invalid service approval envelope")
    return dict(data["services"])


def prepare_stage(
    container: str,
    name: str,
    prepare: list[dict[str, Any]],
    *,
    network: str | None,
    mounts: list[str],
) -> AutostartStage:
    steps = []
    for step in prepare:
        allowed = {"name", "run", "env", "working_dir", "timeout", "continue_on_error"}
        unexpected = set(step) - allowed
        if unexpected:
            raise autostart.AutostartStepError(
                container=container,
                step_name=f"prepare-{name}",
                reason=f"unexpected service prepare fields: {', '.join(sorted(unexpected))}",
            )
        steps.append(
            AutostartStep(
                name=step["name"],
                run=step["run"],
                env=step["env"],
                working_dir=step["working_dir"],
                timeout=step["timeout"],
                continue_on_error=step["continue_on_error"],
            )
        )
    return AutostartStage.model_construct(
        stage=f"prepare-{name}",
        network=network,
        mounts=list(mounts),
        detach=False,
        chains=[AutostartChain(name="main", steps=steps)],
        steps=[],
    )


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


def services_stage_wanted(
    cfg: Config, incus: Incus, container: str, *, services_present: bool | None = None
) -> bool:
    names = decode_names(incus.config_get(container, SERVICES_KEY))
    if names is not None:
        return bool(names)
    return bool(cfg.services) if services_present is None else services_present


def run_services_stage(
    cfg: Config,
    incus: Incus,
    container: str,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None,
    on_progress: Callable[[str, str, str], None] | None,
    cas_restore: bool,
    live_switch: bool = False,
    stage_services: list[str] | None = None,
    excluded_services: list[str] | tuple[str, ...] = (),
) -> None:
    names = selected_services(cfg, incus, container)
    if names == []:
        return
    try:
        result = container_app.run_in_container(
            cfg,
            incus,
            container,
            ["service", "--repo", repo_dir, "plan", "--op", "start", "--json", *(names or [])],
            uid=cfg.container_user.uid,
            cwd=repo_dir,
            env={"HOME": f"/home/{CONTAINER_USERNAME}"},
            init_groups=True,
        )
    except container_app.ContainerJailbeeError as exc:
        warn(f"services not started: {exc}")
        return
    if result.returncode:
        reason = result.stderr.decode(errors="replace").strip().splitlines()
        raise autostart.AutostartStepError(
            container=container,
            step_name="services",
            reason=reason[0] if reason else "container service plan failed",
            exit_code=result.returncode,
        )
    payload = json.loads(result.stdout)
    order = payload["order"]
    if not order:
        return
    services = {item["name"]: item for item in payload["services"]}
    if stage_services is not None or excluded_services:
        targets = (
            set(order).intersection(stage_services)
            if stage_services is not None else set(order).difference(excluded_services)
        )
        # Partition the container's selected plan, not the host definitions:
        # a cloned branch may define a different service graph. Dependencies
        # follow their dependant even if another slot places them later.
        pending = list(targets)
        while pending:
            for dependency in services[pending.pop()].get("depends_on", []):
                if dependency in services and dependency not in targets:
                    targets.add(dependency)
                    pending.append(dependency)
        order = [name for name in order if name in targets]
        if not order:
            return
    prepared = decode_names(incus.config_get_checked(container, PREPARED_KEY)) or []
    if live_switch:
        deferred: set[str] = set()
        for name in order:
            service = services[name]
            access = service["elevation"]
            if (
                service["prepare"]
                and name not in prepared
                and (access["network"] == "loose" or access["mounts"])
            ) or deferred.intersection(service.get("depends_on", [])):
                deferred.add(name)
        if deferred and any("depends_on" not in services[name] for name in order):
            deferred.update(order)
        if deferred:
            names_deferred = ", ".join(name for name in order if name in deferred)
            warn(f"services deferred until next restart: {names_deferred}")
            order = [name for name in order if name not in deferred]
        if not order:
            return
    for name in order:
        service = services[name]
        if not service["prepare"] or name in prepared:
            continue
        elevation = service["elevation"]
        from jailbee.orchestration import labels
        from jailbee.orchestration.grant import ChildRights, GrantError

        spawned_by = incus.config_get_checked(container, labels.SPAWNED_BY)
        if not spawned_by:
            raw_envelope = incus.config_get_checked(container, ENVELOPE_KEY)
            try:
                grants = _approved(
                    raw_envelope if raw_envelope is not None else approval_envelope(cfg)
                )
            except (ValueError, TypeError) as exc:
                raise autostart.AutostartStepError(
                    container=container,
                    step_name=f"prepare-{name}",
                    reason=f"invalid approved service elevation envelope: {exc}",
                ) from exc
            grant = grants.get(name, {"network": None, "mounts": []})
            denied = sorted(set(elevation["mounts"]) - set(grant["mounts"]))
            if elevation["network"] == "loose" and grant["network"] != "loose":
                denied.append("network loose")
            if denied:
                raise autostart.AutostartStepError(
                    container=container,
                    step_name=f"prepare-{name}",
                    reason=(
                        f"service '{name}' exceeds approved elevation: {', '.join(denied)}; "
                        "re-assess the configuration, then request a host or orchestrator "
                        "service refresh"
                    ),
                )
        if spawned_by:
            raw_rights = incus.config_get_checked(container, labels.RIGHTS)
            try:
                rights = ChildRights.from_label(raw_rights) if raw_rights else ChildRights()
            except GrantError as exc:
                raise autostart.AutostartStepError(
                    container=container, step_name=f"prepare-{name}", reason=str(exc)
                ) from exc
            denied = sorted(set(elevation["mounts"]) - set(rights.mounts))
            if elevation["network"] == "loose" and not rights.loose:
                denied.append("network loose")
            if denied:
                raise autostart.AutostartStepError(
                    container=container,
                    step_name=f"prepare-{name}",
                    reason=f"service '{name}' prepare exceeds child rights: {', '.join(denied)}",
                )
        for mount in elevation["mounts"]:
            if mount not in cfg.optional_mounts:
                raise autostart.AutostartStepError(
                    container=container,
                    step_name=f"prepare-{name}",
                    reason=(
                        f"service '{name}' prepare needs optional mount '{mount}', "
                        "which this host does not define"
                    ),
                )
    for name in order:
        service = services[name]
        if not service["prepare"] or name in prepared:
            continue
        elevation = service["elevation"]
        stage = prepare_stage(
            container,
            name,
            service["prepare"],
            network=elevation["network"],
            mounts=elevation["mounts"],
        )
        try:
            autostart.run_stage(
                cfg,
                incus,
                container,
                stage,
                repo_dir,
                mirror_endpoint=mirror_endpoint,
                on_progress=on_progress,
                cas_restore=cas_restore,
            )
        except autostart.AutostartStepError as exc:
            raise autostart.AutostartStepError(
                container=container,
                step_name=f"prepare-{name}",
                reason=exc.reason,
                exit_code=exc.exit_code,
                original=exc.original,
            ) from exc
        except autostart.AutostartCancelledError:
            raise
        except Exception as exc:
            raise autostart.AutostartStepError(
                container=container,
                step_name=f"prepare-{name}",
                reason=f"prepare infrastructure failed: {exc}",
                original=exc,
            ) from exc
        prepared.append(name)
        incus.config_set(container, PREPARED_KEY, encode_names(prepared))
    command = shlex.join(
        [
            "jailbee",
            "--expect-version",
            container_app.expected_version(),
            "service",
            "--repo",
            repo_dir,
            "start",
            *order,
        ]
    )
    step = AutostartStep(
        name="services-start",
        run=command,
        timeout=sum(s["ready_timeout"] for s in payload["services"]) + 30,
    )
    stage = AutostartStage.model_construct(
        stage="services",
        network=None,
        mounts=[],
        detach=False,
        chains=[AutostartChain(name="main", steps=[step])],
        steps=[],
    )
    autostart.run_stage(
        cfg,
        incus,
        container,
        stage,
        repo_dir,
        mirror_endpoint=mirror_endpoint,
        on_progress=on_progress,
        cas_restore=cas_restore,
    )
