"""Repository-only validation avoids host configuration and runtime checks."""

from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def repo_config(tmp_path):
    path = tmp_path / "repo" / ".jailbee" / "config.yaml"
    path.parent.mkdir(parents=True)
    return path


@pytest.mark.parametrize("fixture", ["full_config.yaml", "minimal_config.yaml"])
def test_fixture_repo_layer_ok(repo_config, fixture):
    from jailbee.config.repo_layer import validate_repo_layer

    repo_config.write_text((Path(__file__).parent / "fixtures" / fixture).read_text())
    assert validate_repo_layer(repo_config).error is None


def test_unknown_optional_mount_is_unchecked(repo_config):
    from jailbee.config.repo_layer import validate_repo_layer
    from jailbee.config_validate_cmd import report_repo_layer

    repo_config.write_text("autostart:\n  on_start:\n    - stage: setup\n      mounts: [creds]\n")
    report = validate_repo_layer(repo_config)
    assert report.error is None
    assert len(report.unchecked) == 1
    assert "unchecked (host layer)" in report.unchecked[0]
    assert "'creds'" in report.unchecked[0]
    assert report_repo_layer(report) == 0


@pytest.mark.parametrize("text, token", [
    ("golden:\n  python: '3.11'\n", "golden.python"),
    ("dashboard: {}\n", "dashboard:"),
    ("chrome:\n  enabled: false\n", "chrome:"),
    ("claude:\n  ai_pr_description: false\n", "ai_pr_description"),
])
def test_repo_deprecations_reported_by_both_clis(repo_config, text, token):
    from jailbee.cli import app
    from jailbee.container_cli import container_app

    repo_config.write_text(text)
    runner = CliRunner()
    for cli, extra in ((app, ["--repo-layer"]), (container_app, [])):
        result = runner.invoke(cli, ["config", "validate", *extra, "--config", str(repo_config)])
        assert result.exit_code == 2, result.output
        assert token in result.output


def test_schema_error_exit_1(repo_config):
    from jailbee.config.repo_layer import validate_repo_layer
    from jailbee.config_validate_cmd import report_repo_layer

    repo_config.write_text("bogus: true\n")
    report = validate_repo_layer(repo_config)
    assert report.error is not None
    assert report_repo_layer(report) == 1


@pytest.mark.parametrize(
    "text",
    [
        "bogus: true\n",
        "{}\n",
        "autostart:\n  on_start:\n    - name: old\n      run: 'true'\n      mounts: [creds]\n",
    ],
)
def test_host_and_container_output_identical(repo_config, text):
    from jailbee.cli import app
    from jailbee.container_cli import container_app

    repo_config.write_text(text)
    runner = CliRunner()
    host = runner.invoke(app, ["config", "validate", "--repo-layer", "--config", str(repo_config)])
    container = runner.invoke(container_app, ["config", "validate", "--config", str(repo_config)])
    assert (host.exit_code, host.stdout, host.stderr) == (
        container.exit_code,
        container.stdout,
        container.stderr,
    )


def test_container_default_path_is_git_toplevel(repo_config, monkeypatch):
    from jailbee import git
    from jailbee.container_cli import container_app

    subdir = repo_config.parent.parent / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    monkeypatch.setattr(git, "repo_toplevel", lambda path: repo_config.parent.parent)
    repo_config.write_text("{}\n")
    result = CliRunner().invoke(container_app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    repo_config.unlink()
    result = CliRunner().invoke(container_app, ["config", "validate"])
    assert result.exit_code == 1
    assert "no .jailbee/config.yaml in this repository" in result.output


def test_repo_layer_ignores_host_layers(repo_config, monkeypatch, tmp_path):
    from jailbee import global_config
    from jailbee.config import loader
    from jailbee.config.repo_layer import validate_repo_layer

    global_path = tmp_path / "global.yaml"
    global_path.write_text("bogus: true\n")
    local_path = tmp_path / "local.yaml"
    local_path.write_text("bogus: true\n")
    monkeypatch.setattr(global_config, "default_global_config_path", lambda: global_path)
    monkeypatch.setattr(loader, "local_config_path", lambda prefix: local_path)
    repo_config.write_text("{}\n")
    assert validate_repo_layer(repo_config).error is None


def test_legacy_step_mount_warning_and_notice(repo_config):
    from jailbee.config.repo_layer import validate_repo_layer
    from jailbee.config_validate_cmd import report_repo_layer

    repo_config.write_text(
        "autostart:\n  on_start:\n    - name: old\n      run: 'true'\n      mounts: [creds]\n"
    )
    report = validate_repo_layer(repo_config)
    assert len(report.unchecked) == 1
    assert len(report.notices) == 1
    assert report_repo_layer(report) == 2


def test_git_toplevel_uses_git_wrapper(tmp_path, mocker):
    import subprocess

    from jailbee.git import repo_toplevel

    run = mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, "/work/repo\n", ""),
    )
    assert repo_toplevel(tmp_path) == Path("/work/repo")
    assert run.call_args.args[0] == ["git", "rev-parse", "--show-toplevel"]
    run.return_value = subprocess.CompletedProcess([], 128, "", "not a repository")
    assert repo_toplevel(tmp_path) is None


