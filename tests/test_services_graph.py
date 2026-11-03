import json

import pytest

from jailbee.config.models_services import Service, ServiceDependency, ServicePrepareStep
from jailbee.services.graph import (
    Elevation,
    UnknownServiceError,
    elevation,
    plan_payload,
    restart_set,
    topo_order,
    with_dependencies,
)


def service(*dependencies: str, restart: bool = False, **kwargs: object) -> Service:
    return Service.model_validate(
        {
            "run": "serve",
            "depends_on": {name: ServiceDependency(restart=restart) for name in dependencies},
            **kwargs,
        }
    )


def test_topo_order_dependencies_first() -> None:
    services = {"web": service("backend"), "backend": service("db"), "db": service()}
    assert topo_order(services, ["web", "db", "backend"]) == ["db", "backend", "web"]


def test_topo_tie_break_config_order() -> None:
    services = {"b": service(), "a": service()}
    assert topo_order(services, ["a", "b"]) == ["b", "a"]


def test_topo_prefers_first_available_in_config_order() -> None:
    services = {"web": service("db"), "other": service(), "db": service()}
    assert topo_order(services, services) == ["other", "db", "web"]


def test_newly_ready_earlier_service_precedes_ready_later_service() -> None:
    services = {"a": service("b"), "b": service(), "c": service()}
    assert topo_order(services, services) == ["b", "a", "c"]


def test_with_dependencies_transitive() -> None:
    services = {"web": service("backend"), "backend": service("db"), "db": service()}
    assert with_dependencies(services, ["web"]) == {"web", "backend", "db"}


def test_restart_cascade_only_restart_edges() -> None:
    services = {"a": service(), "b": service("a", restart=True), "c": service("b")}
    assert restart_set(services, ["a"]) == {"a", "b"}


def test_restart_cascade_transitive_restart_edges() -> None:
    services = {"c": service("b", restart=True), "b": service("a", restart=True), "a": service()}
    assert restart_set(services, ["a"]) == {"a", "b", "c"}


def test_elevation_union() -> None:
    svc = service(
        prepare=[
            ServicePrepareStep(run="a", network="loose"),
            ServicePrepareStep(run="b", mounts=["x"]),
            ServicePrepareStep(run="c", mounts=["x", "y"]),
        ]
    )
    assert elevation(svc) == Elevation("loose", ("x", "y"))
    assert elevation(svc).any
    assert not elevation(service()).any


def test_plan_payload_refresh_prepares_only_named() -> None:
    services = {
        "backend": service("db", restart=True, prepare=[{"run": "backend prep"}]),
        "db": service(
            env={"A": "service", "B": "base"},
            working_dir="app",
            prepare=[{"run": "prepare", "env": {"A": "step"}, "network": "loose", "mounts": ["x"]}],
        ),
    }
    payload = plan_payload(services, ["db"], "refresh", step_timeout=45)
    assert payload == {
        "version": 1,
        "op": "refresh",
        "order": ["db", "backend"],
        "services": [
            {
                "name": "db",
                "will_prepare": True,
                "prepare": [
                    {
                        "name": "db-prepare-1",
                        "run": "prepare",
                        "env": {"A": "step", "B": "base"},
                        "working_dir": "app",
                        "timeout": 45,
                        "continue_on_error": False,
                    }
                ],
                "elevation": {"network": "loose", "mounts": ["x"]},
                "ready_timeout": 120,
                "stop_timeout": 10,
            },
            {
                "name": "backend",
                "will_prepare": False,
                "prepare": [
                    {
                        "name": "backend-prepare-1",
                        "run": "backend prep",
                        "env": {},
                        "working_dir": "",
                        "timeout": 45,
                        "continue_on_error": False,
                    }
                ],
                "elevation": {"network": None, "mounts": []},
                "ready_timeout": 120,
                "stop_timeout": 10,
            },
        ],
    }


def test_plan_payload_unknown_name() -> None:
    with pytest.raises(UnknownServiceError, match=r"db.*web"):
        plan_payload({"db": service(), "web": service()}, ["missing"], "start", step_timeout=30)


def test_plan_payload_is_json_serializable() -> None:
    payload = plan_payload({"a": service()}, [], "restart", step_timeout=30)
    assert json.loads(json.dumps(payload)) == payload


def test_plan_start_includes_dependencies_and_never_prepares() -> None:
    services = {
        "web": service(
            "db",
            prepare=[
                {"run": "prep", "timeout": 12, "working_dir": "step", "continue_on_error": True}
            ],
        ),
        "db": service(),
    }
    payload = plan_payload(services, ["web"], "start", step_timeout=30)
    assert payload["order"] == ["db", "web"]
    entries = payload["services"]
    assert isinstance(entries, list)
    assert entries[1]["will_prepare"] is False
    assert entries[1]["prepare"][0]["timeout"] == 12
    assert entries[1]["prepare"][0]["working_dir"] == "step"
    assert entries[1]["prepare"][0]["continue_on_error"] is True


def test_refresh_without_names_prepares_all() -> None:
    payload = plan_payload(
        {"a": service(prepare=[{"run": "prep"}]), "b": service()}, [], "refresh", step_timeout=30
    )
    entries = payload["services"]
    assert isinstance(entries, list)
    assert [entry["will_prepare"] for entry in entries] == [True, False]
