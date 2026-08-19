"""A Qt view that runs one `jailbee` command and shows what it prints.

`shell` and `tmux` still get a host terminal window — they need a real TTY.
The commands whose whole point is their output do not: spawning a terminal
emulator for them would close the window the moment the command exited,
taking the output with it. They run here instead, under a QProcess whose
merged stdout/stderr streams into a text view.

`CommandOutputView` is a plain QWidget so the same widget can later be
embedded in a docked panel in the main window; `CommandOutputDialog` is the
non-modal wrapper the dashboard opens today.
"""

from __future__ import annotations

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# `job log --follow` streams until it is stopped, so the document is bounded
# rather than growing for as long as the window stays open.
_MAX_BLOCKS = 5000


class CommandOutputView(QWidget):
    """Runs ``argv`` under a QProcess and streams its output into a text view.

    ``finished`` carries the exit code and fires for every ending — a clean
    exit, a failure, or the Stop button — so the host can refresh the
    dashboard once and only once.
    """

    finished = Signal(int)

    def __init__(self, argv: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._argv = argv

        self._text = QPlainTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(_MAX_BLOCKS)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # A diff is unreadable in a proportional font; StyleHint keeps this
        # working on hosts with no family literally called "monospace".
        font = QFont()
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamily("monospace")
        self._text.setFont(font)

        self._status = QLabel("running…", self)
        self._stop = QPushButton("Stop", self)
        self._stop.clicked.connect(self.stop)
        self._copy = QPushButton("Copy", self)
        self._copy.clicked.connect(self._copy_all)

        buttons = QHBoxLayout()
        buttons.addWidget(self._status, stretch=1)
        buttons.addWidget(self._copy)
        buttons.addWidget(self._stop)

        layout = QVBoxLayout(self)
        layout.addWidget(self._text, stretch=1)
        layout.addLayout(buttons)

        self._proc = QProcess(self)
        # One stream: interleaving stdout and stderr the way a terminal does
        # keeps an error next to the line that produced it.
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._drain)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)

    def start(self) -> None:
        """Spawn the command. Called once per view."""
        self._proc.start(self._argv[0], self._argv[1:])

    def stop(self) -> None:
        """End the command.

        Not optional: `jailbee job log --follow` polls for new output forever,
        so a window with no way to stop it would leave a process behind.
        """
        if self._proc.state() != QProcess.ProcessState.NotRunning:
            self._proc.kill()

    def text(self) -> str:
        """Everything shown so far (bounded by ``_MAX_BLOCKS``)."""
        return self._text.toPlainText()

    def status_text(self) -> str:
        """The status line — 'running…', an exit code, or a failure."""
        return self._status.text()

    def _drain(self) -> None:
        # QByteArray supports the buffer protocol at runtime, but PySide6's
        # stubs don't declare it, so mypy rejects the bytes() call.
        raw = bytes(self._proc.readAllStandardOutput())  # type: ignore[call-overload]
        chunk: str = raw.decode("utf-8", errors="replace")
        if not chunk:
            return
        # insertPlainText rather than appendPlainText: the output already
        # carries its own newlines, and appending would double them.
        self._text.moveCursor(QTextCursor.MoveOperation.End)
        self._text.insertPlainText(chunk)
        self._text.ensureCursorVisible()

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._drain()  # whatever landed between the last read and the exit
        if status == QProcess.ExitStatus.CrashExit:
            self._status.setText("stopped")
        else:
            self._status.setText("exited 0" if code == 0 else f"exited {code}")
        self._stop.setEnabled(False)
        self.finished.emit(code)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._status.setText(f"'{self._argv[0]}' could not be started")
            self._stop.setEnabled(False)

    def _copy_all(self) -> None:
        self._text.selectAll()
        self._text.copy()
        # Leaving everything selected would hide the next chunk under the
        # selection highlight.
        cursor = self._text.textCursor()
        cursor.clearSelection()
        self._text.setTextCursor(cursor)


class CommandOutputDialog(QDialog):
    """A non-modal window hosting one :class:`CommandOutputView`."""

    def __init__(self, argv: list[str], title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(900, 500)
        self.view = CommandOutputView(argv, self)
        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        self.view.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Never leave the command running behind a closed window."""
        self.view.stop()
        super().closeEvent(event)
