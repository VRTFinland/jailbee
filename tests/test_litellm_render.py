"""Rendering `litellm:` into LiteLLM's config, the callback table and the
per-container file — pure functions, no Incus."""

import json

import pytest
import yaml

from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoOverlay
from jailbee.litellm_render import (
    CATCH_ALL,
    InstanceFiles,
    account_login_providers,
    alias,
    catch_all_route,
    container_key_file,
    container_profiles,
    deployment_id,
    egress_hosts,
    merge_extra,
    render_callback_data,
    render_instance_config,
    render_instance_env,
    render_instance_files,
    served_routes,
    tier_alias,
    upstream_targets,
)


def _by_name(cfg: dict) -> dict[str, dict]:
    return {m["model_name"]: m for m in cfg["model_list"]}


def test_alias_shape():
    assert alias(None, "sol-high") == "jb-default-sol-high"
    assert alias("myrepo", "sol-high") == "jb-myrepo.sol-high"


@pytest.mark.parametrize(
    "a,b",
    [
        (("a-b", "c"), ("a", "b-c")),
        (("default", "sol"), (None, "sol")),
        ((None, "a-b"), ("a", "b")),
    ],
)
def test_aliases_of_different_scopes_never_collide(a, b):
    assert alias(*a) != alias(*b)


def test_tier_alias_names_the_tier_by_role_never_by_claude_family():
    assert tier_alias(None, "codex", "fable") == "jb.codex.most-capable"
    assert tier_alias(None, "codex", "opus") == "jb.codex.capable"
    assert tier_alias(None, "codex", "sonnet") == "jb.codex.standard"
    assert tier_alias("myrepo", "codex", "haiku") == "jb-myrepo.codex.cheap"


@pytest.mark.parametrize("tier", ["fable", "opus", "sonnet", "haiku"])
def test_tier_aliases_carry_no_claude_family_name(tier):
    # Claude Code infers capabilities from `opus`/`haiku`/... in a model name.
    name = tier_alias(None, "p", tier)
    assert not any(family in name for family in ("fable", "opus", "sonnet", "haiku"))


@pytest.mark.parametrize("scope", [None, "default", "a-b"])
def test_tier_aliases_never_meet_route_aliases(scope):
    """Names hold no `.`: a route alias has at most one, a tier alias two."""
    assert alias(scope, "codex-capable").count(".") <= 1
    assert tier_alias(scope, "codex", "opus").count(".") == 2


def test_host_and_repo_tier_aliases_never_meet():
    assert tier_alias(None, "codex", "opus") != tier_alias("default", "codex", "opus")


def test_instance_config_serves_every_route_every_profile_tier_and_the_catch_all():
    rendered = render_instance_config(LiteLLMConfig(), "default")
    models = _by_name(rendered)
    assert set(models) == {
        "jb-default-astra",
        "jb-default-sol-high",
        "jb-default-sol-medium",
        "jb-default-luna-high",
        "jb.codex.most-capable",
        "jb.codex.capable",
        "jb.codex.standard",
        "jb.codex.cheap",
        CATCH_ALL,
    }
    assert models["jb.codex.capable"]["litellm_params"] == {"model": "chatgpt/gpt-6.1-sol"}
    capable = {k: v for k, v in models["jb.codex.capable"]["model_info"].items() if k != "id"}
    sol_info = {k: v for k, v in models["jb-default-sol-high"]["model_info"].items() if k != "id"}
    assert capable == sol_info
    sol = models["jb-default-sol-high"]
    assert sol["litellm_params"] == {"model": "chatgpt/gpt-6.1-sol"}
    assert sol["model_info"] == {
        "mode": "responses",
        "max_input_tokens": 272_000,
        "id": deployment_id("jb-default-sol-high"),
    }


def test_effort_is_not_put_in_litellm_params():
    """Claude Code sends its own effort; the callback applies configured effort."""
    models = _by_name(render_instance_config(LiteLLMConfig(), "default"))
    assert "reasoning_effort" not in models["jb-default-sol-high"]["litellm_params"]


