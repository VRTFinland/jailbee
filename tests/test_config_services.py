"""Service definitions are validated when configuration is loaded."""

from pathlib import Path

import pytest

from jailbee.config import OFFLINE_REMOVED_MSG, ConfigError
from jailbee.config.loader import load_config_from_layers


def load(text, tmp_path):
    from jailbee.config.common import _parse_yaml_text

    path = tmp_path / "repo" / ".jailbee" / "config.yaml"
    return load_config_from_layers(
        {}, _parse_yaml_text(text, str(path)), path, origin=str(path), local_raw={}
    )


@pytest.mark.parametrize(
    "entries, token",
    [
        ([{"stage": "services", "services": ["missing"]}], "unknown services"),
        ([{"stage": "services"}, {"stage": "services"}], "bare"),
        ([{"stage": "services", "services": ["web", "web"]}], "more than once"),
        (
            [
                {"stage": "services", "services": ["web"]},
                {"stage": "services", "services": ["web"]},
            ],
            "more than once",
        ),
        (
            [{"stage": "services", "services": ["web"], "steps": [{"name": "up", "run": "x"}]}],
            "steps",
        ),
        ([{"stage": "services", "network": "loose"}], "network"),
        ([{"stage": "services", "network": "strict"}], "network"),
        ([{"stage": "services", "mounts": ["aws"]}], "mounts"),
        ([{"stage": "setup", "services": ["web"]}], "only.*services"),
    ],
)
def test_invalid_services_slots(tmp_path, entries, token):
    import yaml

    with pytest.raises(ConfigError, match=token):
        load(
            yaml.safe_dump({"services": {"web": {"run": "x"}}, "autostart": {"on_start": entries}}),
            tmp_path,
        )


@pytest.mark.parametrize("trigger", ["on_create", "on_start"])
def test_ordinary_services_stage_names_are_still_unique(tmp_path, trigger):
    with pytest.raises(ConfigError, match=r"duplicate.*stage name"):
        load(
            f"autostart: {{{trigger}: ["
            "{stage: services, steps: [{name: a, run: x}]}, "
            "{stage: services, steps: [{name: b, run: x}]}]}",
            tmp_path,
        )


def test_ordinary_services_stage_can_coexist_with_slots(tmp_path):
    cfg = load(
        "autostart: {on_start: ["
        "{stage: services, steps: [{name: up, run: x}]}, {stage: services}]}",
        tmp_path,
    )
    assert len(cfg.autostart.on_start) == 2


def test_disjoint_services_slots_load_with_dependency_placed_later(tmp_path):
    cfg = load(
        "services: {web: {run: x, depends_on: {db: {}}}, db: {run: x}}\n"
        "autostart: {on_start: [{stage: services, services: [web]}, "
        "{stage: services, services: [db]}, {stage: services}]}",
        tmp_path,
    )
    assert len(cfg.autostart.on_start) == 3


def test_services_slot_is_only_on_start(tmp_path):
    with pytest.raises(ConfigError, match="on_start"):
        load("autostart: {on_create: [{stage: services}]}", tmp_path)


def test_profile_can_name_implicit_services_slot(tmp_path):
    load("autostart: {profiles: {p: {stages: [services]}}, default_profile: p}", tmp_path)


def test_minimal_service(tmp_path):
    service = load('services: {web: {run: "npm run dev"}}', tmp_path).services["web"]
    assert (service.ready_timeout, service.stop_timeout, service.prepare) == (120, 10, [])
    assert (service.health, service.health_timeout) == (None, 5)


def test_health_timeout_must_be_positive(tmp_path):
    with pytest.raises(ConfigError, match="health_timeout"):
        load("services: {web: {run: x, health_timeout: 0}}", tmp_path)


