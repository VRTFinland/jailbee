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


def test_fully_successful_apply_records_an_observed_watermark(mocker: MockerFixture) -> None:
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RepoUpgradeState

    mocker.patch("jailbee.apply.run_apply", return_value=_fake_result())
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    with Session(get_engine()) as session:
        rows = list(session.exec(select(RepoUpgradeState)).all())
    assert len(rows) == 1
    assert rows[0].apply_observed is True


def test_partly_failed_apply_still_records_the_watermark(mocker: MockerFixture) -> None:
    """A restart failure must not keep the upgrade hint nagging forever.

    The advice asks one question: has `apply` run since the release that
    changed what `apply` writes. By the time `run_apply` returns, profiles,
    ACL, /etc/hosts and the dockerd proxy have all been written — new
    containers are correct. A container that refused to come back up is a
    separate failure, reported on its own line and by `jailbee doctor`, and
    it still makes the command exit 1. It used to also suppress the
    watermark, so `jb ls` went on advising an `apply` the user had run.
    """
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RepoUpgradeState

    mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(restart_failures=[("c", "boom")]),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 1
    with Session(get_engine()) as session:
        rows = list(session.exec(select(RepoUpgradeState)).all())
    assert len(rows) == 1
    assert rows[0].apply_observed is True


def test_apply_blocked_on_a_pool_still_records_the_watermark(mocker: MockerFixture) -> None:
    """Same reasoning for the case that prompted this: an unresolved pool
    root suppresses the restarts, but the config was written all the same."""
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RepoUpgradeState

    mocker.patch(
        "jailbee.apply.run_apply",
        return_value=_fake_result(profiles_changed=["p-binds"], unresolved_pools=["gradle"]),
    )
    mocker.patch("jailbee.incus.Incus")

    result = runner.invoke(app, ["apply", "--config", str(FIXTURES / "full_config.yaml")])

    assert result.exit_code == 0, result.output
    with Session(get_engine()) as session:
        rows = list(session.exec(select(RepoUpgradeState)).all())
    assert len(rows) == 1
    assert rows[0].apply_observed is True
