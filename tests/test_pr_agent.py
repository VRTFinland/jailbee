"""Which in-container agent writes the PR text: `pr_ai.resolve_pr_agent`."""

from __future__ import annotations

import pytest

from jailbee.pr_ai import ai_branch_on, ai_description_on, resolve_pr_agent
from tests.conftest import make_cfg


def _cfg(tmp_path, agents=None, pr=None, litellm=None):
    cfg = make_cfg(tmp_path, agents=agents or {}, pr=pr or {})
    if litellm is not None:
        from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoView

        cfg._litellm_view = LiteLLMRepoView(config=LiteLLMConfig(**litellm))
    return cfg


ON = {"enabled": True}
AUTO = {"enabled": True, "autostart": True}


# --- auto -----------------------------------------------------------------------


def test_auto_picks_claude_when_it_is_the_only_enabled_agent(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"claude": ON}))
    assert choice.agent is not None
    assert choice.agent.name == "claude"
    assert choice.agent.headless.startswith("claude ")


def test_auto_prefers_the_autostart_agent_over_an_idle_claude(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"claude": ON, "codex": AUTO}))
    assert choice.agent is not None
    assert choice.agent.name == "codex"


def test_auto_prefers_claude_among_autostart_agents(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"claude": AUTO, "codex": AUTO}))
    assert choice.agent is not None
    assert choice.agent.name == "claude"


def test_auto_skips_an_autostart_agent_that_has_no_headless_mode(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"aider": AUTO, "claude": ON}))
    assert choice.agent is not None
    assert choice.agent.name == "claude"


def test_auto_without_a_usable_agent_is_empty_and_not_a_problem(tmp_path):
    """No agent configured is the ordinary "AI is off" state, not an error."""
    choice = resolve_pr_agent(_cfg(tmp_path))
    assert choice.agent is None
    assert choice.problem is None


def test_auto_with_only_a_headless_less_agent_is_empty(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"aider": AUTO}))
    assert choice.agent is None
    assert choice.problem is None


def test_auto_uses_claude_jb_when_litellm_autostarts(tmp_path):
    cfg = _cfg(tmp_path, {"claude": AUTO}, litellm={"enabled": True, "autostart": True})
    choice = resolve_pr_agent(cfg)
    assert choice.agent is not None
    assert choice.agent.name == "claude-jb"
    assert choice.agent.headless.startswith("claude-jb ")
    assert "--output-format json" in choice.agent.headless


@pytest.mark.parametrize("enabled,autostart", [(False, True), (True, False)])
def test_auto_needs_both_litellm_switches_for_claude_jb(tmp_path, enabled, autostart):
    cfg = _cfg(tmp_path, {"claude": AUTO}, litellm={"enabled": enabled, "autostart": autostart})
    choice = resolve_pr_agent(cfg)
    assert choice.agent is not None
    assert choice.agent.name == "claude"


def test_litellm_autostart_does_not_touch_a_non_claude_pick(tmp_path):
    cfg = _cfg(tmp_path, {"codex": AUTO}, litellm={"enabled": True, "autostart": True})
    choice = resolve_pr_agent(cfg)
    assert choice.agent is not None
    assert choice.agent.name == "codex"


# --- pinned -----------------------------------------------------------------------


def test_a_pinned_agent_is_used_even_when_another_autostarts(tmp_path):
    cfg = _cfg(tmp_path, {"claude": AUTO, "codex": ON}, pr={"agent": "codex"})
    choice = resolve_pr_agent(cfg)
    assert choice.agent is not None
    assert choice.agent.name == "codex"


def test_a_pinned_agent_that_is_not_enabled_is_a_problem(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"claude": ON}, pr={"agent": "codex"}))
    assert choice.agent is None
    assert choice.problem is not None
    assert "codex" in choice.problem
    assert "agents.codex.enabled" in choice.problem


def test_a_pinned_agent_with_no_headless_command_is_a_problem(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"aider": ON}, pr={"agent": "aider"}))
    assert choice.agent is None
    assert choice.problem is not None
    assert "agents.aider.headless" in choice.problem


