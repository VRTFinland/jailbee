import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QProcess

from jailbee.qtui.output import CommandOutputDialog, CommandOutputView


def test_view_streams_output_into_the_text_area(qtbot, mocker):
    view = CommandOutputView(["jailbee", "git", "diff", "alpha-x"])
    qtbot.addWidget(view)
    mocker.patch.object(view._proc, "start")
    read = mocker.patch.object(view._proc, "readAllStandardOutput", return_value=b"hello\n")

    view.start()
    view._drain()

    assert "hello" in view.text()
    assert read.called


def test_view_reports_the_exit_code_and_emits_finished(qtbot):
    view = CommandOutputView(["true"])
    qtbot.addWidget(view)

    with qtbot.waitSignal(view.finished, timeout=1000) as blocker:
        view._on_finished(3, QProcess.ExitStatus.NormalExit)

    assert blocker.args == [3]
    assert "3" in view.status_text()


def test_view_reports_a_crash_distinctly(qtbot):
    """A killed process must not read as 'exited 0' — `job log --follow` is
    ended by the Stop button, and that has to be visible."""
    view = CommandOutputView(["true"])
    qtbot.addWidget(view)

    view._on_finished(0, QProcess.ExitStatus.CrashExit)

    assert "stopped" in view.status_text().lower()


def test_view_reports_a_command_that_never_started(qtbot):
    view = CommandOutputView(["definitely-not-a-real-binary"])
    qtbot.addWidget(view)

    view._on_error(QProcess.ProcessError.FailedToStart)

    assert "could not be started" in view.status_text()


def test_stop_kills_a_running_process(qtbot, mocker):
    view = CommandOutputView(["sleep", "60"])
    qtbot.addWidget(view)
    mocker.patch.object(view._proc, "state", return_value=QProcess.ProcessState.Running)
    kill = mocker.patch.object(view._proc, "kill")

    view.stop()

    kill.assert_called_once_with()


def test_stop_is_a_no_op_when_nothing_is_running(qtbot, mocker):
    view = CommandOutputView(["true"])
    qtbot.addWidget(view)
    mocker.patch.object(view._proc, "state", return_value=QProcess.ProcessState.NotRunning)
    kill = mocker.patch.object(view._proc, "kill")

    view.stop()

    kill.assert_not_called()


def test_dialog_titles_itself_after_the_command(qtbot, mocker):
    mocker.patch.object(CommandOutputView, "start")
    dlg = CommandOutputDialog(["jailbee", "pr", "alpha-x"], title="jailbee pr alpha-x")
    qtbot.addWidget(dlg)

    assert dlg.windowTitle() == "jailbee pr alpha-x"
    assert isinstance(dlg.view, CommandOutputView)