@pytest.mark.parametrize(
    "text, token",
    [
        ("services: {backend: {run: x, depends_on: {db: {}}}}", "db"),
        ("services: {a: {run: x, depends_on: {a: {}}}}", "a"),
        ("services: {a: {run: x, depends_on: {b: {}}}, b: {run: x, depends_on: {a: {}}}}", " -> "),
        ("services: {Web: {run: x}}", "Web"),
        ('services: {"svc:x": {run: x}}', "svc:x"),
        ('services: {web: {run: ""}}', "run"),
        ("services: {web: {run: x, ready_timeout: 0}}", "ready_timeout"),
        ("services: {web: {run: x, stop_timeout: 0}}", "stop_timeout"),
        ("services: {web: {run: x, unknown: true}}", "unknown"),
    ],
)
def test_invalid_service_rejected(tmp_path, text, token):
    with pytest.raises(ConfigError, match=token):
        load(text, tmp_path)


def test_cycle_names_both_services(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(
            "services: {a: {run: x, depends_on: {b: {}}}, b: {run: x, depends_on: {a: {}}}}",
            tmp_path,
        )
    assert "a -> b -> a" in str(exc.value) or "b -> a -> b" in str(exc.value)


def test_prepare_step_elevated(tmp_path):
    cfg = load(
        "services: {web: {run: x, prepare: [{run: x, network: loose}, "
        "{run: x, mounts: [creds]}, {run: x}]}}",
        tmp_path,
    )
    assert [step.elevated for step in cfg.services["web"].prepare] == [True, True, False]


def test_prepare_offline_network_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load("services: {web: {run: x, prepare: [{run: x, network: offline}]}}", tmp_path)
    assert OFFLINE_REMOVED_MSG in str(exc.value)


def test_prepare_mount_must_exist_on_host_load(tmp_path):
    cfg = load("services: {web: {run: x, prepare: [{run: x, mounts: [nope]}]}}", tmp_path)
    assert any(
        "services.web.prepare" in issue and "nope" in issue for issue in cfg.validate_runtime()
    )


def test_full_fixture_has_services(tmp_path):
    text = (Path(__file__).parent / "fixtures" / "full_config.yaml").read_text()
    assert len(load(text, tmp_path).services) >= 2


def test_profile_services_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="autostart profile 'p' names unknown services: missing"):
        load(
            "autostart: {profiles: {p: {stages: [agents], services: [missing]}}, "
            "default_profile: p}",
            tmp_path,
        )


def test_profile_services_none_means_all(tmp_path):
    cfg = load("autostart: {profiles: {p: {stages: [agents]}}, default_profile: p}", tmp_path)
    profile = cfg.autostart.profiles["p"]
    assert profile != "all"
    assert profile.services is None


def test_profile_services_accepts_existing_and_empty(tmp_path):
    cfg = load(
        "services: {web: {run: x}}\nautostart: {profiles: "
        "{p: {stages: [agents], services: [web]}, q: {stages: [agents], services: []}}, "
        "default_profile: p}",
        tmp_path,
    )
    assert cfg.autostart.profiles["p"].services == ["web"]
    assert cfg.autostart.profiles["q"].services == []


def test_invalid_filter_regex_rejected_at_load(tmp_path):
    assert load("services: {web: {run: x, logs: {filters: {errors: error}}}}", tmp_path).services["web"].logs.filters == {"errors": "error"}
    with pytest.raises(ConfigError) as exc:
        load("services: {web: {run: x, logs: {filters: {errors: '['}}}}", tmp_path)
    assert "web" in str(exc.value) and "errors" in str(exc.value)
    with pytest.raises(ConfigError):
        load("services: {web: {run: x, logs: {filters: {BAD: x}}}}", tmp_path)


def test_history_limit_minimum(tmp_path):
    assert load("services: {web: {run: x}}", tmp_path).services["web"].logs.history_limit == 50000
    with pytest.raises(ConfigError):
        load("services: {web: {run: x, logs: {history_limit: 999}}}", tmp_path)
    with pytest.raises(ConfigError):
        load("services: {web: {run: x, status_timeout: 0}}", tmp_path)
