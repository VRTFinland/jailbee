"""CLI tests for `jailbee config migrate`."""

import yaml
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config.local_layer import local_config_path
from jailbee.global_config import default_global_config_path

runner = CliRunner()


def _global(data: dict, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = default_global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    path.chmod(0o600)


def test_dry_run_writes_nothing_and_shows_the_diff(tmp_path, monkeypatch):
    _global({"credentials": {"repos": {"a": "team"}}}, tmp_path, monkeypatch)
    before = default_global_config_path().read_text()

    result = runner.invoke(app, ["config", "migrate"])

    assert result.exit_code == 0, result.output
    assert "credentials.repos.a" in result.output
    assert "--apply" in result.output
    assert default_global_config_path().read_text() == before
    assert not local_config_path("a").exists()


def test_apply_writes_and_reports_backups(tmp_path, monkeypatch):
    _global({"credentials": {"repos": {"a": "team"}}}, tmp_path, monkeypatch)

    result = runner.invoke(app, ["config", "migrate", "--apply"])

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(local_config_path("a").read_text()) == {"credentials": {"group": "team"}}
    assert ".bak" in result.output


def test_nothing_to_migrate(tmp_path, monkeypatch):
    _global({}, tmp_path, monkeypatch)
    result = runner.invoke(app, ["config", "migrate"])
    assert result.exit_code == 0
    assert "Nothing to migrate" in result.output


def test_conflicts_are_listed_and_exit_zero(tmp_path, monkeypatch):
    _global({"credentials": {"repos": {"a": "old"}}}, tmp_path, monkeypatch)
    path = local_config_path("a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("credentials:\n  group: new\n")

    result = runner.invoke(app, ["config", "migrate", "--apply"])

    assert result.exit_code == 0
    assert "differs" in result.output


def test_dry_run_masks_github_tokens(tmp_path, monkeypatch):
    token = "ghp_secretmigrationtoken"
    _global({"github": {"api_tokens": {"a": token}}}, tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = runner.invoke(app, ["config", "migrate"])

    assert result.exit_code == 0
    assert "********" in result.output
    assert token not in result.output


def test_partial_write_failure_does_not_claim_nothing_was_written(tmp_path, monkeypatch, mocker):
    _global({"credentials": {"repos": {"a": "team"}}}, tmp_path, monkeypatch)

    def fail_after_a_write(inputs, plan, session):
        local = local_config_path("a")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("credentials:\n  group: team\n")
        raise OSError("simulated second-file write failure")

    mocker.patch("jailbee.config_migrate.apply_plan", side_effect=fail_after_a_write)
    result = runner.invoke(app, ["config", "migrate", "--apply"])

    assert result.exit_code == 1
    assert "may have partially written files" in result.output
    assert "Check the files and backups" in result.output
    assert "rerun `jailbee config migrate --apply`" in result.output
    assert "Nothing was written" not in result.output


def test_preflight_validation_failure_says_no_files_were_written(tmp_path, monkeypatch):
    _global({"credentials": {"repos": {"a": "team"}}}, tmp_path, monkeypatch)
    path = local_config_path("a")
    path.parent.mkdir(parents=True)
    path.write_text("egress_allow: invalid\n")

    result = runner.invoke(app, ["config", "migrate", "--apply"])

    assert result.exit_code == 1
    assert "Preflight failed; no files were written" in result.output
    assert "Nothing was written" not in result.output
    assert "egress_allow: invalid" in path.read_text()
