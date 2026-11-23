"""Agent markup classification through the shared transcript text boundary."""

from __future__ import annotations

import time

import pytest

from jailbee.transcripts.markup import command_name, is_meta_wrapper, prompt_text, split_prompt


@pytest.mark.parametrize(
    "text",
    [
        "<example>broken</example>",
        "<system-reminder>Injected context</system-reminder>\nFix the session titles",
    ],
)
def test_typed_text_is_not_classified_as_agent_metadata(text: str) -> None:
    assert not is_meta_wrapper(text)


def test_command_title_can_follow_other_agent_wrappers() -> None:
    text = (
        "<local-command-caveat>Local only</local-command-caveat> "
        "<command-name>/model</command-name>"
    )
    assert command_name(text) == "/model"


@pytest.mark.parametrize(
    "text",
    [
        "<command-name>model</command-name>",
        "<command-name>/bad<name></command-name>",
        "<command-name>/bad\nname</command-name>",
        "<local-command-stdout>/model</local-command-stdout>",
        "<command-name>/model",
    ],
)
def test_only_a_well_formed_slash_command_can_supply_a_command_title(text: str) -> None:
    assert command_name(text) == ""


@pytest.mark.parametrize("tag", ["system-reminder", "command-name", "terminal"])
@pytest.mark.parametrize("tail", ["", "x", "\t\n"])
def test_an_unterminated_known_tag_with_long_whitespace_is_linear_time(tag: str, tail: str) -> None:
    for text in (f"<{tag}" + " " * 100_000 + tail, f"<{tag}>x</{tag}" + " " * 100_000):
        started = time.perf_counter()
        assert split_prompt(text) == ("", text.strip())
        assert prompt_text(text) == text.strip()
        assert time.perf_counter() - started < 0.5


def test_an_unclosed_inner_tag_of_the_same_name_still_splits_off_the_wrapper() -> None:
    text = (
        "<system-reminder>Note: tags like <system-reminder> are injected</system-reminder>\nTyped"
    )
    markup, typed = split_prompt(text)
    assert markup == text[: text.index("\nTyped")]
    assert typed == "Typed"


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("<task-notification>x</task-notification>", "Task notification"),
        ("<system-reminder>x</system-reminder>", "System reminder"),
        (
            "<command-name>/model</command-name><command-message>m</command-message>",
            "Command: /model",
        ),
        (
            "<command-name>/clear</command-name><command-args>old stuff</command-args>",
            "Command: /clear old stuff",
        ),
        ("<local-command-stdout>ok</local-command-stdout>", "Command output"),
        ("<bash-input>ls -la</bash-input>", "Shell: ls -la"),
        ("<bash-stdout>x</bash-stdout>", "Shell output"),
        ('<terminal name="b">x</terminal>', "Terminal"),
        ("plain text", ""),
    ],
)
def test_meta_label_names_the_wrapper(text: str, label: str) -> None:
    from jailbee.transcripts.markup import meta_label

    assert meta_label(text) == label
