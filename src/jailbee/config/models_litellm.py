"""`litellm:` — Claude Code on non-Anthropic models through a LiteLLM proxy.

Host-level only (`common._HOST_LEVEL_KEYS`): routes name this host's proxy,
subscription logins and secrets file, which a teammate does not have.

A **route** is one model with its settings. A **profile** maps Claude Code's
four tiers to routes and names the **account** whose proxy instance serves it.
An account is one subscription login and one LiteLLM process: LiteLLM reads
`CHATGPT_TOKEN_DIR` once per process (spike, spec §3). Jailbee ships the
`codex` profile, its routes and the `default` account; a user entry with the
same name overlays the built-in one field by field (`model_fields_set` decides
what was written), so `routes: {sol-high: {effort: max}}` keeps the built-in
model.

Validation never reads `secrets.env` or the `extra` fragment: those are host
files `jailbee litellm up` reads (`litellm_inputs`), so a missing secret breaks
only `up`, not every command that loads `global.yaml`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from jailbee.egress import parse_egress_entry

PINNED_LITELLM_VERSION = "1.104.0"
"""The version `provision/litellm/requirements.lock` was compiled for."""

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
TIERS: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

DEFAULT_ACCOUNT = "default"
ACCOUNT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
"""An account name becomes a state path component, a systemd instance name and
a dev-container key file name, so it must not be able to leave any of them."""

ROUTE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
"""A route name becomes part of a proxy model name: `jb-default-<route>` for the
host's routes, `jb-<prefix>.<route>` for a repo override's. Neither a route name
nor a container prefix may hold a `.`, so a host alias never contains one and a
repo alias contains exactly one: no two aliases can be equal."""

PROFILE_NAME_RE = ROUTE_NAME_RE
"""A profile name becomes part of its tiers' proxy model names:
`jb.<profile>.<level>` for the host's profiles, `jb-<prefix>.<profile>.<level>`
for a repo override's. Without a `.` in it, a tier alias holds exactly two and
never equals a route alias."""

REPO_REFUSED_KEYS: tuple[str, ...] = ("enabled", "version", "accounts", "egress", "extra")
"""`litellm:` keys a repo's `repos/<prefix>.yaml` may not set: they describe the
proxy container and its logins, which every repo on the host shares."""

SUBSCRIPTION_PROVIDERS: frozenset[str] = frozenset({"chatgpt"})
"""Providers whose every route logs in with `jailbee litellm login`; `xai` routes
do only with `oauth: true`."""

PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "chatgpt": ("chatgpt.com", "auth.openai.com"),
    "openai": ("api.openai.com",),
    "openrouter": ("openrouter.ai",),
    "xai": ("api.x.ai",),
    "gemini": ("generativelanguage.googleapis.com",),
    "deepseek": ("api.deepseek.com",),
}
"""Hosts (port 443) the proxy must reach, per LiteLLM provider prefix. A provider
missing here needs `api_base` or `egress` on its route: never a silent allow."""

XAI_AUTH_HOST = "auth.x.ai"
"""The xAI OAuth issuer; every endpoint in its discovery document lives here.
An `oauth: true` route needs it besides `PROVIDER_HOSTS["xai"]`."""

KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    "chatgpt/gpt-6-astra": 272_000,
    "chatgpt/gpt-6.1-sol": 272_000,
    "chatgpt/gpt-6-luna": 272_000,
}
"""Window Claude Code manages per model. Claude Code compacts a fixed reserve
below this value, so one above what the subscription backend accepts would
compact after the backend has already refused the prompt. These are the
subscription backend's input limit, not the API's 1.05M."""

KNOWN_MAX_CONTEXT_WINDOWS: dict[str, int] = {
    "chatgpt/gpt-6-astra": 1_050_000,
    "chatgpt/gpt-6.1-sol": 1_050_000,
    "chatgpt/gpt-6-luna": 1_050_000,
}
"""The most `claude-jb --context` may raise a model's window to: the API's
total. Costs more per request, so it is opt-in per session, never the default."""

PARAMS_DENYLIST: frozenset[str] = frozenset(
    {
        "model",
        "custom_llm_provider",
        "api_base",
        "base_url",
        "api_key",
        "api_version",
        "organization",
        "headers",
        "extra_headers",
        "litellm_credential_name",
        "azure_ad_token",
        "model_info",
        "use_xai_oauth",
    }
)
"""`params` keys that change which provider, endpoint or credential a deployment
uses. `api_key` and `api_base` have their own validated route fields; raw
params would bypass the egress table derived from them and could send the
login token to another host. `use_xai_oauth` has its own route field, `oauth`,
which also binds the route to an account and opens `auth.x.ai`."""

_SECRET_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_RESERVED_SECRET_NAMES = frozenset({"PORT", "PATH", "HOME"})
# `XAI_`: `XAI_API_KEY` in the proxy's environment silently overrides every
# `oauth` route's subscription login, and `XAI_API_BASE` / `XAI_OAUTH_API_BASE`
# would send the OAuth token to another host.
_RESERVED_SECRET_PREFIXES = ("LITELLM_", "JAILBEE_", "CHATGPT_", "XAI_", "PYTHON", "LD_")

INSTRUCTIONS_MAX_BYTES = 64 * 1024
"""Cap on `LiteLLMProfile.instructions`, in UTF-8 bytes. Linux limits one argv
string to 128 KiB (`MAX_ARG_STRLEN`) and `claude-jb` passes the text as one
argument next to the user's own `--append-system-prompt`, so half of that is
the most a profile may take."""

_BUILTIN_ROUTES: dict[str, dict[str, object]] = {
    "astra": {"model": "chatgpt/gpt-6-astra", "effort": "high"},
    "sol-high": {"model": "chatgpt/gpt-6.1-sol", "effort": "high"},
    "sol-medium": {"model": "chatgpt/gpt-6.1-sol", "effort": "medium"},
    "luna-high": {"model": "chatgpt/gpt-6-luna", "effort": "high"},
}
_BUILTIN_PROFILES: dict[str, dict[str, object]] = {
    "codex": {
        "account": DEFAULT_ACCOUNT,
        "fable": "astra",
        "opus": "sol-high",
        "sonnet": "sol-medium",
        "haiku": "luna-high",
    },
}


def provider_of(model: str) -> str:
    """LiteLLM's provider prefix (`openrouter/moonshotai/kimi-k3` → `openrouter`); "" if none."""
    return model.split("/", 1)[0] if "/" in model else ""


