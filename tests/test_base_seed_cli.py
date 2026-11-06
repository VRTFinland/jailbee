"""Tests for `jailbee base seed` and Docker seeding on `base build`."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from jailbee.autostart import AutostartCancelledError

runner = CliRunner()


def test_base_seed_requires_existing_golden(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus", return_value=mocker.Mock(spec=Incus))
    mocker.patch("jailbee.golden.golden_exists", return_value=False)

    result = runner.invoke(app, ["base", "seed"])

    assert result.exit_code == 2
    assert "base build" in result.output
    incus.return_value.exec.assert_not_called()


def test_base_seed_requires_configuration_or_explicit_source(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.Mock(spec=Incus))
    mocker.patch("jailbee.golden.golden_exists", return_value=True)

    result = runner.invoke(app, ["base", "seed"])

    assert result.exit_code == 2
    assert "not configured" in result.output


def test_explicit_docker_source_resolves_short_name_and_runs_seed(
    mocker, make_cfg, tmp_path
) -> None:
    from jailbee.cli import app
    from jailbee.docker_seed import SeedOutcome
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus", return_value=mocker.Mock(spec=Incus))
    mocker.patch("jailbee.golden.golden_exists", return_value=True)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive", return_value="project-feature"
    )
    preflight = mocker.patch("jailbee.docker_seed.preflight")
    run_seed = mocker.patch(
        "jailbee.docker_seed.run_seed",
        return_value=SeedOutcome("seeded", "container:feature", 2, 1024),
        create=True,
    )

    result = runner.invoke(app, ["base", "seed", "--docker-from", "feature", "--keep-seed"])

    assert result.exit_code == 0, result.output
    assert run_seed.call_args.args[3].source_container == "project-feature"
    assert run_seed.call_args.args[3].keep_seed is True
    assert run_seed.call_args.args[3].explicit is True
    preflight.assert_called_once()
    assert incus.call_count == 1
    assert "2 Docker images" in result.output


def test_base_seed_preflight_failure_exits_two(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.docker_seed import SeedPreflightError
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.Mock(spec=Incus))
    mocker.patch("jailbee.golden.golden_exists", return_value=True)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive", return_value="project-feature"
    )
    mocker.patch("jailbee.docker_seed.preflight", side_effect=SeedPreflightError("unsafe"))

    result = runner.invoke(app, ["base", "seed", "--docker-from", "feature"])

    assert result.exit_code == 2
    assert "unsafe" in result.output


def test_base_seed_failed_outcome_exits_one(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.docker_seed import SeedOutcome
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.Mock(spec=Incus))
    mocker.patch("jailbee.golden.golden_exists", return_value=True)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive", return_value="project-feature"
    )
    mocker.patch("jailbee.docker_seed.preflight")
    mocker.patch(
        "jailbee.docker_seed.run_seed",
        return_value=SeedOutcome("failed", "seed-container", detail="load failed"),
        create=True,
    )

    result = runner.invoke(app, ["base", "seed", "--docker-from", "feature"])

    assert result.exit_code == 1
    assert "load failed" in result.output


def test_base_build_preflight_config_failure_warns_and_still_builds(
    mocker, make_cfg, tmp_path
) -> None:
    from jailbee.cli import app
    from jailbee.config.models_golden import DockerSeed
    from jailbee.docker_seed import SeedPreflightError

    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed()})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    build = mocker.patch("jailbee.golden.build_golden_image")
    mocker.patch("jailbee.docker_seed.preflight", side_effect=SeedPreflightError("shared base"))
    record = mocker.patch("jailbee.docker_seed.record_outcome")

    result = runner.invoke(app, ["base", "build"])

    assert result.exit_code == 0, result.output
    build.assert_called_once()
    record.assert_called_once()
    assert "shared base" in result.output


def test_failed_plain_build_does_not_overwrite_previous_seed_result(mocker, make_cfg, tmp_path):
    from jailbee.cli import app
    from jailbee.config.models_golden import DockerSeed
    from jailbee.docker_seed import SeedPreflightError
    from jailbee.incus import IncusError

    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed()})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.golden.build_golden_image", side_effect=IncusError("build failed"))
    mocker.patch("jailbee.docker_seed.preflight", side_effect=SeedPreflightError("shared base"))
    record = mocker.patch("jailbee.docker_seed.record_outcome")

    result = runner.invoke(app, ["base", "build"])

    assert result.exit_code == 1, result.output
    record.assert_not_called()


def test_base_build_seeds_when_configured(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.config.models_golden import DockerSeed
    from jailbee.docker_seed import SeedOutcome

    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed()})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.golden.build_golden_image")
    preflight = mocker.patch("jailbee.docker_seed.preflight")
    run_seed = mocker.patch(
        "jailbee.docker_seed.run_seed",
        return_value=SeedOutcome("skipped", "seed-container", detail="no images"),
        create=True,
    )

    result = runner.invoke(app, ["base", "build"])

    assert result.exit_code == 0, result.output
    preflight.assert_called_once()
    run_seed.assert_called_once()
    assert "no images" in result.output


def test_base_build_disabled_seed_config_skips_seeding(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.config.models_golden import DockerSeed

    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed(enabled=False)})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.golden.build_golden_image")
    preflight = mocker.patch("jailbee.docker_seed.preflight")

    result = runner.invoke(app, ["base", "build"])

    assert result.exit_code == 0, result.output
    preflight.assert_not_called()


def test_base_build_unconfigured_seed_config_skips_seeding(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.golden.build_golden_image")
    preflight = mocker.patch("jailbee.docker_seed.preflight")

    result = runner.invoke(app, ["base", "build"])

    assert result.exit_code == 0, result.output
    preflight.assert_not_called()


def test_base_build_explicit_preflight_failure_does_not_build(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.docker_seed import SeedPreflightError

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    build = mocker.patch("jailbee.golden.build_golden_image")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_for_interactive", return_value="project-feature"
    )
    mocker.patch("jailbee.docker_seed.preflight", side_effect=SeedPreflightError("not running"))

    result = runner.invoke(app, ["base", "build", "--docker-from", "feature"])

    assert result.exit_code == 2
    assert "not running" in result.output
    build.assert_not_called()


def test_base_build_failed_seed_outcome_does_not_fail_build(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.config.models_golden import DockerSeed
    from jailbee.docker_seed import SeedOutcome

    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed()})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.golden.build_golden_image")
    mocker.patch("jailbee.docker_seed.preflight")
    mocker.patch(
        "jailbee.docker_seed.run_seed",
        return_value=SeedOutcome("failed", "seed-container", detail="load failed"),
    )

    result = runner.invoke(app, ["base", "build"])

    assert result.exit_code == 0, result.output
    assert "load failed" in result.output


@pytest.mark.parametrize("stage", ["preflight", "global", "seed"])
@pytest.mark.parametrize("exc", [OSError("disk error"), SQLAlchemyError("database error"),
    AutostartCancelledError(container="seed", signum=15)])
def test_base_build_seed_exception_warns_and_records_failure(mocker, make_cfg, tmp_path, stage, exc):
    from jailbee.cli import app
    from jailbee.config.models_golden import DockerSeed

    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed()})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    build = mocker.patch("jailbee.golden.build_golden_image")
    mocker.patch("jailbee.docker_seed.preflight", side_effect=exc if stage == "preflight" else None)
    mocker.patch("jailbee.cli._load_global", side_effect=exc if stage == "global" else None)
    mocker.patch("jailbee.docker_seed.run_seed", side_effect=exc if stage == "seed" else None)
    record = mocker.patch("jailbee.docker_seed.record_outcome")

    result = runner.invoke(app, ["base", "build"])

    assert result.exit_code == 0, result.exception
    build.assert_called_once()
    outcome = record.call_args.args[1]
    assert outcome.outcome == "failed"
    assert str(exc) in outcome.detail
    assert "Docker seeding failed" in result.output


def test_base_build_seed_keyboard_interrupt_propagates(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import base_build_cmd
    from jailbee.config.models_golden import DockerSeed

    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed()})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.golden.build_golden_image")
    mocker.patch("jailbee.docker_seed.preflight")
    mocker.patch("jailbee.cli._load_global")
    mocker.patch("jailbee.docker_seed.run_seed", side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        base_build_cmd()


def test_base_build_no_seed_option_skips_seeding(mocker, make_cfg, tmp_path) -> None:
    from jailbee.cli import app
    from jailbee.config.models_golden import DockerSeed

    cfg = make_cfg(tmp_path, golden={"docker_seed": DockerSeed()})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.golden.build_golden_image")
    preflight = mocker.patch("jailbee.docker_seed.preflight")

    result = runner.invoke(app, ["base", "build", "--no-docker-seed"])

    assert result.exit_code == 0, result.output
    preflight.assert_not_called()


def test_base_build_rejects_conflicting_seed_options() -> None:
    from jailbee.cli import app

    result = runner.invoke(app, ["base", "build", "--docker-from", "feature", "--no-docker-seed"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
