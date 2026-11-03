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


def test_minimal_service(tmp_path):
    service = load('services: {web: {run: "npm run dev"}}', tmp_path).services["web"]
    assert (service.ready_timeout, service.stop_timeout, service.prepare) == (120, 10, [])


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
