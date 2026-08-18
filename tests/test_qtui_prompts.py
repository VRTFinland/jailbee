import pytest

pytest.importorskip("PySide6")

from jailbee.qtui.prompts import (
    PrAnswers,
    PushAnswers,
    confirm_text,
    pr_flags,
    push_flags,
    push_questions,
)


def test_push_questions_only_asks_what_the_config_left_open():
    """The CLI asks exactly when its default is 'ask'; the GUI must not ask
    more than that, or it would override the repo's own policy."""
    assert push_questions("ask", "base") == (True, False)
    assert push_questions("merge", "ask") == (False, True)
    assert push_questions("ask", "ask") == (True, True)
    assert push_questions("rebase", "current") == (False, False)


def test_push_flags_names_the_action_and_the_source():
    assert push_flags(PushAnswers(action="merge", source=None)) == ["--merge"]
    assert push_flags(PushAnswers(action=None, source="current")) == ["--current"]
    assert push_flags(PushAnswers(action="rebase", source="main")) == [
        "--rebase",
        "--from",
        "main",
    ]


def test_push_flags_is_empty_when_the_config_already_decided():
    """Nothing to pass: `jailbee git push` resolves both from its own config,
    and a flag would silently override it."""
    assert push_flags(PushAnswers(action=None, source=None)) == []


def test_pr_flags_maps_each_answer_to_its_flag():
    assert pr_flags(PrAnswers(ready=True, regenerate=False, confirm_foreign=False)) == ["--ready"]
    assert pr_flags(PrAnswers(ready=False, regenerate=False, confirm_foreign=False)) == ["--draft"]
    assert pr_flags(PrAnswers(ready=None, regenerate=True, confirm_foreign=True)) == [
        "--description",
        "--yes",
    ]


def test_pr_flags_leaves_the_draft_state_alone_by_default():
    """`ready=None` means "don't touch it" — the CLI's own default on an
    update, and passing --draft would demote a PR the user marked ready."""
    assert pr_flags(PrAnswers(ready=None, regenerate=False, confirm_foreign=False)) == []


def test_confirm_text_names_the_host_branch_git_pull_merges_into():
    text = confirm_text("git pull", "alpha-x", "main")
    assert "alpha-x" in text
    assert "main" in text


def test_confirm_text_stays_honest_when_the_base_is_unknown():
    text = confirm_text("git pull", "alpha-x", None)
    assert "alpha-x" in text
    assert "base branch" in text
