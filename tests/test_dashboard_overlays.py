"""The prompt's pure data: PR-number parsing, the suggestion filter, the answer check."""

from __future__ import annotations

from jailbee.dashboard import overlays as ov

_BRANCHES = ("main", "feat/maint", "release/main-fix", "develop")


def _prompt(**kw) -> ov.TextPrompt:
    base = {"purpose": "new-branch", "title": "New container", "label": "New branch"}
    return ov.TextPrompt(**{**base, **kw})


def test_pr_number_validation():
    assert ov.parse_pr_number("42") == 42
    for bad in ("", "0", "-3", "4x", "١٢", "9" * 5000, "1.5"):
        assert ov.parse_pr_number(bad) is None
    prompt = _prompt(purpose="new-pr", label="PR number")
    assert ov.validate_answer(prompt, "abc") == "PR number must be a positive whole number"


def test_enter_on_a_blank_answer_is_refused_with_the_label():
    for blank in ("", "   "):
        assert ov.validate_answer(_prompt(), blank) == "New branch cannot be empty"


def test_a_valid_answer_passes_and_the_caller_trims():
    assert ov.validate_answer(_prompt(), " feat ") is None


def test_filter_suggestions_puts_prefix_matches_first_case_insensitively():
    assert ov.filter_suggestions(_BRANCHES, "MAIN") == ["main", "feat/maint", "release/main-fix"]
    assert ov.filter_suggestions(_BRANCHES, "") == list(_BRANCHES)
    assert ov.filter_suggestions(_BRANCHES, "zzz") == []


def test_require_suggestion_rejects_an_unlisted_name_and_accepts_a_listed_one():
    prompt = _prompt(suggestions=_BRANCHES, require_suggestion=True)
    assert ov.validate_answer(prompt, "nope") == "'nope' is not one of the listed branches"
    assert ov.validate_answer(prompt, " develop ") is None


def test_empty_suggestions_keep_a_plain_prompt():
    prompt = _prompt(suggestions=(), require_suggestion=True)
    assert (
        ov.validate_answer(prompt, "x") is None
    )  # nothing to check against: the CLI is the backstop


def test_suggestions_without_require_take_free_text():
    assert ov.validate_answer(_prompt(suggestions=_BRANCHES), "mai") is None