def test_route_params_pass_through():
    cfg = LiteLLMConfig.model_validate({"routes": {"astra": {"params": {"timeout": 600}}}})
    models = _by_name(render_instance_config(cfg, "default"))
    assert models["jb-default-astra"]["litellm_params"] == {
        "model": "chatgpt/gpt-6-astra",
        "timeout": 600,
    }


def test_catch_all_targets_default_profiles_haiku_route():
    models = _by_name(render_instance_config(LiteLLMConfig(), "default"))
    assert models[CATCH_ALL]["litellm_params"] == {"model": "chatgpt/gpt-6-luna"}


def test_catch_all_falls_back_to_sonnet_when_haiku_unmapped():
    cfg = LiteLLMConfig.model_validate({"profiles": {"codex": {"haiku": None}}})
    models = _by_name(render_instance_config(cfg, "default"))
    assert models[CATCH_ALL]["litellm_params"] == {"model": "chatgpt/gpt-6.1-sol"}


def test_proxy_settings_always_set():
    rendered = render_instance_config(LiteLLMConfig(), "default")
    assert rendered["litellm_settings"] == {
        "drop_params": True,
        "turn_off_message_logging": True,
        "callbacks": ["jailbee_callback.proxy_handler_instance"],
    }
    assert rendered["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY"}


def test_callback_data_marks_chatgpt_and_efforts():
    data = render_callback_data(LiteLLMConfig(), "default")
    assert data["aliases"]["jb-default-sol-high"] == {
        "chatgpt": True,
        "effort": "high",
        "min_effort": None,
    }
    assert data["aliases"]["jb-default-astra"] == {
        "chatgpt": True,
        "effort": "high",
        "min_effort": None,
    }
    assert data["catch_all"] == {"chatgpt": True, "effort": "high", "min_effort": None}


def test_callback_data_keeps_effort_floor_for_overridden_route():
    cfg = LiteLLMConfig.model_validate(
        {"routes": {"luna-high": {"effort": None, "min_effort": "medium"}}}
    )
    data = render_callback_data(cfg, "default")
    assert data["aliases"]["jb-default-luna-high"] == {
        "chatgpt": True,
        "effort": None,
        "min_effort": "medium",
    }
    assert data["catch_all"] == {"chatgpt": True, "effort": None, "min_effort": "medium"}


def test_instance_env():
    env = render_instance_env(port=4100, master_key="sk-jb-x", account="default")
    assert env.splitlines() == [
        "PORT=4100",
        "LITELLM_MASTER_KEY=sk-jb-x",
        "CHATGPT_TOKEN_DIR=/var/lib/jailbee-litellm/default/auth",
        "JAILBEE_LITELLM_HOT_FILE=/var/lib/jailbee-litellm/default/hot.json",
        "JAILBEE_LITELLM_ACK_FILE=/var/lib/jailbee-litellm/default/applied.json",
        "LITELLM_LOCAL_MODEL_COST_MAP=True",
    ]
    assert env.endswith("\n")


def test_container_profiles():
    profiles = container_profiles(LiteLLMConfig(), base_urls={"default": "http://10.0.0.3:4100"})
    assert profiles == {
        "codex": {
            "base_url": "http://10.0.0.3:4100",
            "key_file": "/etc/jailbee/litellm-default.key",
            "effort": None,
            "instructions": None,
            "tiers": {
                "fable": "jb.codex.most-capable",
                "opus": "jb.codex.capable",
                "sonnet": "jb.codex.standard",
                "haiku": "jb.codex.cheap",
            },
            "context_window": 272_000,
        }
    }