def test_a_pinned_agent_that_does_not_exist_is_a_problem(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"claude": ON}, pr={"agent": "nope"}))
    assert choice.agent is None
    assert choice.problem is not None
    assert "nope" in choice.problem


def test_a_user_defined_agent_works_once_it_names_a_headless_command(tmp_path):
    agents = {"mine": {"enabled": True, "command": "mine", "headless": 'mine "$JAILBEE_PR_PROMPT"'}}
    choice = resolve_pr_agent(_cfg(tmp_path, agents, pr={"agent": "mine"}))
    assert choice.agent is not None
    assert choice.agent.headless == 'mine "$JAILBEE_PR_PROMPT"'


def test_claude_jb_can_be_pinned_without_litellm_autostart(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"claude": ON}, pr={"agent": "claude-jb"}))
    assert choice.agent is not None
    assert choice.agent.name == "claude-jb"
    assert choice.agent.headless.startswith("claude-jb ")


def test_pinned_claude_jb_needs_claude_enabled(tmp_path):
    choice = resolve_pr_agent(_cfg(tmp_path, {"codex": ON}, pr={"agent": "claude-jb"}))
    assert choice.agent is None
    assert choice.problem is not None
    assert "agents.claude.enabled" in choice.problem


def test_claude_jb_refuses_a_headless_that_does_not_start_with_claude(tmp_path):
    agents = {"claude": {"enabled": True, "headless": 'env X=1 claude -p "$JAILBEE_PR_PROMPT"'}}
    choice = resolve_pr_agent(_cfg(tmp_path, agents, pr={"agent": "claude-jb"}))
    assert choice.agent is None
    assert choice.problem is not None
    assert "claude-jb" in choice.problem


# --- the model ----------------------------------------------------------------------


def test_claude_family_defaults_to_sonnet(tmp_path):
    for pinned in ("claude", "claude-jb"):
        cfg = _cfg(tmp_path, {"claude": ON}, pr={"agent": pinned})
        agent = resolve_pr_agent(cfg).agent
        assert agent is not None
        assert agent.model == "sonnet"


def test_other_agents_default_to_their_own_model(tmp_path):
    agent = resolve_pr_agent(_cfg(tmp_path, {"codex": ON}, pr={"agent": "codex"})).agent
    assert agent is not None
    assert agent.model is None


def test_an_explicit_model_applies_to_any_agent(tmp_path):
    cfg = _cfg(tmp_path, {"codex": ON}, pr={"agent": "codex", "model": "gpt-5"})
    agent = resolve_pr_agent(cfg).agent
    assert agent is not None
    assert agent.model == "gpt-5"


def test_an_explicit_null_model_overrides_the_claude_default(tmp_path):
    cfg = _cfg(tmp_path, {"claude": ON}, pr={"model": None})
    agent = resolve_pr_agent(cfg).agent
    assert agent is not None
    assert agent.model is None


def test_only_the_claude_family_has_a_resumable_transcript(tmp_path):
    cfg = _cfg(tmp_path, {"claude": ON, "codex": ON})
    claude = resolve_pr_agent(cfg).agent
    cfg = _cfg(tmp_path, {"claude": ON, "codex": ON}, pr={"agent": "codex"})
    codex = resolve_pr_agent(cfg).agent
    assert claude is not None and claude.resumable is True
    assert codex is not None and codex.resumable is False


# --- the switches the CLI reads ---------------------------------------------------------


def test_ai_is_off_when_no_agent_is_available(tmp_path):
    cfg = _cfg(tmp_path)
    assert ai_description_on(cfg, no_ai=False) is False
    assert ai_branch_on(cfg, no_ai=False) is False


def test_ai_is_on_with_a_usable_agent(tmp_path):
    cfg = _cfg(tmp_path, {"claude": ON})
    assert ai_description_on(cfg, no_ai=False) is True
    assert ai_branch_on(cfg, no_ai=False) is True


