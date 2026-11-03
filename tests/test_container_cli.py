"""Container command tree and early startup checks."""

import subprocess
import sys
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from jailbee import cli_context


def test_host_commands_snapshot():
    from jailbee.cli import app

    assert (
        cli_context.HOST_COMMANDS
        == set(typer.main.get_command(app).commands) - cli_context.SHARED_COMMANDS
    )


def test_container_validate_config_help():
    from jailbee.container_cli import container_app

    result = CliRunner().invoke(container_app, ["config", "validate", "--help"])
    assert result.exit_code == 0
    assert "Path to" in result.output


def test_container_help_lists_only_shared():
    from jailbee.container_cli import container_app

    result = CliRunner().invoke(container_app, ["--help"])
    assert result.exit_code == 0
    assert "config" in result.output and "version" in result.output
    assert all(name not in result.output for name in ("destroy", "orchestrate", "new"))


def test_container_cli_lists_inbox():
    from jailbee.container_cli import container_app

    result = CliRunner().invoke(container_app, ["--help"])
    assert result.exit_code == 0
    assert "inbox" in result.output


@pytest.mark.parametrize("handshake", [False, True])
def test_container_cli_inbox_forwards_args(monkeypatch, mocker, tmp_path, handshake):
    from jailbee import container_cli

    identity = tmp_path / "VERSION"
    identity.write_text("1.8.1 aaaa\n")
    monkeypatch.setattr(container_cli, "VERSION_FILE", identity)
    target = mocker.patch("jailbee.inbox.cli.main", return_value=3)
    prefix = ["--expect-version", "1.8.1 aaaa"] if handshake else []
    result = CliRunner().invoke(
        container_cli.container_app, [*prefix, "inbox", "send", "message", "--text", "x"]
    )
    assert result.exit_code == 3
    target.assert_called_once_with(["send", "message", "--text", "x"])


def test_container_cli_inbox_help_and_version_gate(monkeypatch, tmp_path):
    from jailbee import container_cli

    identity = tmp_path / "VERSION"
    identity.write_text("1.8.1 aaaa\n")
    monkeypatch.setattr(container_cli, "VERSION_FILE", identity)
    runner = CliRunner()
    result = runner.invoke(container_cli.container_app, ["--expect-version", "1.8.1 aaaa", "inbox", "--help"])
    assert result.exit_code == 0
    assert "Usage: jailbee inbox" in result.output
    result = runner.invoke(container_cli.container_app, ["--expect-version", "wrong", "inbox", "--help"])
    assert result.exit_code == 86
    assert "version mismatch" in result.output


def test_container_main_dispatches_inbox_without_typer(monkeypatch, mocker, tmp_path):
    from jailbee import container_main

    monkeypatch.setattr(container_main.container_deps, "check", lambda: None)
    monkeypatch.setattr(container_main, "PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["jailbee", "inbox", "help"])
    monkeypatch.delitem(sys.modules, "jailbee.container_cli", raising=False)
    target = mocker.patch("jailbee.inbox.cli.main", return_value=0)
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 0
    target.assert_called_once_with(["help"])
    assert "jailbee.container_cli" not in sys.modules


def test_expect_version_mismatch_exits_86(tmp_path, monkeypatch):
    from jailbee import container_cli

    identity = tmp_path / "VERSION"
    identity.write_text("1.8.1 aaaa\n")
    monkeypatch.setattr(container_cli, "VERSION_FILE", identity)
    runner = CliRunner()
    assert (
        runner.invoke(
            container_cli.container_app, ["--expect-version", "1.8.1 bbbb", "version"]
        ).exit_code
        == 86
    )
    assert (
        runner.invoke(
            container_cli.container_app, ["--expect-version", "1.8.1 aaaa", "version"]
        ).exit_code
        == 0
    )


@pytest.mark.parametrize("option", ["--version", "--help"])
@pytest.mark.parametrize("before", [True, False])
def test_skew_beats_eager_options(tmp_path, monkeypatch, option, before):
    from jailbee import container_cli

    identity = tmp_path / "VERSION"
    identity.write_text("1.8.1 aaaa\n")
    monkeypatch.setattr(container_cli, "VERSION_FILE", identity)
    handshake = ["--expect-version", "1.8.1 bbbb"]
    args = [*handshake, option] if before else [option, *handshake]
    result = CliRunner().invoke(container_cli.container_app, args)
    assert result.exit_code == 86
    assert CliRunner().invoke(container_cli.container_app, [option]).exit_code == 0


def test_identity_stays_on_imported_stage_after_symlink_swap(tmp_path):
    import shutil

    from jailbee import container_cli

    first = tmp_path / "app-first"
    second = tmp_path / "app-second"
    first.mkdir()
    second.mkdir()
    shutil.copytree(
        Path(container_cli.__file__).parent,
        first / "jailbee",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (first / "VERSION").write_text("1.8.1 aaaa\n")
    (second / "VERSION").write_text("1.8.1 bbbb\n")
    current = tmp_path / "current"
    current.symlink_to(first, target_is_directory=True)
    script = """
import sys
from pathlib import Path
from jailbee.container_cli import container_app
current = Path(sys.argv[1])
current.unlink()
current.symlink_to(sys.argv[2], target_is_directory=True)
sys.argv = ['jailbee', '--expect-version', '1.8.1 aaaa', 'version']
container_app()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(current), str(second)],
        cwd=tmp_path,
        env={"PYTHONPATH": str(current)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_fresh_import_dependency_gate_blocks_typer():
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'typer' or name.startswith('typer.') or name == 'jailbee.container_cli':
        raise AssertionError('early import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from jailbee import container_main
container_main.container_deps.check = lambda: 'deps unavailable'
container_main.main()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 87, result.stderr
    assert "deps unavailable" in result.stderr


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