def test_profile_context_window_is_the_smallest_of_the_profiles_routes():
    cfg = LiteLLMConfig.model_validate(
        {
            "routes": {"luna-high": {"context_window": 1_050_000}},
            "profiles": {"wide": {"account": "default", "haiku": "luna-high"}},
        }
    )
    profiles = container_profiles(cfg, base_urls={"default": "u"})
    assert profiles["wide"]["context_window"] == 1_050_000
    assert profiles["codex"]["context_window"] == 272_000


def test_container_profiles_carry_the_profile_instructions():
    cfg = LiteLLMConfig.model_validate(
        {"profiles": {"codex": {"instructions": "Use sonnet for edits."}}}
    )
    profiles = container_profiles(cfg, base_urls={"default": "u"})
    assert profiles["codex"]["instructions"] == "Use sonnet for edits."


def test_egress_hosts_for_chatgpt():
    assert egress_hosts(LiteLLMConfig()) == ["auth.openai.com:443", "chatgpt.com:443"]


_KIMI = {
    "model": "openrouter/moonshotai/kimi-k3",
    "context_window": 262144,
    "api_key": "OPENROUTER_API_KEY",
}


def _two_accounts() -> LiteLLMConfig:
    """codex on `personal`; `work` maps Opus to its own low-effort Sol route; kimi everywhere."""
    return LiteLLMConfig.model_validate(
        {
            "accounts": ["personal", "work"],
            "default_profile": "codex",
            "routes": {"kimi": _KIMI, "sol-low": {"model": "chatgpt/gpt-6.1-sol", "effort": "low"}},
            "profiles": {
                "codex": {"account": "personal"},
                "work": {"account": "work", "opus": "sol-low", "haiku": "kimi"},
                "kimi": {"opus": "kimi", "sonnet": "kimi", "haiku": "kimi"},
            },
        }
    )


def test_subscription_routes_are_served_by_their_accounts_and_api_key_routes_everywhere():
    cfg = _two_accounts()
    assert set(served_routes(cfg, "personal")) == {
        "astra",
        "sol-high",
        "sol-medium",
        "luna-high",
        "kimi",
    }
    assert set(served_routes(cfg, "work")) == {"sol-low", "kimi"}


def test_an_unreferenced_subscription_route_is_served_nowhere():
    cfg = LiteLLMConfig.model_validate({"routes": {"spare": {"model": "chatgpt/gpt-6-luna"}}})
    assert "spare" not in served_routes(cfg, "default")


def test_api_key_is_rendered_as_an_env_reference_never_a_value():
    cfg = LiteLLMConfig.model_validate(
        {"routes": {"kimi": {**_KIMI, "api_base": "https://openrouter.ai/api/v1"}}}
    )
    kimi = _by_name(render_instance_config(cfg, "default"))["jb-default-kimi"]
    assert kimi["litellm_params"] == {
        "model": "openrouter/moonshotai/kimi-k3",
        "api_key": "os.environ/OPENROUTER_API_KEY",
        "api_base": "https://openrouter.ai/api/v1",
    }
    # no Responses mode
    assert kimi["model_info"] == {
        "max_input_tokens": 262144,
        "id": deployment_id("jb-default-kimi"),
    }


def test_catch_all_is_per_account():
    cfg = _two_accounts()
    assert catch_all_route(cfg, "personal").name == "luna-high"  # default profile's haiku
    assert catch_all_route(cfg, "work").name == "kimi"  # first profile bound to work: its haiku


def test_an_api_key_default_profile_is_every_instances_catch_all():
    cfg = _two_accounts().model_copy(update={"default_profile": "kimi"})
    assert catch_all_route(cfg, "personal").name == "kimi"
    assert catch_all_route(cfg, "work").name == "kimi"


def test_an_account_no_profile_uses_has_no_catch_all():
    cfg = LiteLLMConfig.model_validate({"accounts": ["default", "spare"]})
    assert catch_all_route(cfg, "spare") is None
    assert CATCH_ALL not in _by_name(render_instance_config(cfg, "spare"))
    assert render_callback_data(cfg, "spare") == {"aliases": {}, "catch_all": None}