def test_validation_without_host_extras(repo_config):
    import subprocess
    import sys

    repo_config.write_text("{}\n")
    script = """
import builtins, sys
real = builtins.__import__
def guard(name, *args, **kwargs):
    if (name.split('.')[0] in {'sqlmodel', 'questionary', 'ruamel', 'textual'}
            or name == 'jailbee.cli'):
        raise ImportError('forbidden import: ' + name)
    return real(name, *args, **kwargs)
builtins.__import__ = guard
from jailbee.config import loader
loader.detect_upstream_remote = lambda path: None
loader.detect_default_branch = lambda path, remote: 'main'
from jailbee.container_cli import container_app
sys.argv = ['jailbee', 'config', 'validate', '--config', sys.argv[1]]
container_app()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo_config)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "Schema OK" in result.stdout


@pytest.fixture(autouse=True)
def mock_repo_git(monkeypatch):
    from jailbee.config import loader

    monkeypatch.setattr(loader, "detect_upstream_remote", lambda path: None)
    monkeypatch.setattr(loader, "detect_default_branch", lambda path, remote: "main")


@pytest.mark.parametrize("shape", ["steps", "chains"])
def test_nested_mounts_reported_but_host_rejects(repo_config, shape):
    from jailbee.config import ConfigError
    from jailbee.config.common import _parse_yaml_text
    from jailbee.config.loader import load_config_from_layers
    from jailbee.config.repo_layer import validate_repo_layer

    if shape == "steps":
        nested = (
            "      steps:\n        - name: setup\n"
            "          run: 'true'\n          mounts: [creds]\n"
        )
    else:
        nested = (
            "      chains:\n        - name: main\n          steps:\n"
            "            - name: setup\n              run: 'true'\n"
            "              mounts: [creds]\n"
        )
    text = "autostart:\n  on_start:\n    - stage: boot\n" + nested
    repo_config.write_text(text)
    report = validate_repo_layer(repo_config)
    assert report.error is None
    assert len(report.unchecked) == 1 and "'creds'" in report.unchecked[0]
    assert len(report.notices) == 1 and "setup" in report.notices[0]
    with pytest.raises(ConfigError, match="move them to the stage"):
        load_config_from_layers(
            {},
            _parse_yaml_text(text, str(repo_config)),
            repo_config,
            origin=str(repo_config),
            local_raw={},
        )


@pytest.mark.parametrize("host", [True, False])
def test_relative_config_matches_absolute(repo_config, monkeypatch, host):
    from jailbee.cli import app
    from jailbee.config.repo_layer import validate_repo_layer
    from jailbee.container_cli import container_app

    repo_config.write_text("{}\n")
    monkeypatch.chdir(repo_config.parent.parent)
    relative = Path(".jailbee/config.yaml")
    assert validate_repo_layer(relative).error is None
    args = ["config", "validate"] + (["--repo-layer"] if host else [])
    runner = CliRunner()
    a = runner.invoke(app if host else container_app, [*args, "--config", str(relative)])
    b = runner.invoke(app if host else container_app, [*args, "--config", str(repo_config)])
    assert (a.exit_code, a.stdout, a.stderr) == (b.exit_code, b.stdout, b.stderr)
    assert a.exit_code == 0
