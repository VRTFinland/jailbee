"""`litellm:` config → the files the LiteLLM container and `claude-jb` read.

Pure: no Incus, no filesystem. `litellm` pushes what this returns.

One LiteLLM process per account (`CHATGPT_TOKEN_DIR` is process-wide), so
everything here is rendered per account. An instance serves every API-key
route, plus the subscription routes of the profiles bound to its account.

Effort never goes into a deployment's `litellm_params`: the 2026-09-29 spike
showed a deployment's `reasoning_effort` is only a default, and Claude Code
always sends its own session effort (`output_config.effort`), so the value
would never apply. The jailbee callback (`provision/litellm/jailbee_callback.py`)
applies fixed and floor efforts from `render_callback_data`'s table instead.

Every instance renders every **scope**: the host's own routes under
`jb-default-<route>`, and each repo override that changes routes or profiles
under `jb-<prefix>.<route>`. Repos with different overrides then share one
instance, and one login, without answering each other's model names. The
catch-all stays the host's.

`claude-jb` hands Claude Code **tier aliases**, not route aliases:
`jb.<profile>.<level>` (`jb-<prefix>.<profile>.<level>` in a repo scope), each
served by whatever route the profile maps that tier to right now. A running
session holds the model names it started with, so a route renamed, dropped or
remapped in `global.yaml` must not take a name away from it. The level names a
tier by role, never by Claude family: Claude Code reads `opus`, `haiku` and the
like out of a model name and changes what it sends. Route aliases stay served
for `/model` and for sessions started before tier aliases existed.

Secrets appear in the rendered config only as `os.environ/<NAME>`; their
values live in the per-instance `instance.env`.

`hot.json` is what the callback re-reads while the proxy runs (alias table,
`model_list`, and the names of the secrets `model_list` uses, which it then
re-reads from `instance.env`); everything else is read at start and needs a
restart.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from jailbee.config.models_litellm import PROVIDER_HOSTS, XAI_AUTH_HOST, api_base_endpoint
from jailbee.egress import parse_egress_entry

if TYPE_CHECKING:
    from jailbee.config.models_litellm import LiteLLMConfig, ResolvedProfile, ResolvedRoute

CATCH_ALL = "claude-*"
CONTAINER_STATE_DIR = "/var/lib/jailbee-litellm"
HOT_FILE = "hot.json"
ACK_FILE = "applied.json"
ENV_FILE = "instance.env"
_ENV_PREFIX = "os.environ/"
_CHEAPEST_FIRST = ("haiku", "sonnet", "opus", "fable")
TIER_LEVELS: Mapping[str, str] = {
    "fable": "most-capable",
    "opus": "capable",
    "sonnet": "standard",
    "haiku": "cheap",
}

Scopes = Mapping[str, "LiteLLMConfig"]
"""Repo prefix → that repo's merged config, for repos served under their own aliases."""


def alias(scope: str | None, route: str) -> str:
    """`jb-default-<route>` for the host's routes, `jb-<prefix>.<route>` for a repo's.

    Neither a route name nor a prefix may contain `.`, so the two forms never meet.
    """
    return f"jb-default-{route}" if scope is None else f"jb-{scope}.{route}"


def level_alias(scope: str | None, profile: str, level: str) -> str:
    """`jb.<profile>.<level>` for the host's profiles, `jb-<prefix>.<profile>.<level>` for a repo's.

    Profile names hold no `.`, so a tier alias holds exactly two and a route
    alias at most one; `jb.` and `jb-<prefix>.` keep the scopes apart.
    """
    return f"jb{'' if scope is None else f'-{scope}'}.{profile}.{level}"


def tier_alias(scope: str | None, profile: str, tier: str) -> str:
    return level_alias(scope, profile, TIER_LEVELS[tier])


def _scoped(
    cfg: LiteLLMConfig, scopes: Scopes | None
) -> Iterator[tuple[str | None, LiteLLMConfig]]:
    yield None, cfg
    for prefix in sorted(scopes or {}):
        yield prefix, (scopes or {})[prefix]


def container_key_file(account: str) -> str:
    return f"/etc/jailbee/litellm-{account}.key"


def served_routes(cfg: LiteLLMConfig, account: str) -> dict[str, ResolvedRoute]:
    routes = cfg.effective_routes()
    bound = {
        route
        for profile in cfg.effective_profiles().values()
        if cfg.instance_account(profile) == account
        for route in profile.tiers.values()
    }
    return {n: r for n, r in routes.items() if not r.subscription or n in bound}