def test_callback_data_covers_only_the_served_aliases():
    data = render_callback_data(_two_accounts(), "work")
    assert set(data["aliases"]) == {
        "jb-default-sol-low",
        "jb-default-kimi",
        "jb.work.capable",
        "jb.work.cheap",
        "jb.kimi.capable",
        "jb.kimi.standard",
        "jb.kimi.cheap",
    }
    assert data["aliases"]["jb.work.capable"] == data["aliases"]["jb-default-sol-low"]
    assert data["aliases"]["jb-default-kimi"]["chatgpt"] is False
    assert data["aliases"]["jb-default-sol-low"] == {
        "chatgpt": True,
        "effort": "low",
        "min_effort": None,
    }


def test_instance_env_carries_referenced_secrets_single_quoted_and_sorted():
    env = render_instance_env(
        port=4101,
        master_key="sk-jb-x",
        account="work",
        secrets={"XAI_API_KEY": "xai-2", "OPENROUTER_API_KEY": "sk-or-1"},
    )
    lines = env.splitlines()
    assert lines[:2] == ["PORT=4101", "LITELLM_MASTER_KEY=sk-jb-x"]
    assert "CHATGPT_TOKEN_DIR=/var/lib/jailbee-litellm/work/auth" in lines
    assert lines[-2:] == ["OPENROUTER_API_KEY='sk-or-1'", "XAI_API_KEY='xai-2'"]


def test_instance_env_refuses_a_value_it_cannot_quote():
    with pytest.raises(ValueError):
        render_instance_env(port=1, master_key="k", account="a", secrets={"K": "it's"})


def test_extra_merges_last_with_lists_appended_and_scalars_winning():
    extra = {
        "model_list": [{"model_name": "mine", "litellm_params": {"model": "mistral/large"}}],
        "litellm_settings": {"callbacks": ["my.handler"], "drop_params": False},
        "router_settings": {"num_retries": 2},
    }
    rendered = render_instance_config(LiteLLMConfig(), "default", extra=extra)
    assert [m["model_name"] for m in rendered["model_list"]][-1] == "mine"
    assert rendered["litellm_settings"]["callbacks"] == [
        "jailbee_callback.proxy_handler_instance",
        "my.handler",
    ]
    assert rendered["litellm_settings"]["drop_params"] is False
    assert rendered["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY"}
    assert rendered["router_settings"] == {"num_retries": 2}


def test_merge_extra_does_not_mutate_its_inputs():
    base = {"a": {"b": [1]}}
    merge_extra(base, {"a": {"b": [2]}})
    assert base == {"a": {"b": [1]}}


def test_instance_files_digest_changes_with_every_part():
    files = render_instance_files(LiteLLMConfig(), "default", port=4100, master_key="k1")
    base = files.digest("callback v1")
    assert files.digest("callback v1") == base
    assert files.digest("callback v2") != base
    rekeyed = render_instance_files(LiteLLMConfig(), "default", port=4100, master_key="k2")
    assert rekeyed.digest("callback v1") != base
    secret = render_instance_files(
        LiteLLMConfig(), "default", port=4100, master_key="k1", secrets={"K": "v"}
    )
    assert secret.digest("callback v1") != base
    assert isinstance(files, InstanceFiles) and files.account == "default"


def test_container_profiles_point_each_profile_at_its_account():
    cfg = _two_accounts()
    profiles = container_profiles(
        cfg, base_urls={"personal": "http://10.0.0.3:4100", "work": "http://10.0.0.3:4101"}
    )
    assert profiles["codex"]["base_url"] == "http://10.0.0.3:4100"
    assert profiles["codex"]["key_file"] == container_key_file("personal")
    assert profiles["work"]["base_url"] == "http://10.0.0.3:4101"
    assert profiles["kimi"]["base_url"] == "http://10.0.0.3:4100"  # accounts[0]
    assert container_key_file("work") == "/etc/jailbee/litellm-work.key"