def api_base_endpoint(url: str) -> str:
    """The `host:port` the proxy must reach for `api_base`.

    Messages never echo the URL: it may carry a token in its query string.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("api_base must be an http:// or https:// URL with a host")
    if parts.username or parts.password:
        raise ValueError(
            "api_base must not carry credentials; put the key in secrets.env and name it in api_key"
        )
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.hostname}:{port}"


def check_secret_name(name: str) -> str:
    """A secrets.env variable name that cannot shadow the proxy's own environment.

    The first message never echoes `name`: someone who got it wrong may have
    pasted the key itself.
    """
    if not _SECRET_NAME.fullmatch(name):
        raise ValueError(
            "api_key takes the NAME of a variable in secrets.env (A-Z, 0-9, _), "
            "never the key itself"
        )
    if name in _RESERVED_SECRET_NAMES or name.startswith(_RESERVED_SECRET_PREFIXES):
        raise ValueError(f"secret name {name!r} is reserved for the proxy's own environment")
    return name


def _check_egress(entries: list[str]) -> list[str]:
    for entry in entries:
        parse_egress_entry(entry)
    return entries


def _check_route_names(routes: dict[str, object]) -> None:
    bad = sorted(name for name in routes if not ROUTE_NAME_RE.fullmatch(name))
    if bad:
        raise ValueError(
            f"invalid route name(s) {', '.join(repr(n) for n in bad)}: use 1-64 lowercase "
            "letters, digits, '-' or '_', starting with a letter or digit"
        )


def _check_profile_names(profiles: dict[str, object]) -> None:
    bad = sorted(name for name in profiles if not PROFILE_NAME_RE.fullmatch(name))
    if bad:
        raise ValueError(
            f"invalid profile name(s) {', '.join(repr(n) for n in bad)}: use 1-64 lowercase "
            "letters, digits, '-' or '_', starting with a letter or digit"
        )


def input_free_lines(error: ValidationError, root: tuple[str, ...] = ()) -> str:
    """Pydantic's messages without `input_value`, one per line.

    `api_key` takes a secrets.env *name*; someone who pastes the key itself
    would otherwise get it echoed back. `root` prefixes each location, for a
    block validated on its own (`("litellm",)`).
    """
    lines: list[str] = []
    for err in error.errors(include_url=False, include_input=False):
        where = ".".join(str(part) for part in (*root, *err["loc"]))
        lines.append(f"{where}: {err['msg']}" if where else err["msg"])
    return "\n".join(lines)


def _written(model: BaseModel | None) -> dict[str, object]:
    """The fields a layer actually wrote, explicit nulls included."""
    return {} if model is None else {k: getattr(model, k) for k in model.model_fields_set}


class LiteLLMRoute(BaseModel):
    """One model and its settings; every field is optional for built-in overlays."""

    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(
        default=None,
        description=(
            "LiteLLM model string, e.g. `chatgpt/gpt-6.1-sol` or "
            "`openrouter/moonshotai/kimi-k3`. Required for a route jailbee does not ship."
        ),
    )
    effort: EffortLevel | None = Field(
        default=None,
        description=(
            "Fixed reasoning effort: set on every request to this route, so Claude "
            "Code's `/effort` has no effect on it. Lets two tiers share one model "
            "and still differ. Mutually exclusive with `min_effort`."
        ),
    )
    min_effort: EffortLevel | None = Field(
        default=None,
        description=(
            "Effort floor: a lower requested effort is raised to this; `/effort` "
            "works above it. Mutually exclusive with `effort`."
        ),
    )
    context_window: int | None = Field(
        default=None,
        gt=0,
        description=(
            "This route's context window in tokens (use the backend's maximum input), "
            "published to the proxy as the model's `max_input_tokens`. Claude Code takes "
            "one window per session, so `claude-jb` passes the smallest among the "
            "profile's routes as `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. "
            "Defaults to 272000 for `chatgpt/gpt-6-astra`, `chatgpt/gpt-6.1-sol` and "
            "`chatgpt/gpt-6-luna`; required for any other model."
        ),
    )
    max_context_window: int | None = Field(
        default=None,
        gt=0,
        description=(
            "The largest window `claude-jb --context` may select for this route, in tokens "
            "(never below `context_window`). A profile's ceiling is the smallest among its "
            "routes'. Defaults to 1050000 for `chatgpt/gpt-6-astra`, `chatgpt/gpt-6.1-sol` "
            "and `chatgpt/gpt-6-luna`, and otherwise to `context_window`: such a route "
            "cannot be raised."
        ),
    )
    api_key: str | None = Field(
        default=None,
        description=(
            "Name of the variable in `~/.config/jailbee/litellm/secrets.env` that holds "
            "this route's API key: the name, never the key. Not allowed on `chatgpt/` "
            "routes, which log in with `jailbee litellm login`."
        ),
    )
    api_base: str | None = Field(
        default=None,
        description=(
            "Provider endpoint URL passed to LiteLLM. Its host replaces the provider's "
            "default hosts in the proxy's egress allowlist. Not allowed on `chatgpt/` routes."
        ),
    )
    egress: list[str] = Field(
        default_factory=list,
        description=(
            "Extra `host[:port]` entries (port 443 by default) the proxy may reach for "
            "this route. Needed for a provider jailbee has no host table for, unless "
            "`api_base` is set."
        ),
    )
    params: dict[str, object] = Field(
        default_factory=dict,
        description="Raw `litellm_params` merged into this route's deployment.",
    )
    oauth: bool | None = Field(
        default=None,
        description=(
            "Use the account's xAI subscription login (`jailbee litellm login --provider "
            "xai`) instead of an API key. Only on `xai/` models; experimental."
        ),
    )

    @field_validator("api_key")
    @classmethod
    def _api_key_is_a_name(cls, value: str | None) -> str | None:
        return None if value is None else check_secret_name(value)

    @field_validator("api_base")
    @classmethod
    def _api_base_is_a_url(cls, value: str | None) -> str | None:
        if value is not None:
            api_base_endpoint(value)
        return value

    @field_validator("egress")
    @classmethod
    def _egress_parses(cls, value: list[str]) -> list[str]:
        return _check_egress(value)


class LiteLLMProfile(BaseModel):
    """Claude Code tier → route. An explicit `null` unmaps a built-in tier."""

    model_config = ConfigDict(extra="forbid")
    account: str | None = Field(
        default=None,
        description=(
            "Account (from `litellm.accounts`) whose proxy instance serves this profile. "
            "Required when the profile maps a `chatgpt/` route; a profile of API-key "
            "routes only is served by the first account's instance when unset."
        ),
    )
    fable: str | None = Field(default=None, description="Route for Claude Code's Fable tier.")
    opus: str | None = Field(default=None, description="Route for Claude Code's Opus tier.")
    sonnet: str | None = Field(default=None, description="Route for Claude Code's Sonnet tier.")
    haiku: str | None = Field(default=None, description="Route for Claude Code's Haiku tier.")
    effort: EffortLevel | None = Field(
        default=None,
        description=(
            "Session default effort: `claude-jb` passes `--effort <value>` unless "
            "you pass `--effort` yourself."
        ),
    )
    instructions: str | None = Field(
        default=None,
        description=(
            "Model-policy text `claude-jb` appends to Claude Code's system prompt in "
            "sessions of this profile (at most 64 KiB; use a YAML `|` block for several "
            "lines). A higher layer replaces it whole; `null` removes it."
        ),
        json_schema_extra={"multiline": True},
    )

    @field_validator("instructions")
    @classmethod
    def _instructions_usable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("is empty; write `null` to remove it")
        if len(value.encode()) > INSTRUCTIONS_MAX_BYTES:
            raise ValueError(f"exceeds {INSTRUCTIONS_MAX_BYTES // 1024} KiB")
        return value


class LiteLLMRepoOverlay(BaseModel):
    """A repo's `litellm:` block in `~/.config/jailbee/repos/<prefix>.yaml`.

    Stacked on the host block by `LiteLLMConfig.with_overlay`. Host-local like
    the block it overrides; a committed `.jailbee/config.yaml` may not carry it.
    """

    model_config = ConfigDict(extra="forbid")
    routes: dict[str, LiteLLMRoute] = Field(
        default_factory=dict,
        description=(
            "Routes for this repo only. An entry named like a global or built-in route "
            "overrides it field by field; a field set here replaces the global value whole "
            "(`params` and `egress` included)."
        ),
    )
    profiles: dict[str, LiteLLMProfile] = Field(
        default_factory=dict,
        description=(
            "Profiles for this repo only. An entry named like a global or built-in profile "
            "overrides it tier by tier; `null` unmaps a tier."
        ),
    )
    default_profile: str | None = Field(
        default=None,
        description="Profile `claude-jb` uses in this repo's containers by default.",
    )
    autostart: bool | None = Field(
        default=None,
        description=(
            "Overrides `litellm.autostart` for this repo: run the Claude autostart window "
            "with `claude-jb` (true) or `claude` (false)."
        ),
    )

    @field_validator("routes")
    @classmethod
    def _route_names(cls, value: dict[str, LiteLLMRoute]) -> dict[str, LiteLLMRoute]:
        _check_route_names(dict(value))
        return value

    @field_validator("profiles")
    @classmethod
    def _profile_names(cls, value: dict[str, LiteLLMProfile]) -> dict[str, LiteLLMProfile]:
        _check_profile_names(dict(value))
        return value


@dataclass(frozen=True)
class ResolvedRoute:
    name: str
    model: str
    effort: str | None
    min_effort: str | None
    context_window: int
    max_context_window: int
    params: dict[str, object]
    api_key: str | None = None
    api_base: str | None = None
    egress: tuple[str, ...] = ()
    oauth: bool = False

    @property
    def provider(self) -> str:
        return provider_of(self.model)

    @property
    def chatgpt(self) -> bool:
        """The ChatGPT subscription backend, whose quirks the renderer and callback handle."""
        return self.provider == "chatgpt"

    @property
    def login_provider(self) -> str | None:
        """The login this route's account must hold: `chatgpt`, `xai`, or None for an API key."""
        if self.provider in SUBSCRIPTION_PROVIDERS:
            return self.provider
        return "xai" if self.oauth else None

    @property
    def subscription(self) -> bool:
        return self.login_provider is not None


