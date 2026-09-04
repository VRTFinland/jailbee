"""Network-facing config models: net-mode descriptions, loose-auto-revert
policy, JetBrains/GitHub egress host lists, and shared Claude credentials.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jailbee.paths import xdg_data_home

# Descriptions baked into the generated Incus net-profiles (visible via
# `incus profile show <prefix>-net-{strict,loose}`). The modes themselves
# are fixed — only `egress_allow` (strict-mode allowlist) is
# user-configurable.
NET_DESCRIPTIONS: dict[str, str] = {
    "strict": "Minimal egress for normal dev",
    "loose": "Wider egress for debugging",
}

# The `offline` network mode (no NIC attached) was removed: `strict` is
# already a default-deny egress allowlist, so `offline` only duplicated it
# with extra UI surface. Config files carrying the old value get this
# message rather than a bare enum error.
OFFLINE_REMOVED_MSG = (
    "network mode 'offline' was removed — use 'strict' (default-deny egress allowlist)"
)


def _reject_offline(v: object) -> object:
    """`mode="before"` validator body shared by the two network fields."""
    if v == "offline":
        raise ValueError(OFFLINE_REMOVED_MSG)
    return v


# Hosts JetBrains IDEs need to reach for license activation, plugin
# marketplace, installer/CDN, and framework dependency config. Added
# automatically by `Config.effective_egress_allow()` whenever
# `jetbrains.enabled` is true so users don't have to know JetBrains'
# service topology.
#
# Sourced from JetBrains' published allowlist guidance
# (https://intellij-support.jetbrains.com/hc/en-us/articles/206544429):
# - account / cloudconfig: JBA license activation and validation
# - plugins: plugin marketplace
# - www, download, download-cf, download-cdn: docs + installers + CDNs
# - frameworks: Java framework dependency config + AI prompt rules
# - data.services: legacy services endpoint (retained for older IDE builds)
# - resources: OAuth provider icons rendered in the JBA sign-in dialog;
#   without it the login UI cannot finish loading and license activation
#   silently stalls in "trial available" state.
# - api.jetbrains.cloud: license trace-status endpoint. Note the `.cloud`
#   TLD — a wildcard on `*.jetbrains.com` would NOT match this host.
# - oauth.account: JBA OAuth sign-in endpoint. Different subdomain AND
#   different IP space (AWS ELB in eu-west-1) than account.jetbrains.com,
#   so the account allowlist entry doesn't cover it. Without this the
#   sign-in flow cannot complete the OAuth handshake.
# - downloads.marketplace: plugin payload CDN (CloudFront), separate IP
#   space from plugins.jetbrains.com. Required for installing or updating
#   plugins from the marketplace in strict mode.
JETBRAINS_LICENSE_HOSTS: tuple[str, ...] = (
    "account.jetbrains.com:443",
    "oauth.account.jetbrains.com:443",
    "cloudconfig.jetbrains.com:443",
    "plugins.jetbrains.com:443",
    "downloads.marketplace.jetbrains.com:443",
    "www.jetbrains.com:443",
    "resources.jetbrains.com:443",
    "download.jetbrains.com:443",
    "download-cf.jetbrains.com:443",
    "download-cdn.jetbrains.com:443",
    "frameworks.jetbrains.com:443",
    "data.services.jetbrains.com:443",
    "api.jetbrains.cloud:443",
)

# Hosts JetBrains AI Assistant uses. Added by `effective_egress_allow()`
# only when `jetbrains.enabled` AND `jetbrains.ai_enabled` are true,
# because AI Assistant is opt-in and most users won't need these.
JETBRAINS_AI_HOSTS: tuple[str, ...] = (
    "api.app.prod.grazie.aws.intellij.net:443",
    "api.jetbrains.ai:443",
)

# Hosts gh CLI reaches: api.github.com for REST/GraphQL. Added
# automatically by `Config.effective_egress_allow()` when `github.enabled`
# so users don't have to know the GitHub API topology. Intentionally
# minimal — github.com:443 / codeload.github.com:443 /
# uploads.github.com:443 / objects.githubusercontent.com:443 stay off
# until a use case forces them in.
GITHUB_API_HOSTS: tuple[str, ...] = ("api.github.com:443",)


_DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h)$")


def _parse_duration(value: str) -> timedelta:
    """Parse ``30s`` / ``5m`` / ``2h`` into a ``timedelta``.

    Unitless integers go through ``LooseAutoRevert.after`` directly (typed
    as ``int``) and are interpreted as minutes — this helper handles only
    suffixed strings.
    """
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise ValueError(f"invalid duration {value!r}; expected `<int>s|m|h` (e.g. `5m`)")
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "s":
        return timedelta(seconds=n)
    if unit == "m":
        return timedelta(minutes=n)
    return timedelta(hours=n)


class LooseAutoRevert(BaseModel):
    """Policy for auto-reverting `jailbee net loose` after a TTL.

    Lives in both ``~/.config/jailbee/global.yaml`` and per-repo
    ``.jailbee/config.yaml``. Per-repo overrides global field by field — see
    ``Config.effective_loose_auto_revert``.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=True,
        description="Whether `jailbee net loose` auto-reverts at all.",
    )
    after: str | int = Field(
        default="5m",
        description=(
            "How long to stay in loose mode before auto-reverting. Accepts `30s`, "
            "`5m`, `2h`, or a bare int meaning minutes; capped at 24h. Each "
            "`jailbee net loose` call can override this for that one switch."
        ),
    )

    def duration(self) -> timedelta:
        """Parse ``after`` into a ``timedelta``. Raises ``ValueError`` on
        bad input (negative, zero, unparseable, or >24h).
        """
        raw = self.after
        if isinstance(raw, int):
            if raw <= 0:
                raise ValueError(f"loose_auto_revert.after must be > 0, got {raw}")
            td = timedelta(minutes=raw)
        else:
            td = _parse_duration(raw)
        if td <= timedelta(0):
            raise ValueError(f"loose_auto_revert.after must be > 0, got {raw!r}")
        if td > timedelta(hours=24):
            raise ValueError(f"loose_auto_revert.after must be <= 24h, got {raw!r}")
        return td