def test_container_profiles_leave_out_profiles_without_an_instance():
    profiles = container_profiles(_two_accounts(), base_urls={"personal": "http://10.0.0.3:4100"})
    assert set(profiles) == {"codex", "kimi"}


def test_egress_is_derived_from_served_routes_api_base_and_host_egress():
    cfg = LiteLLMConfig.model_validate(
        {
            "routes": {
                "kimi": _KIMI,
                "local": {
                    "model": "openai/qwen",
                    "context_window": 32768,
                    "api_base": "https://llm.example.com:8443/v1",
                    "egress": ["extra.example.com"],
                },
            },
            "egress": ["10.0.0.5:11434"],
        }
    )
    assert egress_hosts(cfg) == sorted(
        [
            "10.0.0.5:11434",
            "auth.openai.com:443",
            "chatgpt.com:443",
            "extra.example.com",
            "llm.example.com:8443",  # api_base replaces api.openai.com
            "openrouter.ai:443",
        ]
    )
    assert ("extra.example.com", 443) in upstream_targets(cfg)
    assert ("llm.example.com", 8443) in upstream_targets(cfg)


def test_upstream_targets_skip_cidrs():
    cfg = LiteLLMConfig.model_validate({"egress": ["10.0.0.0/24"]})
    assert all(host != "10.0.0.0/24" for host, _ in upstream_targets(cfg))


def _scope(**overlay: object) -> LiteLLMConfig:
    return LiteLLMConfig().with_overlay(LiteLLMRepoOverlay.model_validate(overlay))


def test_every_scope_is_rendered_beside_the_host_routes():
    scopes = {"myrepo": _scope(routes={"sol-high": {"effort": "max"}})}
    models = _by_name(render_instance_config(LiteLLMConfig(), "default", scopes=scopes))
    assert "jb-default-sol-high" in models and "jb-myrepo.sol-high" in models
    assert "jb-myrepo.astra" in models
    table = render_callback_data(LiteLLMConfig(), "default", scopes=scopes)["aliases"]
    assert table["jb-myrepo.sol-high"]["effort"] == "max"
    assert table["jb-default-sol-high"]["effort"] == "high"


def test_the_catch_all_stays_the_hosts():
    scopes = {"myrepo": _scope(routes={"luna-high": {"model": "chatgpt/gpt-6.1-sol"}})}
    models = _by_name(render_instance_config(LiteLLMConfig(), "default", scopes=scopes))
    assert models[CATCH_ALL]["litellm_params"]["model"] == "chatgpt/gpt-6-luna"


def test_a_scope_serves_its_subscription_routes_only_on_its_profiles_account():
    host = LiteLLMConfig.model_validate({"accounts": ["default", "work"]})
    scopes = {
        "myrepo": host.with_overlay(
            LiteLLMRepoOverlay.model_validate({"profiles": {"codex": {"account": "work"}}})
        )
    }
    on_work = _by_name(render_instance_config(host, "work", scopes=scopes))
    on_default = _by_name(render_instance_config(host, "default", scopes=scopes))
    assert "jb-myrepo.sol-high" in on_work and "jb-myrepo.sol-high" not in on_default
    assert "jb-default-sol-high" in on_default and "jb-default-sol-high" not in on_work


def test_container_profiles_use_the_scope_tier_aliases():
    cfg = _scope(routes={"sol-high": {"effort": "max"}})
    profiles = container_profiles(cfg, base_urls={"default": "u"}, scope="myrepo")
    assert profiles["codex"]["tiers"]["opus"] == "jb-myrepo.codex.capable"
    host = container_profiles(LiteLLMConfig(), base_urls={"default": "u"})
    assert host["codex"]["tiers"]["opus"] == "jb.codex.capable"


