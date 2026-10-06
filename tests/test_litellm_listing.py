"""`jailbee litellm ls` text — pure, no Incus."""

from jailbee.config.local_layer import LocalLiteLLMView
from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoOverlay, LiteLLMRepoView
from jailbee.litellm_listing import listing_lines


def _view(prefix: str, scope: str | None, **overlay: object) -> LocalLiteLLMView:
    host = LiteLLMConfig(enabled=True)
    merged = host.with_overlay(LiteLLMRepoOverlay.model_validate(overlay))
    return LocalLiteLLMView(
        prefix, LiteLLMRepoView(config=merged, scope=scope, origin=f"/c/repos/{prefix}.yaml")
    )


def test_host_block_lists_the_builtin_profile_and_its_efforts():
    lines = listing_lines(LiteLLMConfig(enabled=True), [], global_origin="/c/global.yaml")
    text = "\n".join(lines)
    assert lines[0] == "global  (/c/global.yaml)"
    assert "default profile: codex · autostart: off · aliases: jb.<profile>.<level>" in text
    assert "opus    jb.codex.capable       sol-high" in text
    assert "codex*  account default" in text
    assert "fable" in text and "chatgpt/gpt-6-astra" in text and "high (fixed)" in text
    assert "high (fixed)" in text and "272000 tokens" in text


def test_a_raisable_route_shows_its_ceiling_and_a_fixed_one_does_not():
    cfg = LiteLLMConfig.model_validate(
        {
            "routes": {
                "fixed": {
                    "model": "openai/x",
                    "context_window": 100_000,
                    "api_base": "https://llm.example.com/v1",
                }
            },
            "profiles": {"p": {"account": "default", "opus": "fixed", "haiku": "luna-high"}},
        }
    )
    lines = listing_lines(cfg.model_copy(update={"enabled": True}), [], global_origin="/c/g.yaml")
    assert any("luna-high" in x and x.endswith("272000 tokens (up to 1050000)") for x in lines)
    assert any("fixed" in x and x.endswith("100000 tokens") for x in lines)


def test_each_repo_block_shows_its_own_scope_and_settings():
    repo = _view(
        "myrepo",
        "myrepo",
        routes={"sol-high": {"effort": "max"}},
        autostart=True,
    )
    text = "\n".join(
        listing_lines(LiteLLMConfig(enabled=True), [repo], global_origin="/c/global.yaml")
    )
    host_part, repo_part = text.split("repo myrepo  (/c/repos/myrepo.yaml)")
    assert "high (fixed)" in host_part and "max (fixed)" in repo_part
    assert "autostart: on · aliases: jb-myrepo.<profile>.<level>" in repo_part
    assert "jb-myrepo.codex.capable" in repo_part


def test_a_repo_without_own_scope_says_it_uses_the_host_aliases():
    repo = _view("lean", None, default_profile="codex")
    text = "\n".join(
        listing_lines(LiteLLMConfig(enabled=True), [repo], global_origin="/c/global.yaml")
    )
    assert "repo lean" in text
    assert text.split("repo lean")[1].count("aliases: jb.<profile>.<level>") == 1


def test_floor_efforts_and_profile_session_effort_are_shown():
    repo = _view(
        "r",
        "r",
        routes={"luna-high": {"effort": None, "min_effort": "high"}},
        profiles={"codex": {"effort": "low"}},
    )
    text = "\n".join(
        listing_lines(LiteLLMConfig(enabled=True), [repo], global_origin="/c/global.yaml")
    )
    repo_part = text.split("repo r")[1]
    assert ">= high" in repo_part
    assert "session effort low" in repo_part


def test_an_oauth_route_is_marked_as_a_subscription():
    cfg = LiteLLMConfig.model_validate(
        {
            "enabled": True,
            "routes": {"grok": {"model": "xai/grok-4.3", "oauth": True, "context_window": 256_000}},
            "profiles": {"g": {"account": "default", "opus": "grok"}},
        }
    )
    text = "\n".join(listing_lines(cfg, [], global_origin="/c/global.yaml"))
    assert "xai/grok-4.3 (subscription)" in text
