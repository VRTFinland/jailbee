from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QProcess

from jailbee.qtui.output import CommandOutputDialog, CommandOutputView

_CWD = Path("/repos/alpha")


def test_view_runs_the_command_in_the_given_working_directory(qtbot, mocker):
    """A repo with no config file is addressed by cwd alone, so a QProcess
    left in the GUI's own directory would run `git diff` against the wrong
    repo — and say nothing about it."""
    view = CommandOutputView(["jailbee", "git", "diff", "alpha-x"], _CWD)
    qtbot.addWidget(view)
    set_dir = mocker.patch.object(view._proc, "setWorkingDirectory")
    start = mocker.patch.object(view._proc, "start")

    view.start()

    set_dir.assert_called_once_with(str(_CWD))
    start.assert_called_once()


def test_dialog_hands_its_cwd_to_the_view(qtbot, mocker):
    """The dialog is what the dashboard actually opens; a cwd it accepted but
    dropped would be invisible."""
    set_dir = mocker.patch.object(QProcess, "setWorkingDirectory")
    mocker.patch.object(QProcess, "start")
    dlg = CommandOutputDialog(["jailbee", "pr", "alpha-x"], "jailbee pr alpha-x", _CWD)
    qtbot.addWidget(dlg)

    set_dir.assert_called_once_with(str(_CWD))


def test_view_streams_output_into_the_text_area(qtbot, mocker):
    view = CommandOutputView(["jailbee", "git", "diff", "alpha-x"], _CWD)
    qtbot.addWidget(view)
    mocker.patch.object(view._proc, "start")
    read = mocker.patch.object(view._proc, "readAllStandardOutput", return_value=b"hello\n")

    view.start()
    view._drain()

    assert "hello" in view.text()
    assert read.called


def test_view_reports_the_exit_code_and_emits_finished(qtbot):
    view = CommandOutputView(["true"], _CWD)
    qtbot.addWidget(view)

    with qtbot.waitSignal(view.finished, timeout=1000) as blocker:
        view._on_finished(3, QProcess.ExitStatus.NormalExit)

    assert blocker.args == [3]
    assert "3" in view.status_text()


def test_view_reports_a_crash_distinctly(qtbot):
    """A killed process must not read as 'exited 0' — `job log --follow` is
    ended by the Stop button, and that has to be visible."""
    view = CommandOutputView(["true"], _CWD)
    qtbot.addWidget(view)

    view._on_finished(0, QProcess.ExitStatus.CrashExit)

    assert "stopped" in view.status_text().lower()


def test_view_reports_a_command_that_never_started(qtbot):
    view = CommandOutputView(["definitely-not-a-real-binary"], _CWD)
    qtbot.addWidget(view)

    view._on_error(QProcess.ProcessError.FailedToStart)

    assert "could not be started" in view.status_text()


def test_stop_kills_a_running_process(qtbot, mocker):
    view = CommandOutputView(["sleep", "60"], _CWD)
    qtbot.addWidget(view)
    mocker.patch.object(view._proc, "state", return_value=QProcess.ProcessState.Running)
    kill = mocker.patch.object(view._proc, "kill")

    view.stop()

    kill.assert_called_once_with()


def test_stop_is_a_no_op_when_nothing_is_running(qtbot, mocker):
    view = CommandOutputView(["true"], _CWD)
    qtbot.addWidget(view)
    mocker.patch.object(view._proc, "state", return_value=QProcess.ProcessState.NotRunning)
    kill = mocker.patch.object(view._proc, "kill")

    view.stop()

    kill.assert_not_called()


def test_dialog_titles_itself_after_the_command(qtbot, mocker):
    mocker.patch.object(CommandOutputView, "start")
    dlg = CommandOutputDialog(["jailbee", "pr", "alpha-x"], "jailbee pr alpha-x", _CWD)
    qtbot.addWidget(dlg)

    assert dlg.windowTitle() == "jailbee pr alpha-x"
    assert isinstance(dlg.view, CommandOutputView)