def test_egress_and_probe_targets_include_every_scope():
    scopes = {
        "myrepo": _scope(
            routes={
                "kimi": {
                    "model": "openrouter/moonshotai/kimi-k3",
                    "context_window": 262144,
                    "api_key": "OPENROUTER_API_KEY",
                }
            }
        )
    }
    assert "openrouter.ai:443" in egress_hosts(LiteLLMConfig(), scopes=scopes)
    assert "openrouter.ai:443" not in egress_hosts(LiteLLMConfig())
    assert ("openrouter.ai", 443) in upstream_targets(LiteLLMConfig(), scopes=scopes)


def test_instance_files_digest_changes_when_a_scope_is_added():
    base = render_instance_files(LiteLLMConfig(), "default", port=4100, master_key="k")
    scoped = render_instance_files(
        LiteLLMConfig(),
        "default",
        port=4100,
        master_key="k",
        scopes={"myrepo": _scope(routes={"sol-high": {"effort": "max"}})},
    )
    # A scope only adds routes, which are hot.
    assert base.hot_digest() != scoped.hot_digest()
    assert base.digest("cb") == scoped.digest("cb")


def _api_route(model: str, effort: str) -> dict[str, object]:
    return {"model": model, "context_window": 200000, "effort": effort, "api_key": "K"}


def test_scopes_whose_prefix_and_route_names_interleave_stay_apart_on_one_instance():
    scopes = {
        "a-b": _scope(routes={"c": _api_route("openrouter/m-ab-c", "low")}),
        "a": _scope(routes={"b-c": _api_route("openrouter/m-a-bc", "high")}),
        "default": _scope(
            routes={
                "sol-high": {
                    "model": "chatgpt/gpt-6-other",
                    "context_window": 200000,
                    "effort": "max",
                }
            }
        ),
    }
    host = LiteLLMConfig()
    rendered = render_instance_config(host, "default", scopes=scopes)
    names = [m["model_name"] for m in rendered["model_list"]]
    assert len(names) == len(set(names))
    models = _by_name(rendered)
    assert models["jb-a-b.c"]["litellm_params"]["model"] == "openrouter/m-ab-c"
    assert models["jb-a.b-c"]["litellm_params"]["model"] == "openrouter/m-a-bc"
    assert models["jb-default.sol-high"]["litellm_params"]["model"] == "chatgpt/gpt-6-other"
    assert models["jb-default-sol-high"]["litellm_params"]["model"] == "chatgpt/gpt-6.1-sol"

    table = render_callback_data(host, "default", scopes=scopes)["aliases"]
    for name in ("jb-a-b.c", "jb-a.b-c", "jb-default.sol-high", "jb-default-sol-high"):
        assert name in table
    assert table["jb-a-b.c"]["effort"] == "low"
    assert table["jb-a.b-c"]["effort"] == "high"
    assert table["jb-default.sol-high"]["effort"] == "max"
    assert table["jb-default-sol-high"]["effort"] == "high"
    assert set(table) == set(names) - {CATCH_ALL}


def test_a_profile_tier_is_served_only_where_its_route_is():
    personal = _by_name(render_instance_config(_two_accounts(), "personal"))
    work = _by_name(render_instance_config(_two_accounts(), "work"))
    assert "jb.codex.capable" in personal and "jb.codex.capable" not in work
    assert "jb.work.capable" in work and "jb.work.capable" not in personal
    # API-key tiers are served by every instance, as their routes are.
    assert "jb.kimi.capable" in personal and "jb.kimi.capable" in work


def test_a_renamed_route_keeps_the_tier_alias_a_running_session_holds():
    """The regression: renaming a route in `global.yaml` broke every session
    started before, because the session held the route's alias."""
    before = LiteLLMConfig()
    after = LiteLLMConfig.model_validate(
        {
            "routes": {"sol-max": {"model": "chatgpt/gpt-6.1-sol", "effort": "max"}},
            "profiles": {"codex": {"opus": "sol-max"}},
        }
    )
    held = container_profiles(before, base_urls={"default": "u"})["codex"]["tiers"]["opus"]
    models = _by_name(render_instance_config(after, "default"))
    assert models[held]["litellm_params"] == {"model": "chatgpt/gpt-6.1-sol"}
    assert render_callback_data(after, "default")["aliases"][held]["effort"] == "max"