@dataclass(frozen=True)
class ResolvedProfile:
    name: str
    tiers: dict[str, str]
    effort: str | None
    account: str | None = None
    instructions: str | None = None


def _overlay(builtin: dict[str, object], user: BaseModel | None) -> dict[str, object]:
    merged = dict(builtin)
    if user is not None:
        for key in user.model_fields_set:
            merged[key] = getattr(user, key)
    return merged


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


class LiteLLMConfig(BaseModel):
    """Host-level `litellm:` block."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "Turns on the jailbee-managed LiteLLM proxy (`jailbee litellm up`) and "
            "`claude-jb` in containers. Off by default."
        ),
    )
    version: str | None = Field(
        default=None,
        description=(
            "LiteLLM version to install. Defaults to the version jailbee pins and "
            "hash-locks; any other value installs without the hash lock and warns."
        ),
    )
    accounts: list[str] = Field(
        default_factory=lambda: [DEFAULT_ACCOUNT],
        min_length=1,
        description=(
            "Subscription logins, one LiteLLM instance each (`jailbee litellm login "
            "<account>`). The built-in `codex` profile uses `default`: set "
            "`profiles.codex.account` when you rename or drop it."
        ),
    )
    default_profile: str = Field(
        default="codex",
        description="Profile `claude-jb` uses when neither `--profile` nor "
        "`JAILBEE_LITELLM_PROFILE` names one.",
    )
    autostart: bool = Field(
        default=False,
        description=(
            "Start the Claude agent's autostart window with `claude-jb` instead of `claude`: "
            "the command's first word is replaced and its flags are kept. A repo's "
            "`repos/<prefix>.yaml` can override it."
        ),
    )
    routes: dict[str, LiteLLMRoute] = Field(
        default_factory=dict,
        description=(
            "Named models. An entry named like a built-in route (`astra`, "
            "`sol-high`, `sol-medium`, `luna-high`) overrides it field by field."
        ),
    )
    profiles: dict[str, LiteLLMProfile] = Field(
        default_factory=dict,
        description=(
            "Named tier maps. An entry named like a built-in profile (`codex`) "
            "overrides it tier by tier; `null` unmaps a tier."
        ),
    )
    egress: list[str] = Field(
        default_factory=list,
        description=(
            "Extra `host[:port]` entries (port 443 by default) the proxy may reach, for "
            "deployments added through `extra`. Routes' hosts are derived automatically."
        ),
    )
    extra: str | None = Field(
        default=None,
        description=(
            "Path to a raw LiteLLM config fragment deep-merged into every instance's "
            "config last (lists appended). It may not define `jb-*`, `jb.*` or `claude-*` models "
            "or `general_settings.master_key`."
        ),
    )

    @field_validator("accounts")
    @classmethod
    def _account_names(cls, value: list[str]) -> list[str]:
        for account in value:
            if not ACCOUNT_NAME_RE.fullmatch(account):
                raise ValueError(
                    f"invalid account name {account!r}: use 1-32 lowercase letters, digits, "
                    "'-' or '_', starting with a letter or digit"
                )
        repeated = sorted({a for a in value if value.count(a) > 1})
        if repeated:
            raise ValueError(f"accounts listed more than once: {', '.join(repeated)}")
        return value

    @field_validator("egress")
    @classmethod
    def _egress_parses(cls, value: list[str]) -> list[str]:
        return _check_egress(value)

    @field_validator("routes")
    @classmethod
    def _route_names(cls, value: dict[str, LiteLLMRoute]) -> dict[str, LiteLLMRoute]:
        _check_route_names(dict(value))
        return value

    @field_validator("profiles")
    @classmethod
    def _profile_names(cls, value: dict[str, LiteLLMProfile]) -> dict[str, LiteLLMProfile]:
        _check_profile_names(dict(value))
        return value

    def effective_version(self) -> str:
        return self.version or PINNED_LITELLM_VERSION

    def with_overlay(self, overlay: LiteLLMRepoOverlay) -> LiteLLMConfig:
        """This host config with one repo's overrides on top, validated as a whole.

        Route by route and field by field, profile by profile and tier by
        tier — the rule a global entry already follows over a built-in one,
        so built-in ← global ← repo is one mechanism. Raises
        `pydantic.ValidationError` when the result does not hold together.
        """
        data = _written(self)
        data["routes"] = {
            **self.routes,
            **{
                name: LiteLLMRoute.model_validate(
                    {**_written(self.routes.get(name)), **_written(r)}
                )
                for name, r in overlay.routes.items()
            },
        }
        data["profiles"] = {
            **self.profiles,
            **{
                name: LiteLLMProfile.model_validate(
                    {**_written(self.profiles.get(name)), **_written(p)}
                )
                for name, p in overlay.profiles.items()
            },
        }
        if overlay.default_profile is not None:
            data["default_profile"] = overlay.default_profile
        if overlay.autostart is not None:
            data["autostart"] = overlay.autostart
        return LiteLLMConfig.model_validate(data)

    def instance_account(self, profile: ResolvedProfile) -> str:
        """The account whose instance serves `profile`; API-key-only profiles use the first."""
        return profile.account or self.accounts[0]

    def effective_routes(self) -> dict[str, ResolvedRoute]:
        out: dict[str, ResolvedRoute] = {}
        for name in [*_BUILTIN_ROUTES, *(n for n in self.routes if n not in _BUILTIN_ROUTES)]:
            raw = _overlay(_BUILTIN_ROUTES.get(name, {}), self.routes.get(name))
            model = raw.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError(f"route '{name}' has no model")
            provider = provider_of(model)
            api_key = _optional_str(raw.get("api_key"))
            api_base = _optional_str(raw.get("api_base"))
            egress = raw.get("egress") or []
            assert isinstance(egress, list)
            if provider in SUBSCRIPTION_PROVIDERS and (api_key or api_base):
                raise ValueError(
                    f"route '{name}': `{provider}/` routes log in with `jailbee litellm "
                    "login`; `api_key` and `api_base` are not allowed on them"
                )
            oauth = raw.get("oauth") is True
            if oauth and provider != "xai":
                raise ValueError(f"route '{name}': `oauth` is only supported on `xai/` routes")
            if oauth and (api_key or api_base):
                raise ValueError(
                    f"route '{name}': `oauth` routes log in with `jailbee litellm login "
                    "--provider xai`; `api_key` and `api_base` are not allowed on them"
                )
            if provider not in PROVIDER_HOSTS and not api_base and not egress:
                what = f"provider {provider!r}" if provider else f"model {model!r}"
                raise ValueError(
                    f"route '{name}': jailbee has no egress hosts for {what}; "
                    "set `api_base` or `egress`"
                )
            effort, min_effort = raw.get("effort"), raw.get("min_effort")
            if effort is not None and min_effort is not None:
                raise ValueError(f"route '{name}' sets both `effort` and `min_effort`")
            window = raw.get("context_window") or KNOWN_CONTEXT_WINDOWS.get(model)
            if not isinstance(window, int):
                raise ValueError(f"route '{name}' needs `context_window` (unknown model {model!r})")
            explicit_max = raw.get("max_context_window")
            if isinstance(explicit_max, int) and explicit_max < window:
                raise ValueError(
                    f"route '{name}': `max_context_window` ({explicit_max}) is below "
                    f"`context_window` ({window})"
                )
            ceiling = (
                explicit_max
                if isinstance(explicit_max, int)
                else max(window, KNOWN_MAX_CONTEXT_WINDOWS.get(model, 0))
            )
            params = raw.get("params") or {}
            assert isinstance(params, dict)
            forbidden = sorted(k for k in params if str(k).lower() in PARAMS_DENYLIST)
            if forbidden:
                raise ValueError(
                    f"route '{name}' params must not set {', '.join(forbidden)}: "
                    "they change the provider, endpoint or credential"
                )
            out[name] = ResolvedRoute(
                name=name,
                model=model,
                effort=_optional_str(effort),
                min_effort=_optional_str(min_effort),
                context_window=window,
                max_context_window=ceiling,
                params=dict(params),
                api_key=api_key,
                api_base=api_base,
                egress=tuple(str(e) for e in egress),
                oauth=oauth,
            )
        return out

    def effective_profiles(self) -> dict[str, ResolvedProfile]:
        out: dict[str, ResolvedProfile] = {}
        for name in [*_BUILTIN_PROFILES, *(n for n in self.profiles if n not in _BUILTIN_PROFILES)]:
            raw = _overlay(_BUILTIN_PROFILES.get(name, {}), self.profiles.get(name))
            tiers = {t: str(raw[t]) for t in TIERS if raw.get(t) is not None}
            out[name] = ResolvedProfile(
                name=name,
                tiers=tiers,
                effort=_optional_str(raw.get("effort")),
                account=_optional_str(raw.get("account")),
                instructions=_optional_str(raw.get("instructions")),
            )
        return out

    @model_validator(mode="after")
    def _check(self) -> LiteLLMConfig:
        routes = self.effective_routes()
        profiles = self.effective_profiles()
        for profile in profiles.values():
            if not profile.tiers:
                raise ValueError(f"profile '{profile.name}' maps no tier")
            for tier, route in profile.tiers.items():
                if route not in routes:
                    raise ValueError(
                        f"profile '{profile.name}' tier '{tier}' names unknown route '{route}'"
                    )
            if profile.account is not None and profile.account not in self.accounts:
                raise ValueError(
                    f"profile '{profile.name}' uses account '{profile.account}', which is not "
                    f"in `litellm.accounts` ({', '.join(self.accounts)}): add it there or set "
                    f"`profiles.{profile.name}.account`"
                )
            if profile.account is None and any(
                routes[r].subscription for r in profile.tiers.values()
            ):
                raise ValueError(
                    f"profile '{profile.name}' maps a subscription route (`chatgpt/` or "
                    "`oauth: true`), so it must name an `account`"
                )
        if self.default_profile not in profiles:
            raise ValueError(f"default_profile '{self.default_profile}' is not a profile")
        return self


@dataclass(frozen=True)
class LiteLLMRepoView:
    """One repo's view of `litellm:` — what `claude-jb` uses in its containers.

    `config` is the host block with the repo's override on top. `scope` is the
    repo prefix when the override changes routes or profiles, so the proxy
    serves this repo under its own aliases (`jb-<prefix>.<route>`); None when
    it uses the host's (`jb-default-<route>`). `origin` names the override's
    file, None without one.
    """

    config: LiteLLMConfig = field(default_factory=LiteLLMConfig)
    scope: str | None = None
    origin: str | None = None
