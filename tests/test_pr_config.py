"""The `pr:` block, the legacy `ai_pr_*` fold, and the `headless` agent field."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jailbee.agent_presets import AGENT_PRESETS, claude_preset
from jailbee.config import AgentConfig, ClaudeAgentConfig, PrConfig, resolve_agents_raw
from jailbee.config.legacy_pr import fold_legacy_pr_keys
from tests.conftest import make_cfg

# --- PrConfig ----------------------------------------------------------------


def test_pr_defaults_follow_the_old_claude_defaults():
    pr = PrConfig()
    assert pr.agent == "auto"
    assert pr.ai_description is True
    assert pr.ai_branch is True
    assert pr.prompt is None
    assert pr.model is None
    assert pr.timeout == 600


@pytest.mark.parametrize("name", ["auto", "claude", "claude-jb", "codex", "my-agent2"])
def test_pr_agent_accepts_names(name):
    assert PrConfig(agent=name).agent == name


@pytest.mark.parametrize("bad", ["", "Claude", "a b", "a;b", "../x"])
def test_pr_agent_rejects_non_names(bad):
    with pytest.raises(ValidationError, match="agent"):
        PrConfig(agent=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_pr_timeout_rejects_non_positive(bad):
    with pytest.raises(ValidationError, match="timeout"):
        PrConfig(timeout=bad)


def test_pr_prompt_is_capped():
    with pytest.raises(ValidationError, match="prompt"):
        PrConfig(prompt="x" * 20_001)


@pytest.mark.parametrize("bad", ["sonnet --dangerously-skip-permissions", "   "])
def test_pr_model_must_be_a_single_token(bad):
    with pytest.raises(ValidationError, match="model"):
        PrConfig(model=bad)


def test_pr_model_null_is_distinct_from_unset():
    """Unset means "the agent's own PR default"; an explicit null means "inherit"."""
    assert "model" not in PrConfig().model_fields_set
    assert "model" in PrConfig(model=None).model_fields_set


def test_pr_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        PrConfig.model_validate({"nonsense": 1})


def test_config_carries_a_pr_block(tmp_path):
    cfg = make_cfg(tmp_path, pr={"agent": "codex", "timeout": 900})
    assert cfg.pr.agent == "codex"
    assert cfg.pr.timeout == 900


def test_config_pr_defaults_when_absent(tmp_path):
    assert make_cfg(tmp_path).pr == PrConfig()


# --- the legacy fold -----------------------------------------------------------


LEGACY = {
    "ai_pr_description": False,
    "ai_pr_branch": False,
    "ai_pr_model": "haiku",
    "ai_pr_timeout": 900,
    "pr_prompt": "Mention the ticket.",
}
FOLDED = {
    "ai_description": False,
    "ai_branch": False,
    "model": "haiku",
    "timeout": 900,
    "prompt": "Mention the ticket.",
}


def test_fold_moves_every_legacy_key_from_agents_claude():
    out, folded = fold_legacy_pr_keys({"agents": {"claude": {"enabled": True, **LEGACY}}})
    assert folded is True
    assert out["pr"] == FOLDED
    assert out["agents"]["claude"] == {"enabled": True}


def test_fold_moves_every_legacy_key_from_the_legacy_claude_block():
    out, folded = fold_legacy_pr_keys({"claude": {"enabled": True, **LEGACY}})
    assert folded is True
    assert out["pr"] == FOLDED
    assert out["claude"] == {"enabled": True}


def test_fold_keeps_an_explicit_null_model():
    out, _ = fold_legacy_pr_keys({"claude": {"ai_pr_model": None}})
    assert out["pr"] == {"model": None}


def test_fold_lets_an_explicit_pr_key_win():
    raw = {"pr": {"timeout": 1200}, "claude": {"ai_pr_timeout": 900, "ai_pr_model": "haiku"}}
    out, folded = fold_legacy_pr_keys(raw)
    assert folded is True
    assert out["pr"] == {"timeout": 1200, "model": "haiku"}


def test_fold_does_not_mutate_its_input():
    raw = {"claude": {"ai_pr_timeout": 900}}
    fold_legacy_pr_keys(raw)
    assert raw == {"claude": {"ai_pr_timeout": 900}}


def test_fold_is_a_noop_without_legacy_keys():
    raw = {"agents": {"claude": {"enabled": True}}, "pr": {"agent": "codex"}}
    out, folded = fold_legacy_pr_keys(raw)
    assert folded is False
    assert out == raw


def test_fold_leaves_a_malformed_pr_block_for_validation_to_reject():
    out, folded = fold_legacy_pr_keys({"pr": "nope", "claude": {"ai_pr_timeout": 900}})
    assert folded is False
    assert out["pr"] == "nope"


def test_resolve_agents_raw_folds_the_legacy_keys_too():
    """The merged-dict backstop: `make_cfg` and the editor reach it directly."""
    out = resolve_agents_raw({"claude": {"enabled": True, "ai_pr_timeout": 900}})
    assert out["pr"] == {"timeout": 900}
    assert "ai_pr_timeout" not in out["agents"]["claude"]


def test_make_cfg_accepts_the_legacy_spelling(tmp_path):
    cfg = make_cfg(tmp_path, claude={"enabled": True, "ai_pr_timeout": 900})
    assert cfg.pr.timeout == 900


def test_claude_agent_no_longer_declares_the_pr_fields():
    for legacy in LEGACY:
        assert legacy not in ClaudeAgentConfig.model_fields


# --- `headless` -----------------------------------------------------------------


def test_headless_defaults_to_none():
    assert AgentConfig().headless is None


def test_headless_must_not_be_blank():
    with pytest.raises(ValidationError, match="headless"):
        AgentConfig(headless="   ")


@pytest.mark.parametrize("name", ["codex", "gemini", "opencode", "pi"])
def test_presets_with_a_headless_command_read_the_prompt_from_the_environment(name):
    headless = AGENT_PRESETS[name]["headless"]
    assert isinstance(headless, str)
    assert '"$JAILBEE_PR_PROMPT"' in headless


@pytest.mark.parametrize("name", ["aider", "grok"])
def test_presets_without_a_known_headless_mode_declare_none(name):
    assert "headless" not in AGENT_PRESETS[name]


def test_claude_headless_preserves_the_established_command():
    headless = claude_preset()["headless"]
    assert isinstance(headless, str)
    assert '--session-id "$JAILBEE_PR_SESSION"' in headless
    assert '-p "$JAILBEE_PR_PROMPT"' in headless
    assert "--output-format json" in headless
    assert "--dangerously-skip-permissions" in headless
    assert '${JAILBEE_PR_MODEL:+--model "$JAILBEE_PR_MODEL"}' in headless
    assert headless.startswith("claude ")


# --- the load path ----------------------------------------------------------------


def _unwrapped_stderr(capsys) -> str:
    """Captured stderr with Rich's soft wrapping undone (see test_config_browsers)."""
    return capsys.readouterr().err.replace("\n", "")


