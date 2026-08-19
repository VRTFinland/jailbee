"""Tests for the `gie apply` CLI command."""

from __future__ import annotations

from pathlib import Path

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def _fake_result(**overrides):
    from jailbee.apply import ApplyResult

    defaults = dict(
        profiles_changed=[],
        profiles_unchanged=[],
        acl_changed=False,
        hosts_repinned=[],
        docker_proxy_reapplied=[],
        restarted=[],
        restart_failures=[],
        offline_migrated=[],
        ports_changed=[],
    )
    defaults.update(overrides)
    return ApplyResult(**defaults)  # type: ignore[arg-type]


def test_cli_apply_invokes_run_apply(mocker: MockerFixture) -> None:
    run_apply = mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert run_apply.call_count == 1
    kwargs = run_apply.call_args.kwargs
    assert kwargs["assume_yes"] is False
    assert kwargs["no_restart"] is False


def test_cli_apply_passes_yes_flag(mocker: MockerFixture) -> None:
    run_apply = mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(
        app,
        [
            "apply",
            "--yes",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert run_apply.call_args.kwargs["assume_yes"] is True


def test_cli_apply_passes_no_restart_flag(mocker: MockerFixture) -> None:
    run_apply = mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(
        app,
        [
            "apply",
            "--no-restart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert run_apply.call_args.kwargs["no_restart"] is True


def test_cli_apply_reports_up_to_date_when_nothing_changed(mocker: MockerFixture) -> None:
    mocker.patch("jailbee.apply.run_apply", return_value=_fake_result())
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output


def test_cli_apply_offline_migration_counts_as_a_change(mocker: MockerFixture) -> None:
    """A run whose only effect was migrating containers off `net-offline`
    must not claim nothing happened — the per-container info() lines above
    the summary say otherwise."""
    mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(offline_migrated=["foo-feat-x"]),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert "already up to date" not in result.output
    assert "Apply complete" in result.output


def test_cli_apply_ports_changed_counts_as_a_change(mocker: MockerFixture) -> None:
    """A run whose only effect was reconciling port forwards must not claim
    nothing happened — the per-container info() lines above the summary
    say otherwise."""
    mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(ports_changed=["foo-feat-x"]),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    assert "already up to date" not in result.output
    assert "Apply complete" in result.output


def test_cli_apply_nonzero_exit_on_restart_failures(mocker: MockerFixture) -> None:
    mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(
            profiles_changed=["foo-binds"],
            restarted=["a"],
            restart_failures=[("b", "boom")],
        ),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "-y", "--config", str(FIXTURES / "full_config.yaml")])
    assert result.exit_code == 1


def test_cli_apply_nonzero_exit_on_port_failures(mocker: MockerFixture) -> None:
    """A reconciliation failure on one container is reported and still fails
    the command's exit code, the same way a restart failure does — the run
    is not fully successful even though the sweep continued for the rest."""
    mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(
            port_failures=[("foo-feat-x", "something is already listening on port 5037")],
        ),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "-y", "--config", str(FIXTURES / "full_config.yaml")])
    assert result.exit_code == 1
    output = result.stdout + (result.stderr or "")
    assert "foo-feat-x" in output
    assert "already listening on port 5037" in output
