"""Agent markup classification through the shared transcript text boundary."""

from __future__ import annotations

import pytest

from jailbee.transcripts.markup import command_name, is_meta_wrapper


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
    text = "<local-command-caveat>Local only</local-command-caveat> <command-name>/model</command-name>"
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
