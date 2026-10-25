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


# --- validation and command building ---------------------------------------

from pathlib import Path  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from jailbee.config import Config  # noqa: E402
from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoView  # noqa: E402
from jailbee.orchestration.runner import runner_command, runner_problems  # noqa: E402
from tests.conftest import make_cfg, with_agent  # noqa: E402


def _cfg(tmp_path: Path, *, litellm: bool = True, **claude: object) -> Config:
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    cfg = with_agent(cfg, "claude", **{"prompt_args": "-p", **claude})
    cfg._litellm_view = LiteLLMRepoView(config=LiteLLMConfig(enabled=litellm))
    return cfg


def _payload(mocker, profiles: dict | None, default: str = "grok") -> MagicMock:
    data = None if profiles is None else {"default_profile": default, "profiles": profiles}
    return mocker.patch(
        "jailbee.orchestration.runner.litellm.container_sync_payload",
        return_value={"json": data},
    )


def _problems(cfg: Config, runner: Runner) -> list[str]:
    return runner_problems(cfg, MagicMock(), MagicMock(), runner)


def test_runner_ok_for_native_claude(tmp_path, mocker) -> None:
    spy = _payload(mocker, {})
    assert _problems(_cfg(tmp_path), Runner(agent="claude", model="opus")) == []
    spy.assert_not_called()


def test_runner_refused_when_claude_disabled(tmp_path) -> None:
    cfg = with_agent(_cfg(tmp_path), "claude", enabled=False)
    assert _problems(cfg, Runner(agent="claude")) == ["agent `claude` is not enabled in this repo"]


def test_runner_refused_without_prompt_args(tmp_path) -> None:
    cfg = _cfg(tmp_path, prompt_args=None)
    assert _problems(cfg, Runner(agent="claude")) == [
        "agent `claude` has no prompt_args, so it cannot take a prompt"
    ]


def test_runner_refused_for_unrecognised_command(tmp_path) -> None:
    cfg = _cfg(tmp_path, command="env X=1 claude")
    assert _problems(cfg, Runner(agent="claude")) == [
        "agents.claude.command must start with `claude` or `claude-jb` to take a runner "
        "(it is `env X=1 claude`)"
    ]


def test_claude_jb_refused_without_litellm(tmp_path, mocker) -> None:
    _payload(mocker, {})
    cfg = _cfg(tmp_path, litellm=False)
    assert _problems(cfg, Runner(agent="claude-jb")) == [
        "claude-jb needs LiteLLM enabled in this repo"
    ]


def test_claude_jb_refused_for_unserved_profile(tmp_path, mocker) -> None:
    _payload(mocker, {"other": {"tiers": {"opus": "x"}}})
    assert _problems(_cfg(tmp_path), Runner(agent="claude-jb", profile="grok")) == [
        "LiteLLM profile `grok` is not served (configured: other; run `jailbee litellm up`)"
    ]


def test_claude_jb_unserved_when_nothing_running(tmp_path, mocker) -> None:
    _payload(mocker, None)
    assert _problems(_cfg(tmp_path), Runner(agent="claude-jb", profile="grok")) == [
        "LiteLLM profile `grok` is not served (configured: —; run `jailbee litellm up`)"
    ]


def test_claude_jb_refused_for_missing_tier(tmp_path, mocker) -> None:
    _payload(mocker, {"grok": {"tiers": {"sonnet": "x"}}})
    runner = Runner(agent="claude-jb", profile="grok", model="opus")
    assert _problems(_cfg(tmp_path), runner) == ["LiteLLM profile `grok` has no `opus` tier"]


def test_claude_jb_default_profile_checked(tmp_path, mocker) -> None:
    _payload(mocker, {"grok": {"tiers": {"sonnet": "x"}}}, default="grok")
    assert _problems(_cfg(tmp_path), Runner(agent="claude-jb", model="opus")) == [
        "LiteLLM profile `grok` has no `opus` tier"
    ]
    assert _problems(_cfg(tmp_path), Runner(agent="claude-jb", model="sonnet")) == []


def test_runner_command_rewrites() -> None:
    cases = [
        (
            "claude --dangerously-skip-permissions",
            Runner(agent="claude-jb", profile="grok", model="sonnet"),
            "claude-jb --dangerously-skip-permissions -P grok --model sonnet",
        ),
        ("claude-jb", Runner(agent="claude", model="opus"), "claude --model opus"),
        ("/usr/local/bin/claude", Runner(agent="claude"), "/usr/local/bin/claude"),
        ("aider", Runner(agent="claude"), None),
    ]
    for command, runner, expected in cases:
        assert runner_command(command, runner) == expected