# Durations offered when jailbee asks how long to stay in loose — the CLI
# prompt's preset list and the Qt dashboard's dialog items. Not a policy:
# the effective default still comes from `LooseAutoRevert.after`, and any
# value `LooseAutoRevert.duration()` accepts can be typed instead.
LOOSE_TTL_PRESETS: tuple[str, ...] = ("5m", "15m", "30m", "1h", "2h", "4h", "8h")


# A credential-group name becomes one directory name under
# `<xdg_data_home>/jailbee/claude-credentials/`. Restricting it to a single
# lowercase path segment is what stops `../` from escaping that root — the
# directory holds a live Claude credential.
_CREDENTIAL_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _validated_group(value: str | None) -> str | None:
    if value is None:
        return None
    if not _CREDENTIAL_GROUP_RE.match(value):
        raise ValueError(f"invalid credential group name {value!r}: must match [a-z0-9][a-z0-9-]*")
    return value


class ClaudeCredentials(BaseModel):
    """Which repos on this host share one Claude credential directory.

    Host-level only (`_HOST_LEVEL_KEYS`), read from
    `~/.config/jailbee/global.yaml`. A group name is a property of *this*
    machine's working set: committed to a repo's config it would apply to
    every teammate and name a group that exists on one machine only.

    `group` is the default for every repo on the host; `repos` overrides it per
    `container_prefix`, and an explicit `null` there opts one repo out. Absent
    block, or no resolved group, means the repo keeps its own credential inside
    its config home — today's behaviour.

    Only the *credential* is shared. Each repo keeps its own `~/.claude`, so
    project history, MCP config and sessions never cross repos. Claude Code
    resolves `.credentials.json` and `.oauth_refresh.lock` from
    `CLAUDE_SECURESTORAGE_CONFIG_DIR`, independently of `CLAUDE_CONFIG_DIR`,
    which is what makes the split possible at all.
    """

    model_config = ConfigDict(extra="forbid")
    group: str | None = Field(
        default=None,
        description=(
            "Default credential group for every repo on this host. Absent means no "
            "sharing — each repo keeps its own credential."
        ),
    )
    repos: dict[str, str | None] = Field(
        default={},
        description=(
            "Per-repo override keyed by `container_prefix`. Wins over `group`, "
            "including when the value is `null` — the only way to keep one repo on "
            "its own credential while the rest of the host shares one."
        ),
    )

    @field_validator("group")
    @classmethod
    def _check_group(cls, value: str | None) -> str | None:
        return _validated_group(value)

    @field_validator("repos")
    @classmethod
    def _check_repos(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        for group in value.values():
            _validated_group(group)
        return value

    def dir_for(self, container_prefix: str) -> Path | None:
        """The credential directory for one repo, or None when it shares none.

        A `repos` entry wins over `group` *including when it is `null`* —
        opting one repo out is the only way to keep it on its own credential
        while the rest of the host shares one.
        """
        group = self.repos[container_prefix] if container_prefix in self.repos else self.group
        if not group:
            return None
        return xdg_data_home() / "jailbee" / "claude-credentials" / group


def parse_loose_ttl(raw: str) -> timedelta | None:
    """Parse a user-supplied loose TTL. ``never`` → None (no auto-revert).

    The single definition of the duration syntax accepted by `jailbee net loose
    --for`, the CLI's interactive prompt and the Qt dashboard's dialog — all
    three share it so a value one accepts can never be rejected by another.
    Delegates to `LooseAutoRevert.duration()` so the units and the 24h cap stay
    in one place; raises `ValueError` with its message.
    """
    value = raw.strip()
    if value.lower() == "never":
        return None
    return LooseAutoRevert(after=value).duration()


def format_loose_after(after: str | int) -> str:
    """Render a `LooseAutoRevert.after` value as prompt-ready text.

    The field is `str | int`; a bare int means minutes.
    """
    return f"{after}m" if isinstance(after, int) else after
