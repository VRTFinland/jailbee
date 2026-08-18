"""What the GUI has to ask before dispatching, and how the answers become flags.

The CLI asks these questions on a TTY. The GUI's child process has no stdin,
so each question becomes a Qt dialog and each answer an explicit flag. The
rule is *ask no more than the CLI would*: a repo that pinned
`push.default_action` has already answered, and a flag on top of that would
override the repo's own policy.

The flag-building functions here are pure, so they can be tested without
showing a dialog; the dialogs (added separately) only collect answers.
"""

from __future__ import annotations

from dataclasses import dataclass


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