def account_login_providers(
    cfg: LiteLLMConfig, account: str, scopes: Scopes | None = None
) -> tuple[str, ...]:
    """The logins (`chatgpt`, `xai`) the account's instance needs, over every scope."""
    return tuple(
        sorted(
            {
                route.login_provider
                for _, view in _scoped(cfg, scopes)
                for route in served_routes(view, account).values()
                if route.login_provider is not None
            }
        )
    )


def _served_aliases(
    cfg: LiteLLMConfig, account: str, scopes: Scopes | None
) -> Iterator[tuple[str, ResolvedRoute]]:
    """Every model name this instance answers but the catch-all, with its route.

    A tier alias is served wherever its route is, so an API-key tier answers on
    every instance, like its route alias.
    """
    for scope, view in _scoped(cfg, scopes):
        served = served_routes(view, account)
        for name, route in served.items():
            yield alias(scope, name), route
        for profile in view.effective_profiles().values():
            for tier, name in profile.tiers.items():
                if name in served:
                    yield tier_alias(scope, profile.name, tier), served[name]


def _cheapest(
    profile: ResolvedProfile, routes: Mapping[str, ResolvedRoute]
) -> ResolvedRoute | None:
    for tier in _CHEAPEST_FIRST:
        if tier in profile.tiers:
            return routes[profile.tiers[tier]]
    return None


def catch_all_route(cfg: LiteLLMConfig, account: str) -> ResolvedRoute | None:
    """Hard-coded Claude model IDs are background work: the cheapest mapped tier.

    The default profile's, when this instance serves all of it; otherwise the
    first profile (by name) bound to this account; None if no profile is.
    """
    served = served_routes(cfg, account)
    profiles = cfg.effective_profiles()
    default = profiles[cfg.default_profile]
    if all(route in served for route in default.tiers.values()):
        return _cheapest(default, served)
    for name in sorted(profiles):
        if cfg.instance_account(profiles[name]) == account:
            return _cheapest(profiles[name], served)
    return None


def deployment_id(model_name: str) -> str:
    """The stable id the hot reload addresses a deployment by (`upsert`/`delete` key on it)."""
    return f"jb:{model_name}"


def _deployment(model_name: str, route: ResolvedRoute) -> dict[str, object]:
    params: dict[str, object] = {"model": route.model, **route.params}
    if route.api_key:
        params["api_key"] = f"os.environ/{route.api_key}"
    if route.api_base:
        params["api_base"] = route.api_base
    if route.oauth:
        params["use_xai_oauth"] = True
    info: dict[str, object] = {
        "id": deployment_id(model_name),
        "max_input_tokens": route.context_window,
    }
    if route.chatgpt:
        info = {"mode": "responses", **info}
    return {"model_name": model_name, "litellm_params": params, "model_info": info}