def test_a_repo_scope_serves_its_profile_tiers_under_its_prefix():
    scopes = {"myrepo": _scope(routes={"sol-high": {"effort": "max"}})}
    table = render_callback_data(LiteLLMConfig(), "default", scopes=scopes)["aliases"]
    assert table["jb-myrepo.codex.capable"]["effort"] == "max"
    assert table["jb.codex.capable"]["effort"] == "high"


def _files(**kw):
    return render_instance_files(
        kw.pop("cfg", LiteLLMConfig()), "default", port=4100, master_key="k", **kw
    )


def test_every_deployment_has_a_stable_unique_id():
    models = json.loads(_files().hot_json)["models"]
    ids = [m["model_info"]["id"] for m in models]
    assert len(ids) == len(set(ids)) and all(i.startswith("jb:") for i in ids)
    assert json.loads(_files().hot_json)["models"] == models  # stable across renders


def test_extra_deployments_get_an_id_from_their_content():
    def entry(model: str) -> dict:
        return {"model_name": "mine", "litellm_params": {"model": model}}

    def ids(extra):
        models = json.loads(_files(extra=extra).hot_json)["models"]
        return [m["model_info"]["id"] for m in models if m["model_name"] == "mine"]

    first = ids({"model_list": [entry("openai/a")]})
    assert first == ids({"model_list": [entry("openai/a")]})
    assert first != ids({"model_list": [entry("openai/b")]})
    keep = {**entry("openai/a"), "model_info": {"id": "mine-1"}}
    assert ids({"model_list": [keep]}) == ["mine-1"]


def test_hot_json_carries_the_callback_table_and_the_model_list():
    files = _files()
    hot = json.loads(files.hot_json)
    assert hot["callback"] == render_callback_data(LiteLLMConfig(), "default")
    assert hot["models"] == yaml.safe_load(files.config_yaml)["model_list"]
    assert "model_list" not in yaml.safe_load(files.settings_yaml)


def test_digest_ignores_routes_and_effort_but_not_what_the_proxy_reads_at_start():
    base = _files()
    maxed = _files(cfg=LiteLLMConfig.model_validate({"routes": {"sol-high": {"effort": "max"}}}))
    assert maxed.digest("cb") == base.digest("cb")
    assert maxed.hot_digest() != base.hot_digest()
    settings = _files(extra={"router_settings": {"num_retries": 2}})
    assert settings.digest("cb") != base.digest("cb")
    assert settings.hot_digest() == base.hot_digest()
    secret = _files(secrets={"K": "v"})
    assert secret.digest("cb") != base.digest("cb") and secret.hot_digest() == base.hot_digest()
    assert base.digest("cb v2") != base.digest("cb")


_KEYS_ONLY = {
    "accounts": ["default", "keys"],
    "routes": {
        "kimi": {
            "model": "openrouter/moonshotai/kimi-k3",
            "context_window": 262144,
            "api_key": "OPENROUTER_API_KEY",
        }
    },
    "profiles": {"kimi": {"account": "keys", "opus": "kimi"}},
}


def test_login_providers_follow_the_routes_an_account_serves():
    cfg = LiteLLMConfig.model_validate(_KEYS_ONLY)
    assert account_login_providers(cfg, "default") == ("chatgpt",)
    assert account_login_providers(cfg, "keys") == ()
    files = render_instance_files(cfg, "keys", port=4101, master_key="k")
    assert files.login_providers == ()
    assert render_instance_files(cfg, "default", port=4100, master_key="k").login_providers == (
        "chatgpt",
    )


def test_a_repo_scope_can_make_an_account_need_a_login():
    host = LiteLLMConfig.model_validate(_KEYS_ONLY)
    scoped = host.with_overlay(
        LiteLLMRepoOverlay.model_validate({"profiles": {"kimi": {"opus": "luna-high"}}})
    )
    assert account_login_providers(host, "keys", {"myrepo": scoped}) == ("chatgpt",)


