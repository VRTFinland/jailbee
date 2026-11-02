"""Container command tree and early startup checks."""

import sys

import pytest
import typer.main
from typer.testing import CliRunner

from jailbee import cli_context


def test_host_commands_snapshot():
    from jailbee.cli import app

    assert cli_context.HOST_COMMANDS == set(typer.main.get_command(app).commands) - cli_context.SHARED_COMMANDS


def test_container_help_lists_only_shared():
    from jailbee.container_cli import container_app

    result = CliRunner().invoke(container_app, ["--help"])
    assert result.exit_code == 0
    assert "config" in result.output and "version" in result.output
    assert all(name not in result.output for name in ("destroy", "orchestrate", "new"))


def test_expect_version_mismatch_exits_86(tmp_path, monkeypatch):
    from jailbee import container_cli

    identity = tmp_path / "VERSION"
    identity.write_text("1.8.1 aaaa\n")
    monkeypatch.setattr(container_cli, "VERSION_FILE", identity)
    runner = CliRunner()
    assert runner.invoke(container_cli.container_app, ["--expect-version", "1.8.1 bbbb", "version"]).exit_code == 86
    assert runner.invoke(container_cli.container_app, ["--expect-version", "1.8.1 aaaa", "version"]).exit_code == 0


def test_container_host_command_errors(monkeypatch, capsys):
    from jailbee import container_main

    monkeypatch.setattr(container_main.container_deps, "check", lambda: None)
    monkeypatch.setattr(container_main, "PACKAGE_DIR", container_main.Path(__file__).parent)
    monkeypatch.setattr(sys, "argv", ["jailbee", "new"])
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 2
    assert "`jailbee new` runs on the host" in capsys.readouterr().err


def test_deps_failure_before_typer(monkeypatch, capsys):
    import builtins

    from jailbee import container_main

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name not in {"typer", "jailbee.container_cli"}
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(container_main.container_deps, "check", lambda: "deps unavailable")
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 87
    assert "deps unavailable" in capsys.readouterr().err


def test_missing_mount(monkeypatch, tmp_path, capsys):
    from jailbee import container_main

    monkeypatch.setattr(container_main.container_deps, "check", lambda: None)
    monkeypatch.setattr(container_main, "PACKAGE_DIR", tmp_path / "missing")
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 88
    assert "run `jb apply` on the host" in capsys.readouterr().err
