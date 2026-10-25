from __future__ import annotations

import pytest
from pydantic import ValidationError

from jailbee.orchestration.runner import Runner, parse_runner_label


def test_runner_profile_only_with_claude_jb() -> None:
    with pytest.raises(ValidationError):
        Runner(agent="claude", profile="grok")


def test_runner_profile_uses_litellm_name_pattern() -> None:
    with pytest.raises(ValidationError):
        Runner(agent="claude-jb", profile="Grok;rm")
    assert Runner(agent="claude-jb", profile="grok").profile == "grok"


def test_runner_label_round_trip_and_describe() -> None:
    r = Runner(agent="claude-jb", profile="grok", model="sonnet")
    assert Runner.from_label(r.to_label()) == r
    assert r.describe() == "claude-jb grok sonnet"
    assert parse_runner_label("{not json") is None
    assert parse_runner_label(None) is None
    assert parse_runner_label(r.to_label()) == r


def test_label_is_compact_sorted_without_nulls() -> None:
    assert Runner(agent="claude", model="opus").to_label() == '{"agent":"claude","model":"opus"}'


def test_describe_variants() -> None:
    assert Runner(agent="claude", model="opus").describe() == "claude opus"
    assert Runner(agent="claude-jb").describe() == "claude-jb"


def test_from_label_raises_value_error() -> None:
    with pytest.raises(ValueError):
        Runner.from_label('{"agent":"codex"}')
    with pytest.raises(ValueError):
        Runner.from_label("nope")
