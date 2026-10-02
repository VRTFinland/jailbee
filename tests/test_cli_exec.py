"""Tests for `jailbee exec`."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import panel_text

runner = CliRunner()


def test_exec_always_passes_the_gui_environment(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    ex = mocker.patch.object(Incus, "exec_interactive", return_value=0)
    runner.invoke(app, ["exec", "c1", "--", "true"])
    env = ex.call_args.kwargs["env"]
    assert env["HOME"] == "/home/dev"
    assert "WAYLAND_DISPLAY" in env and "DISPLAY" in env


def test_exec_returns_the_commands_exit_code(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch.object(Incus, "exec_interactive", return_value=3)
    assert runner.invoke(app, ["exec", "c1", "--", "false"]).exit_code == 3


def test_exec_detach_returns_immediately_and_names_the_log(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    interactive = mocker.patch.object(Incus, "exec_interactive")
    detached = mocker.patch("jailbee.gui.launch_detached")
    result = runner.invoke(app, ["exec", "-d", "c1", "--", "firefox"])
    assert result.exit_code == 0
    assert not interactive.called
    assert detached.called
    assert "/tmp/jailbee-exec-" in result.output


def test_exec_detach_gives_each_launch_a_distinct_log_path(tmp_path, mocker):
    """`launch_detached` opens the log with `>` (truncate). A timestamp alone
    has one-second resolution, so two `-d` launches against the same
    container inside one wall-clock second — a scripted loop, a
    double-launch — would silently clobber each other's output: exactly the
    "output goes somewhere the user cannot find" failure `--detach` exists
    to avoid. Two invocations back to back (same process, same second) must
    still get different paths.
    """
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch.object(Incus, "exec_interactive")
    detached = mocker.patch("jailbee.gui.launch_detached")

    runner.invoke(app, ["exec", "-d", "c1", "--", "firefox"])
    runner.invoke(app, ["exec", "-d", "c1", "--", "firefox"])

    log_paths = [call.args[4] for call in detached.call_args_list]
    assert len(log_paths) == 2
    assert log_paths[0] != log_paths[1]


def test_exec_detach_gui_from_a_gui_session_prepares_the_shared_display(
    tmp_path, mocker, monkeypatch
) -> None:
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    monkeypatch.setenv("JAILBEE_SSH_SESSION", "1")
    monkeypatch.setenv("JAILBEE_SSH_GUI", "8022")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    prepare = mocker.patch("jailbee.remote_display.prepare_shared_display")
    detached = mocker.patch("jailbee.gui.launch_detached")
    result = runner.invoke(app, ["exec", "-d", "--gui", "c1", "--", "firefox"])
    assert result.exit_code == 0
    prepare.assert_called_once()
    assert prepare.call_args.args[1] == "c1"
    assert detached.call_args.args[2]["WAYLAND_DISPLAY"] == "/run/jailbee-display/wayland-0"
    assert isinstance(prepare.call_args.args[0], Incus)


def test_exec_foreground_never_prepares_the_display(tmp_path, mocker, monkeypatch) -> None:
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    monkeypatch.setenv("JAILBEE_SSH_SESSION", "1")
    monkeypatch.setenv("JAILBEE_SSH_GUI", "8022")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    prepare = mocker.patch("jailbee.remote_display.prepare_shared_display")
    mocker.patch.object(Incus, "exec_interactive", return_value=0)
    runner.invoke(app, ["exec", "c1", "--", "true"])
    prepare.assert_not_called()


def test_exec_detach_gui_reports_a_display_error_cleanly(tmp_path, mocker, monkeypatch) -> None:
    from jailbee.remote_display import DisplayError
    from tests.conftest import make_cfg

    monkeypatch.setenv("JAILBEE_SSH_SESSION", "1")
    monkeypatch.setenv("JAILBEE_SSH_GUI", "8022")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch(
        "jailbee.remote_display.prepare_shared_display", side_effect=DisplayError("no client")
    )
    detached = mocker.patch("jailbee.gui.launch_detached")
    result = runner.invoke(app, ["exec", "-d", "--gui", "c1", "--", "firefox"])
    assert result.exit_code == 1
    assert "no client" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not detached.called


def test_exec_detach_without_gui_never_prepares_the_display(tmp_path, mocker, monkeypatch) -> None:
    """A detached `make test` in a GUI SSH session must run, not wait for RDP."""
    from tests.conftest import make_cfg

    monkeypatch.setenv("JAILBEE_SSH_SESSION", "1")
    monkeypatch.setenv("JAILBEE_SSH_GUI", "8022")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    prepare = mocker.patch("jailbee.remote_display.prepare_shared_display")
    detached = mocker.patch("jailbee.gui.launch_detached")

    result = runner.invoke(app, ["exec", "-d", "c1", "--", "make", "test"])

    assert result.exit_code == 0, result.output
    prepare.assert_not_called()
    assert detached.called
    assert detached.call_args.args[2]["WAYLAND_DISPLAY"] != "/run/jailbee-display/wayland-0"


def test_exec_gui_without_detach_is_rejected_with_exit_2(tmp_path, mocker) -> None:
    from jailbee.incus import Incus

    interactive = mocker.patch.object(Incus, "exec_interactive", return_value=0)
    detached = mocker.patch("jailbee.gui.launch_detached")

    result = runner.invoke(app, ["exec", "--gui", "c1", "--", "firefox"])

    assert result.exit_code == 2
    assert "--detach" in result.output
    assert not interactive.called
    assert not detached.called


def test_exec_detach_gui_on_the_host_uses_the_host_environment(
    tmp_path, mocker, monkeypatch
) -> None:
    from tests.conftest import make_cfg

    for name in ("JAILBEE_SSH_SESSION", "JAILBEE_SSH_GUI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    prepare = mocker.patch("jailbee.remote_display.prepare_shared_display")
    detached = mocker.patch("jailbee.gui.launch_detached")

    result = runner.invoke(app, ["exec", "-d", "--gui", "c1", "--", "firefox"])

    assert result.exit_code == 0, result.output
    prepare.assert_not_called()
    assert detached.call_args.args[2]["WAYLAND_DISPLAY"] == "wayland-1"


def test_exec_without_arguments_asks_for_both(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    resolve = mocker.patch("jailbee.cli._resolve_existing", return_value=(Incus(), "c1"))
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._ask", return_value="ls -la 'my dir'")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    run = mocker.patch.object(Incus, "exec_interactive", return_value=0)

    assert runner.invoke(app, ["exec"]).exit_code == 0
    assert resolve.call_args.args[1] is None
    assert run.call_args.args[1] == [
        "bash",
        "-lc",
        "cd /home/dev/repo && exec ls -la 'my dir'",
    ]


def test_exec_unbalanced_quotes_re_ask(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.cli._resolve_existing", return_value=(Incus(), "c1"))
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    ask = mocker.patch("jailbee.prompting._ask", side_effect=["echo 'oops", "true"])
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch.object(Incus, "exec_interactive", return_value=0)

    result = runner.invoke(app, ["exec", "c1"])
    assert result.exit_code == 0, result.output
    assert ask.call_count == 2


def test_exec_without_command_off_a_tty_exits_2(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.cli._resolve_existing", return_value=(Incus(), "c1"))
    result = runner.invoke(app, ["exec", "c1"])
    assert result.exit_code == 2
    assert "missing command" in panel_text(result.output)