def merge_extra(base: dict[str, object], extra: Mapping[str, object]) -> dict[str, object]:
    """Deep-merge `extra` into a copy of `base`: mappings recurse, lists append, scalars win.

    `litellm_inputs.check_extra` has already refused a fragment whose scalar
    would replace one of jailbee's mappings or lists.
    """
    out = dict(base)
    for key, value in extra.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = merge_extra(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            out[key] = [*current, *value]
        else:
            out[key] = value
    return out


def _with_ids(rendered: dict[str, object]) -> dict[str, object]:
    """Give every `extra` deployment the `model_info.id` the hot reload needs.

    Derived from the entry's own content, so it is identical across renders and
    changes when the entry does (the reload then adds the new one and deletes the old).
    """
    models = rendered.get("model_list")
    if not isinstance(models, list):
        return rendered
    out: list[object] = []
    for entry in models:
        if isinstance(entry, dict):
            info = entry.get("model_info")
            if info is None or (isinstance(info, dict) and "id" not in info):
                blob = json.dumps(entry, sort_keys=True, default=str).encode()
                name = entry.get("model_name", "?")
                digest = hashlib.sha256(blob).hexdigest()[:10]
                entry = {**entry, "model_info": {**(info or {}), "id": f"jb-extra:{name}:{digest}"}}
        out.append(entry)
    return {**rendered, "model_list": out}


def env_references(value: object) -> set[str]:
    """Every `NAME` of an `os.environ/NAME` string anywhere in `value`."""
    if isinstance(value, str):
        return {value.removeprefix(_ENV_PREFIX)} if value.startswith(_ENV_PREFIX) else set()
    if isinstance(value, Mapping):
        return {n for v in value.values() for n in env_references(v)}
    if isinstance(value, list):
        return {n for v in value for n in env_references(v)}
    return set()


def _values_digest(values: Mapping[str, str]) -> str:
    """Changes when any value does; hot.json carries this, never a value."""
    return hashlib.sha256(json.dumps(dict(values), sort_keys=True).encode()).hexdigest()


def render_instance_config(
    cfg: LiteLLMConfig,
    account: str,
    *,
    extra: Mapping[str, object] | None = None,
    scopes: Scopes | None = None,
) -> dict[str, object]:
    model_list = [_deployment(name, route) for name, route in _served_aliases(cfg, account, scopes)]
    catch_all = catch_all_route(cfg, account)
    if catch_all is not None:
        model_list.append(_deployment(CATCH_ALL, catch_all))
    rendered: dict[str, object] = {
        "model_list": model_list,
        "litellm_settings": {
            "drop_params": True,
            "turn_off_message_logging": True,
            "callbacks": ["jailbee_callback.proxy_handler_instance"],
        },
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }
    return _with_ids(merge_extra(rendered, extra) if extra else rendered)


def _entry(route: ResolvedRoute) -> dict[str, object]:
    # The callback's key stays `chatgpt`: it flattens `system` for that backend only.
    return {"chatgpt": route.chatgpt, "effort": route.effort, "min_effort": route.min_effort}


def render_callback_data(
    cfg: LiteLLMConfig, account: str, *, scopes: Scopes | None = None
) -> dict[str, object]:
    catch_all = catch_all_route(cfg, account)
    return {
        "aliases": {name: _entry(route) for name, route in _served_aliases(cfg, account, scopes)},
        "catch_all": None if catch_all is None else _entry(catch_all),
    }


def render_instance_env(
    *,
    port: int,
    master_key: str,
    account: str,
    secrets: Mapping[str, str] | None = None,
) -> str:
    """systemd `EnvironmentFile` that `jailbee litellm login` also sources with bash.

    Single quotes mean the same thing to both parsers only without a quote,
    backslash or newline inside; `litellm_inputs` refuses such values first.

    `XAI_OAUTH_TOKEN_DIR` is set whether or not the account serves an `oauth`
    route, so its first one is a reload, not a restart. The callback re-reads
    secrets from this file (`JAILBEE_LITELLM_ENV_FILE`) on a reload.
    """
    base = f"{CONTAINER_STATE_DIR}/{account}"
    lines = [
        f"PORT={port}",
        f"LITELLM_MASTER_KEY={master_key}",
        f"CHATGPT_TOKEN_DIR={base}/auth",
        f"XAI_OAUTH_TOKEN_DIR={base}/xai-auth",
        f"JAILBEE_LITELLM_HOT_FILE={base}/{HOT_FILE}",
        f"JAILBEE_LITELLM_ACK_FILE={base}/{ACK_FILE}",
        f"JAILBEE_LITELLM_ENV_FILE={base}/{ENV_FILE}",
        "LITELLM_LOCAL_MODEL_COST_MAP=True",
    ]
    for name, value in sorted((secrets or {}).items()):
        if any(c in value for c in "'\\\n\r\0"):
            raise ValueError(f"secret {name} cannot be written to the proxy environment")
        lines.append(f"{name}='{value}'")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class InstanceFiles:
    """What one instance reads from `<CONTAINER_STATE_DIR>/<account>/`.

    `settings_yaml` is `config_yaml` without its `model_list`: the part of the
    config only a restart re-reads. It is the digest's basis and is never pushed.
    `env_settings` is `instance_env` without the secrets only `model_list`
    references: the part only a restart re-reads; never pushed.
    `login_providers` are the logins the instance's routes need; `litellm._converge`
    reads them, the digests do not.
    """

    account: str
    config_yaml: str
    settings_yaml: str
    hot_json: str
    instance_env: str
    env_settings: str
    login_providers: tuple[str, ...] = ()

    def digest(self, callback_source: str) -> str:
        """Digest of everything only a restart re-reads (the cold half)."""
        sha = hashlib.sha256()
        for name, text in (
            ("jailbee_callback.py", callback_source),
            ("settings.yaml", self.settings_yaml),
            ("instance.env", self.env_settings),
        ):
            sha.update(name.encode() + b"\0" + text.encode() + b"\0")
        return sha.hexdigest()

    def hot_digest(self) -> str:
        """Digest of `hot.json`'s bytes: what the proxy's acknowledgement echoes."""
        return hashlib.sha256(self.hot_json.encode()).hexdigest()


def render_instance_files(
    cfg: LiteLLMConfig,
    account: str,
    *,
    port: int,
    master_key: str,
    secrets: Mapping[str, str] | None = None,
    extra: Mapping[str, object] | None = None,
    scopes: Scopes | None = None,
) -> InstanceFiles:
    config = render_instance_config(cfg, account, extra=extra, scopes=scopes)
    settings = {k: v for k, v in config.items() if k != "model_list"}
    referenced = env_references(config)
    cold_names = env_references(settings)
    own = {n: v for n, v in (secrets or {}).items() if n in referenced}
    cold = {n: v for n, v in own.items() if n in cold_names}
    hot_env = {n: v for n, v in own.items() if n not in cold_names}
    hot = {
        "callback": render_callback_data(cfg, account, scopes=scopes),
        "models": config["model_list"],
        "env": {"names": sorted(hot_env), "digest": _values_digest(hot_env)},
    }

    def env(values: Mapping[str, str]) -> str:
        return render_instance_env(
            port=port, master_key=master_key, account=account, secrets=values
        )

    return InstanceFiles(
        account=account,
        config_yaml=yaml.safe_dump(config, sort_keys=False),
        settings_yaml=yaml.safe_dump(settings, sort_keys=False),
        hot_json=json.dumps(hot, indent=2, default=str) + "\n",
        instance_env=env(own),
        env_settings=env(cold),
        login_providers=account_login_providers(cfg, account, scopes),
    )


def container_profiles(
    cfg: LiteLLMConfig, *, base_urls: Mapping[str, str], scope: str | None = None
) -> dict[str, dict[str, object]]:
    """`claude-jb`'s view of every profile whose account has a running instance, in one scope."""
    routes = cfg.effective_routes()
    out: dict[str, dict[str, object]] = {}
    for name, profile in cfg.effective_profiles().items():
        account = cfg.instance_account(profile)
        if account not in base_urls:
            continue
        out[name] = {
            "base_url": base_urls[account],
            "key_file": container_key_file(account),
            "effort": profile.effort,
            "instructions": profile.instructions,
            "tiers": {t: tier_alias(scope, name, t) for t in profile.tiers},
            # One value for the whole session, whichever tier is in use: the
            # smallest, so no tier is filled past what its backend accepts.
            "context_window": min(routes[r].context_window for r in profile.tiers.values()),
            # What `claude-jb --context` may raise it to, by the same rule.
            "max_context_window": min(routes[r].max_context_window for r in profile.tiers.values()),
        }
    return out


def _route_egress(route: ResolvedRoute) -> list[str]:
    if route.api_base:
        hosts = [api_base_endpoint(route.api_base)]
    else:
        hosts = [f"{h}:443" for h in PROVIDER_HOSTS.get(route.provider, ())]
    if route.oauth:
        hosts.append(f"{XAI_AUTH_HOST}:443")
    return [*hosts, *route.egress]


def egress_hosts(cfg: LiteLLMConfig, *, scopes: Scopes | None = None) -> list[str]:
    """`host[:port]` entries the proxy container may reach (no port = 443)."""
    entries = {
        entry
        for _, view in _scoped(cfg, scopes)
        for account in view.accounts
        for route in served_routes(view, account).values()
        for entry in _route_egress(route)
    }
    entries.update(cfg.egress)
    return sorted(entries)


def upstream_targets(cfg: LiteLLMConfig, *, scopes: Scopes | None = None) -> list[tuple[str, int]]:
    """Hosts `jailbee doctor` probes from inside the proxy; CIDR entries are not probeable."""
    targets: list[tuple[str, int]] = []
    for raw in egress_hosts(cfg, scopes=scopes):
        spec = parse_egress_entry(raw)
        if spec.is_literal and "/" in spec.target:
            continue
        targets.append((spec.target, spec.port or 443))
    return targets
