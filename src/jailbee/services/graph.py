"""Pure dependency and preparation plans for repository services."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from jailbee.config.models_services import Service


class UnknownServiceError(ValueError):
    """A requested service is not defined."""


def _selected(services: Mapping[str, Service], names: Iterable[str]) -> set[str]:
    selected = set(names)
    unknown = selected - services.keys()
    if unknown:
        raise UnknownServiceError(
            f"Unknown services: {', '.join(sorted(unknown))}; "
            f"defined services: {', '.join(services) or '(none)'}"
        )
    return selected


def topo_order(services: Mapping[str, Service], names: Iterable[str]) -> list[str]:
    remaining = _selected(services, names)
    result: list[str] = []
    while remaining:
        for name, service in services.items():
            if name in remaining and not (service.depends_on.keys() & remaining):
                remaining.remove(name)
                result.append(name)
                break
        else:
            raise ValueError("Service dependency cycle")
    return result


def with_dependencies(services: Mapping[str, Service], names: Iterable[str]) -> set[str]:
    selected = _selected(services, names)
    pending = list(selected)
    while pending:
        name = pending.pop()
        for dependency in services[name].depends_on:
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return selected


def restart_set(services: Mapping[str, Service], names: Iterable[str]) -> set[str]:
    selected = _selected(services, names)
    pending = list(selected)
    while pending:
        dependency = pending.pop()
        for name, service in services.items():
            edge = service.depends_on.get(dependency)
            if name not in selected and edge is not None and edge.restart:
                selected.add(name)
                pending.append(name)
    return selected


@dataclass(frozen=True)
class Elevation:
    network: Literal["loose"] | None
    mounts: tuple[str, ...]

    @property
    def any(self) -> bool:
        return self.network is not None or bool(self.mounts)


def elevation(service: Service) -> Elevation:
    network: Literal["loose"] | None = None
    mounts: dict[str, None] = {}
    for step in service.prepare:
        if step.network == "loose":
            network = "loose"
        mounts.update(dict.fromkeys(step.mounts))
    return Elevation(network, tuple(mounts))


def plan_payload(
    services: Mapping[str, Service],
    names: Sequence[str],
    op: Literal["start", "restart", "refresh"],
    *,
    step_timeout: int,
) -> dict[str, object]:
    targets = _selected(services, names if names else services)
    selected = with_dependencies(services, targets) if op == "start" else restart_set(services, targets)
    order = topo_order(services, selected)
    entries: list[dict[str, object]] = []
    for name in order:
        service = services[name]
        access = elevation(service)
        entries.append({
            "name": name,
            "will_prepare": op == "refresh" and name in targets and bool(service.prepare),
            "prepare": [
                {
                    "name": f"{name}-prepare-{i}",
                    "run": step.run,
                    "env": {**service.env, **step.env},
                    "working_dir": step.working_dir or service.working_dir,
                    "timeout": step.timeout or step_timeout,
                    "continue_on_error": step.continue_on_error,
                }
                for i, step in enumerate(service.prepare, 1)
            ],
            "elevation": {"network": access.network, "mounts": list(access.mounts)},
            "ready_timeout": service.ready_timeout,
            "stop_timeout": service.stop_timeout,
        })
    return {"version": 1, "op": op, "order": order, "services": entries}
