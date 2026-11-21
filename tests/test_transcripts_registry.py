"""The transcript source registry: agent name → source, imported on demand."""

from __future__ import annotations

import pytest

from jailbee.transcripts.base import TranscriptSource, get_source


def test_claude_has_a_source() -> None:
    source = get_source("claude")

    assert isinstance(source, TranscriptSource)
    assert source.name == "claude"


def test_claude_jb_reads_with_the_claude_source() -> None:
    assert get_source("claude-jb") is get_source("claude")


def test_an_agent_without_a_source_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        get_source("no-such-agent")
