"""What the GUI has to ask before dispatching, and how the answers become flags.

The CLI asks these questions on a TTY. The GUI's child process has no stdin,
so each question becomes a Qt dialog and each answer an explicit flag. The
rule is *ask no more than the CLI would*: a repo that pinned
`push.default_action` has already answered, and a flag on top of that would
override the repo's own policy.

The flag-building functions here are pure, so they can be tested without
showing a dialog; the dialogs below only collect answers.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class PushAnswers:
    """Answers to `jailbee git push`'s two questions.

    ``action`` is "merge" / "rebase" / "plain", or None to leave it to the
    repo's `push.default_action`. ``source`` is a branch name, the literal
    "current" for the host's checked-out branch, or None to leave it to
    `push.default_source`.
    """

    action: str | None
    source: str | None


@dataclass(frozen=True)
class PrAnswers:
    """Answers to `jailbee pr`'s prompts.

    ``ready`` is True for --ready, False for --draft, None to leave the PR's
    draft state alone (the CLI's own default on an update). ``regenerate``
    asks Claude for a fresh description; ``confirm_foreign`` is the standing
    confirmation for publishing to a PR head jailbee did not create, which
    off-TTY is an error rather than a prompt.
    """

    ready: bool | None
    regenerate: bool
    confirm_foreign: bool


def push_questions(action_default: str, source_default: str) -> tuple[bool, bool]:
    """``(ask_action, ask_source)`` for a repo's push defaults."""
    return action_default == "ask", source_default == "ask"


def pr_refresh_title(name: str, pr_number: int | None) -> str:
    """The `git push --pr` dialog's title.

    Names the PR when the row knows its number. It always should — the menu
    entry exists only on a container carrying one — but the dispatch is by verb
    string, so a missing number stays a missing number rather than becoming a
    fabricated one.
    """
    if pr_number is None:
        return f"Refresh '{name}' from its PR head"
    return f"Refresh '{name}' from PR #{pr_number}"


def push_flags(answers: PushAnswers) -> list[str]:
    """The `jailbee git push` flags for ``answers`` (empty when nothing was asked)."""
    flags: list[str] = []
    if answers.action is not None:
        flags.append(f"--{answers.action}")
    if answers.source == "current":
        flags.append("--current")
    elif answers.source is not None:
        flags += ["--from", answers.source]
    return flags


def pr_flags(answers: PrAnswers) -> list[str]:
    """The `jailbee pr` flags for ``answers``."""
    flags: list[str] = []
    if answers.ready is True:
        flags.append("--ready")
    elif answers.ready is False:
        flags.append("--draft")
    if answers.regenerate:
        flags.append("--description")
    if answers.confirm_foreign:
        flags.append("--yes")
    return flags


def confirm_text(verb: str, name: str, base_branch: str | None) -> str:
    """The question to put in the confirmation dialog for ``verb``.

    `git pull` is the one bridge verb that writes to the *host* repo, which is
    not obvious from a menu entry, so the confirmation names the branch it
    merges into — or says plainly that it does not know which one.
    """
    if verb == "git pull":
        target = f"host branch '{base_branch}'" if base_branch else "its recorded base branch"
        return f"Merge '{name}' commits into {target}?"
    return f"{verb} {name}?"


_ACTIONS: tuple[tuple[str, str], ...] = (
    ("Merge the pushed ref into the container's branch", "merge"),
    ("Rebase the container's branch onto it", "rebase"),
    ("Transport only — push the ref, run nothing", "plain"),
)


class PushOptionsDialog(QDialog):
    """Asks `jailbee git push`'s open questions for one container.

    Only the questions the repo left open are shown: `push.default_action`
    defaults to "ask", so the action combo is the common case, while the source
    combo appears only for `push.default_source: ask`.

    The source choices are the two this dialog can express as flags without
    reading the host repo: the container's recorded base branch (`--from
    <base>`) and the host's checked-out branch (`--current`). A repo that wants
    the host's default branch instead should pin `push.default_source`.

    ``title`` overrides the window title for the caller that reuses this dialog
    for something other than a base update — `git push --pr`, whose source is
    the PR head rather than the base branch (and which therefore never asks the
    source question at all).
    """

    def __init__(
        self,
        name: str,
        *,
        ask_action: bool,
        ask_source: bool,
        base_branch: str | None,
        title: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or f"Update '{name}' from base")
        form = QFormLayout()
        self._action: QComboBox | None = None
        self._source: QComboBox | None = None
        if ask_action:
            self._action = QComboBox(self)
            for label, value in _ACTIONS:
                self._action.addItem(label, value)
            form.addRow("Action", self._action)
        if ask_source:
            self._source = QComboBox(self)
            if base_branch:
                self._source.addItem(f"Base branch ({base_branch})", base_branch)
            self._source.addItem("Current host branch", "current")
            form.addRow("Source", self._source)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def answers(self) -> PushAnswers:
        """What the user chose (None per question that was not asked)."""
        return PushAnswers(
            action=None if self._action is None else str(self._action.currentData()),
            source=None if self._source is None else str(self._source.currentData()),
        )


class PrOptionsDialog(QDialog):
    """Asks the three things `jailbee pr` would prompt for on a TTY.

    Off-TTY the CLI accepts the AI-proposed head branch name unchanged and
    skips the "regenerate the description?" offer, so those are the defaults
    here too; the adoption confirmation is an *error* off-TTY, which is why it
    is an explicit checkbox rather than a silent --yes.
    """

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Create or update PR for '{name}'")
        self._ready = QCheckBox("Mark the PR ready for review", self)
        self._regen = QCheckBox("Regenerate the description with Claude", self)
        self._adopt = QCheckBox("Confirm publishing to an existing PR's head branch", self)
        note = QLabel(
            "A new PR opens as a draft. Leave 'ready' unchecked to leave an "
            "existing PR's draft state alone.",
            self,
        )
        note.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for widget in (self._ready, self._regen, self._adopt, note, buttons):
            layout.addWidget(widget)

    def answers(self) -> PrAnswers:
        return PrAnswers(
            ready=True if self._ready.isChecked() else None,
            regenerate=self._regen.isChecked(),
            confirm_foreign=self._adopt.isChecked(),
        )
