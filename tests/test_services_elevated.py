"""Refresh plan validation and elevation authorization."""

import copy
import json

import pytest

from jailbee.config.models_services import Service
from jailbee.orchestration.grant import ChildRights, Grant, Grantable, RepoCap
from jailbee.services import graph
from jailbee.services.elevated import Plan, RefreshError, host_policy, orchestrator_policy
from tests.conftest import make_config


def payload():
    services = {
        "db": Service(run="db", prepare=[{"run": "init", "network": "loose"}]),
        "api": Service(
            run="api",
            prepare=[
                {"run": "setup", "mounts": ["creds"], "env": {"X": "a b"}, "working_dir": "a b"}
            ],
        ),
        "web": Service(run="web"),
    }
    return graph.plan_payload(services, [], "refresh", step_timeout=42)


def parse(p, named=()):
    p = copy.deepcopy(p)
    if named:
        for service in p["services"]:
            service["will_prepare"] = service["name"] in named and bool(service["prepare"])
    return Plan.from_payload(json.dumps(p), named)


def test_from_payload_roundtrip():
    plan = parse(payload())
    assert plan.named == plan.order
    assert Plan.from_payload(json.dumps(plan.to_json()), plan.named) == plan
    assert plan.services[1].prepare[0]["env"] == {"X": "a b"}
    assert plan.services[1].prepare[0]["working_dir"] == "a b"


def test_elevated_only_when_will_prepare():
    p = payload()
    p["services"][0]["will_prepare"] = False
    plan = parse(p, ["api"])
    assert not plan.services[0].elevated
    assert plan.declared() == ChildRights(mounts=("creds",))


def test_declared_union():
    assert parse(payload()).declared() == ChildRights(loose=True, mounts=("creds",))


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        json.dumps({"version": 2}),
        json.dumps({"version": 1, "op": "refresh", "order": ["../x"], "services": []}),
        '"' + "x" * (300 * 1024) + '"',
        b"\xff",
    ],
)
def test_from_payload_rejects_garbage(raw):
    with pytest.raises(RefreshError):
        Plan.from_payload(raw, [])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(version=True),
        lambda p: p.update(op="restart"),
        lambda p: p.update(order=["db", "db", "web"]),
        lambda p: p["services"][0].update(will_prepare="false"),
        lambda p: p["services"][0]["prepare"][0].update(run=42),
        lambda p: p["services"][0]["prepare"][0].update(extra="x"),
        lambda p: p["services"][0]["prepare"][0].update(env={"X": 2}),
        lambda p: p["services"][0]["elevation"].update(network="strict"),
        lambda p: p["services"][0]["elevation"].update(mounts=[42]),
        lambda p: p["services"][0].update(depends_on=["../x"]),
        lambda p: p["services"][0].update(ready_timeout=False),
        lambda p: p["services"].reverse(),
    ],
)
def test_rejects_malformed_payload(mutate):
    p = copy.deepcopy(payload())
    mutate(p)
    with pytest.raises(RefreshError):
        parse(p)


def dependency_payload(names=()):
    services = {
        "db": Service(run="db", prepare=[{"run": "db-init"}]),
        "api": Service(run="api", prepare=[{"run": "api-init"}], depends_on={"db": {}}),
    }
    return graph.plan_payload(services, names, "refresh", step_timeout=42)


def test_rejects_reversed_dependency_order():
    p = dependency_payload()
    p["order"].reverse()
    p["services"].reverse()
    with pytest.raises(RefreshError):
        parse(p)


@pytest.mark.parametrize("dependency", ["db", "api"])
def test_rejects_cycles_and_self_dependencies(dependency):
    p = dependency_payload()
    p["services"][0]["depends_on"] = [dependency]
    with pytest.raises(RefreshError):
        parse(p)


def test_accepts_omitted_upstream_dependency():
    p = dependency_payload(["api"])
    assert parse(p, ["api"]).order == ("api",)


def test_near_limit_compact_payload_roundtrip():
    p = dependency_payload()
    p["services"][0]["prepare"][0]["run"] = "x" * (256 * 1024 - 2000) + "ä\U0001f600"
    raw = json.dumps(p, separators=(",", ":"), ensure_ascii=False)
    assert len(raw.encode()) < 256 * 1024
    plan = Plan.from_payload(raw, [])
    assert Plan.from_payload(json.dumps(plan.to_json()), plan.named) == plan


def test_rejects_compact_input_whose_roundtrip_exceeds_limit():
    p = dependency_payload()
    p["services"][0]["prepare"][0]["run"] = "ä" * 100000
    raw = json.dumps(p, separators=(",", ":"), ensure_ascii=False)
    assert len(raw.encode()) < 256 * 1024
    with pytest.raises(RefreshError):
        Plan.from_payload(raw, [])


def test_rejects_unknown_named():
    with pytest.raises(RefreshError):
        parse(payload(), ["missing"])


def test_orchestrator_policy_loose_not_grantable(tmp_path):
    assert "loose network mode is not grantable" in orchestrator_policy(
        make_config(tmp_path), Grant(), parse(payload())
    )


def test_orchestrator_policy_mount_outside_cap(tmp_path):
    cfg = make_config(tmp_path)
    grant = Grant(grantable=Grantable(mounts=("creds",)))
    assert "mount 'creds' is not an optional mount of this repo" in orchestrator_policy(
        cfg, grant, parse(payload(), ["api"])
    )


def test_orchestrator_policy_repo_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("jailbee.services.elevated.repo_cap", lambda cfg: RepoCap(mounts=()))
    grant = Grant(grantable=Grantable(mounts=("creds",)))
    assert "mount 'creds' is not grantable" in orchestrator_policy(
        make_config(tmp_path), grant, parse(payload(), ["api"])
    )


def test_host_policy_unknown_mount(tmp_path):
    cfg = make_config(tmp_path)
    assert host_policy(cfg, parse(payload())) == [
        "service 'api' prepare needs optional mount 'creds', which this host does not define"
    ]
    assert host_policy(cfg, parse(payload(), ["db"])) == []