_GROK_CFG = {
    "routes": {"grok": {"model": "xai/grok-4.3", "oauth": True, "context_window": 256_000}},
    "profiles": {"grok": {"account": "default", "opus": "grok", "haiku": "grok"}},
}


def test_an_oauth_deployment_uses_the_subscription_login():
    models = _by_name(render_instance_config(LiteLLMConfig.model_validate(_GROK_CFG), "default"))
    grok = models["jb-default-grok"]
    assert grok["litellm_params"] == {"model": "xai/grok-4.3", "use_xai_oauth": True}
    assert "mode" not in grok["model_info"]  # `mode: responses` is the ChatGPT backend's


def test_the_callback_does_not_treat_an_oauth_route_as_chatgpt():
    data = render_callback_data(LiteLLMConfig.model_validate(_GROK_CFG), "default")
    assert data["aliases"]["jb-default-grok"]["chatgpt"] is False
    assert data["aliases"]["jb-default-sol-high"]["chatgpt"] is True


def test_oauth_routes_open_the_xai_auth_host():
    hosts = egress_hosts(LiteLLMConfig.model_validate(_GROK_CFG))
    assert "auth.x.ai:443" in hosts and "api.x.ai:443" in hosts


def test_an_account_without_oauth_routes_renders_todays_env():
    files = render_instance_files(LiteLLMConfig(), "default", port=4100, master_key="k")
    assert "XAI_" not in files.instance_env
    assert files.instance_env == render_instance_env(port=4100, master_key="k", account="default")
    assert files.login_providers == ("chatgpt",)


def test_an_account_serving_an_oauth_route_gets_the_xai_token_dir():
    files = render_instance_files(
        LiteLLMConfig.model_validate(_GROK_CFG), "default", port=4100, master_key="k"
    )
    assert "XAI_OAUTH_TOKEN_DIR=/var/lib/jailbee-litellm/default/xai-auth\n" in files.instance_env
    assert files.login_providers == ("chatgpt", "xai")


def test_a_mixed_account_needs_both_logins_and_both_token_dirs():
    cfg = LiteLLMConfig.model_validate(
        {
            **_GROK_CFG,
            "profiles": {"mix": {"account": "default", "opus": "grok", "haiku": "luna-high"}},
        }
    )
    assert account_login_providers(cfg, "default") == ("chatgpt", "xai")
    env = render_instance_files(cfg, "default", port=4100, master_key="k").instance_env
    assert "CHATGPT_TOKEN_DIR=" in env and "XAI_OAUTH_TOKEN_DIR=" in env


def test_an_api_key_only_account_needs_no_login():
    cfg = LiteLLMConfig.model_validate(
        {
            "accounts": ["default", "keys"],
            "routes": {
                "kimi": {
                    "model": "openrouter/moonshotai/kimi-k3",
                    "context_window": 262144,
                    "api_key": "OPENROUTER_API_KEY",
                }
            },
            "profiles": {"kimi": {"account": "keys", "opus": "kimi"}},
        }
    )
    assert account_login_providers(cfg, "keys") == ()


def test_an_oauth_route_in_a_repo_scope_needs_the_xai_login():
    host = LiteLLMConfig()
    scoped = host.with_overlay(LiteLLMRepoOverlay.model_validate(_GROK_CFG))
    assert account_login_providers(host, "default") == ("chatgpt",)
    assert account_login_providers(host, "default", {"myrepo": scoped}) == ("chatgpt", "xai")
    files = render_instance_files(
        host, "default", port=4100, master_key="k", scopes={"myrepo": scoped}
    )
    assert "XAI_OAUTH_TOKEN_DIR=" in files.instance_env
    assert "auth.x.ai:443" in egress_hosts(host, scopes={"myrepo": scoped})
