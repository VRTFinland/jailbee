"""A committed repo opt-out reaches host and container command boundaries."""

from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def github_opt_out(tmp_path, monkeypatch, mocker):
    xdg = tmp_path / ".config"
    global_path = xdg / "jailbee" / "global.yaml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("github:\n  enabled: true\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    path = tmp_path / "myrepo" / ".jailbee" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("github: {enabled: false}\n")
    return path


@pytest.mark.parametrize("layer", ["effective", "repo", "container"])
def test_config_validate_accepts_repo_github_opt_out(github_opt_out: Path, layer):
    from jailbee.cli import app
    from jailbee.container_cli import container_app

    cli = container_app if layer == "container" else app
    extra = ["--repo-layer"] if layer == "repo" else []
    result = CliRunner().invoke(
        cli, ["config", "validate", *extra, "--config", str(github_opt_out)]
    )

    assert result.exit_code == 0, result.output
    assert "Schema OK" in result.output


def test_doctor_accepts_repo_github_opt_out(github_opt_out: Path, mocker):
    from jailbee.cli import app
    from jailbee.doctor import _check_github

    mocker.patch("jailbee.incus.Incus")
    # Retain the real GitHub checks while excluding unrelated host prerequisites.
    run = mocker.patch(
        "jailbee.doctor.run_checks", side_effect=lambda cfg, incus, **kwargs: _check_github(cfg)
    )

    result = CliRunner().invoke(app, ["doctor", "--config", str(github_opt_out)])

    assert result.exit_code == 0, result.output
    assert run.call_args.args[0].github.enabled is False
    assert "github" not in result.output.lower()


def test_repo_opt_out_skips_github_egress_and_token_injection(github_opt_out: Path, tmp_path):
    from jailbee.autostart import _github_token_step
    from jailbee.config import load_config

    local = tmp_path / ".config" / "jailbee" / "repos" / "myrepo.yaml"
    local.parent.mkdir()
    local.write_text("github:\n  token: github_pat_test\n")
    local.chmod(0o600)
    cfg = load_config(github_opt_out)

    assert cfg.github.token is not None
    assert "api.github.com:443" not in cfg.effective_egress_allow()
    assert _github_token_step(cfg) is None


def test_host_local_github_setting_retains_precedence(github_opt_out: Path, tmp_path):
    from jailbee.config import load_config

    local = tmp_path / ".config" / "jailbee" / "repos" / "myrepo.yaml"
    local.parent.mkdir()
    local.write_text("github:\n  enabled: true\n  token: github_pat_test\n")
    local.chmod(0o600)

    cfg = load_config(github_opt_out)

    assert cfg.github.enabled is True
