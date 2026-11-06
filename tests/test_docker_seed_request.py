"""Docker seed request planning, independent of CLI parsing."""

import pytest

from jailbee import docker_seed
from jailbee.config.models_golden import DockerSeed


def test_plan_request_resolves_source_and_preflights(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    resolve = mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive", return_value="repo-src"
    )
    preflight = mocker.patch("jailbee.docker_seed.preflight")

    plan = docker_seed.plan_request(cfg, incus, docker_from="src", keep_seed=True)

    assert plan.request.source_container == "repo-src"
    assert plan.requested
    resolve.assert_called_once_with(cfg, incus, "src")
    preflight.assert_called_once_with(cfg, incus, plan.request)
    assert any("--keep-seed has no effect" in warning for warning in plan.warnings)


def test_disabled_request_warns_about_ineffective_keep(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    preflight = mocker.patch("jailbee.docker_seed.preflight")
    plan = docker_seed.plan_request(cfg, mocker.Mock(), keep_seed=True)
    assert not plan.requested
    assert any("--keep-seed has no effect" in warning for warning in plan.warnings)
    preflight.assert_not_called()


def test_config_preflight_skip_is_returned_not_recorded(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed()})
    mocker.patch(
        "jailbee.docker_seed.preflight", side_effect=docker_seed.SeedPreflightError("shared")
    )
    record = mocker.patch("jailbee.docker_seed.record_outcome")
    plan = docker_seed.plan_request(cfg, mocker.Mock())
    assert not plan.requested and plan.skipped.outcome == "skipped"
    record.assert_not_called()


def test_explicit_request_requires_golden(mocker, make_cfg, tmp_path):
    mocker.patch("jailbee.golden.golden_exists", return_value=False)
    with pytest.raises(docker_seed.SeedPreflightError, match="base build"):
        docker_seed.plan_request(make_cfg(tmp_path), mocker.Mock(), standalone=True)