def _load(global_raw, repo_raw, tmp_path, **kwargs):
    from jailbee.config.loader import load_config_from_layers

    path = tmp_path / ".jailbee" / "config.yaml"
    return load_config_from_layers(
        global_raw, {"container_prefix": "myrepo", **repo_raw}, path, origin=str(path), **kwargs
    )


def test_the_load_path_reads_the_legacy_spelling(tmp_path):
    cfg = _load({}, {"agents": {"claude": {"enabled": True, "ai_pr_timeout": 900}}}, tmp_path)
    assert cfg.pr.timeout == 900


def test_a_repos_legacy_key_beats_the_globals_pr_block(tmp_path):
    """The repo is the more specific layer; folding after the merge would invert that."""
    cfg = _load(
        {"pr": {"timeout": 1200}},
        {"agents": {"claude": {"enabled": True, "ai_pr_timeout": 900}}},
        tmp_path,
    )
    assert cfg.pr.timeout == 900


def test_a_pr_block_in_the_repo_beats_a_legacy_key_in_global(tmp_path):
    cfg = _load(
        {"claude": {"ai_pr_timeout": 1200}},
        {"pr": {"timeout": 900}},
        tmp_path,
    )
    assert cfg.pr.timeout == 900


def test_global_yaml_can_pick_the_agent(tmp_path):
    cfg = _load({"pr": {"agent": "codex"}}, {}, tmp_path)
    assert cfg.pr.agent == "codex"


def test_the_notice_names_the_file_that_carries_the_old_keys(tmp_path, capsys):
    from jailbee.global_config import default_global_config_path

    _load({"claude": {"ai_pr_timeout": 1200}}, {}, tmp_path)

    err = _unwrapped_stderr(capsys)
    assert "pr:" in err
    assert str(default_global_config_path()) in err
    assert str(tmp_path / ".jailbee" / "config.yaml") not in err


def test_no_notice_without_old_keys(tmp_path, capsys):
    _load({"pr": {"agent": "codex"}}, {}, tmp_path)
    assert "pr:" not in capsys.readouterr().err


def test_emit_hint_false_folds_silently(tmp_path, capsys):
    cfg = _load({}, {"claude": {"ai_pr_timeout": 900}}, tmp_path, emit_hint=False)
    assert cfg.pr.timeout == 900
    assert capsys.readouterr().err == ""
