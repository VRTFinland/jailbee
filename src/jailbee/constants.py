"""Shared constants with no dependency on `config.py` or `init_command.py`.

A leaf module (imports nothing from `jailbee`) so that other leaf modules —
notably `agent_presets.py` — can import from it without creating an import
cycle back through `config.py`.

`config.py` and `init_command.py` import `SHARED_SUBDIRS` from here, so that
name stays reachable through either of them. The Claude host tuples do not:
`CLAUDE_API_HOSTS` and `CLAUDE_PLUGIN_HOSTS` moved here outright and are no
longer attributes of `jailbee.config` — import them from this module.
"""

from __future__ import annotations

# Hosts Claude Code reaches: api.anthropic.com (chat/completions),
# code.claude.com (in-CLI documentation links / /help), claude.ai (the
# `install.sh` bootstrap entry point — it 302-redirects to
# downloads.claude.ai, so the bare host is needed for the initial GET),
# and downloads.claude.ai (the redirect target: `latest` version pointer,
# `manifest.json` checksums, and the platform binary blob). Both
# `curl https://claude.ai/install.sh | bash` (run by `ensure-claude.sh`
# at `jailbee new` time) and `claude` self-updates use these. Added
# automatically by `Config.effective_egress_allow()` when `claude.enabled`
# so users don't have to know the Anthropic service topology. Note: the
# install now runs inside the (possibly strict-mode) container rather than
# the unrestricted golden-build container, so claude.ai must be on the ACL.
CLAUDE_API_HOSTS: tuple[str, ...] = (
    "api.anthropic.com:443",
    "code.claude.com:443",
    "claude.ai:443",
    "downloads.claude.ai:443",
)

# Hosts Claude Code's plugin/marketplace machinery reaches at session start
# and on `/plugin install` / `/reload-plugins`: GitHub for marketplace clones
# and content fetches, npm for plugin-bundled tools (LSP servers, etc.).
# Added automatically by `Config.effective_egress_allow()` when both
# `claude.enabled` and `claude.plugins_enabled` are true so that skills,
# SessionStart hooks and plugin updates work in strict-mode containers.
CLAUDE_PLUGIN_HOSTS: tuple[str, ...] = (
    "github.com:443",
    "api.github.com:443",
    "raw.githubusercontent.com:443",
    "objects.githubusercontent.com:443",
    "codeload.github.com:443",
    "registry.npmjs.org:443",
)

SHARED_SUBDIRS = (
    "caches/pnpm-store",
    "caches/gradle",
    "caches/npm",
    "caches/m2",
    "chrome-pool/slots",
    "chrome-pool/by-container",
    "docker-registry",
    "ssh",
)