def test_no_ai_turns_both_off(tmp_path):
    cfg = _cfg(tmp_path, {"claude": ON})
    assert ai_description_on(cfg, no_ai=True) is False
    assert ai_branch_on(cfg, no_ai=True) is False


def test_the_config_flags_gate_each_surface_independently(tmp_path):
    cfg = _cfg(tmp_path, {"claude": ON}, pr={"ai_description": False})
    assert ai_description_on(cfg, no_ai=False) is False
    assert ai_branch_on(cfg, no_ai=False) is True
    cfg = _cfg(tmp_path, {"claude": ON}, pr={"ai_branch": False})
    assert ai_description_on(cfg, no_ai=False) is True
    assert ai_branch_on(cfg, no_ai=False) is False


def test_a_pinned_but_unusable_agent_keeps_ai_on_so_generation_can_explain(tmp_path):
    """Switching it off silently would hide the misconfiguration the user just made."""
    cfg = _cfg(tmp_path, {"claude": ON}, pr={"agent": "codex"})
    assert ai_description_on(cfg, no_ai=False) is True
    assert ai_branch_on(cfg, no_ai=False) is True


# --- auto builds on primary_agent ------------------------------------------------


def _legacy_auto(cfg):
    """Verbatim copy of the pre-primary_agent `auto` branch, frozen for comparison."""
    from jailbee.autostart import litellm_autostart_on
    from jailbee.pr_ai import PrAgentChoice, _through_claude_jb, _usable

    enabled = sorted(
        (n for n, a in cfg.agents.items() if a.enabled), key=lambda n: (n != "claude", n)
    )
    pools = (
        [n for n in enabled if cfg.agents[n].autostart],
        enabled,
    )
    for pool in pools:
        for name in pool:
            agent = _usable(cfg, name)
            if agent is None:
                continue
            if name == "claude" and litellm_autostart_on(cfg):
                return PrAgentChoice(_through_claude_jb(agent) or agent)
            return PrAgentChoice(agent)
    return PrAgentChoice(None)


def test_auto_unchanged_for_every_preset_combination(tmp_path):
    import itertools

    names = ["claude", "codex", "gemini", "aider", "opencode", "grok"]
    base = _cfg(tmp_path, {n: ON for n in names})
    states = [
        {"enabled": False, "autostart": False},
        {"enabled": True, "autostart": False},
        {"enabled": True, "autostart": True},
    ]
    for combo in itertools.product(states, repeat=len(names)):
        agents = {n: base.agents[n].model_copy(update=s) for n, s in zip(names, combo, strict=True)}
        cfg = base.model_copy(update={"agents": agents})
        new, old = resolve_pr_agent(cfg), _legacy_auto(cfg)
        assert (new.agent and new.agent.name, new.problem) == (
            old.agent and old.agent.name,
            old.problem,
        ), combo


def test_explicit_primary_agent_steers_pr_auto(tmp_path):
    cfg = _cfg(tmp_path, {"claude": AUTO, "codex": ON}).model_copy(
        update={"primary_agent": "codex"}
    )
    choice = resolve_pr_agent(cfg)
    assert choice.agent is not None
    assert choice.agent.name == "codex"


def test_explicit_primary_without_headless_falls_back_to_search(tmp_path):
    cfg = _cfg(tmp_path, {"aider": AUTO, "claude": ON}).model_copy(
        update={"primary_agent": "aider"}
    )
    choice = resolve_pr_agent(cfg)
    assert choice.agent is not None
    assert choice.agent.name == "claude"


def test_explicit_primary_claude_is_rerouted_through_claude_jb(tmp_path):
    cfg = _cfg(
        tmp_path,
        {"claude": ON, "codex": AUTO},
        litellm={"enabled": True, "autostart": True},
    ).model_copy(update={"primary_agent": "claude"})
    choice = resolve_pr_agent(cfg)
    assert choice.agent is not None
    assert choice.agent.name == "claude-jb"
