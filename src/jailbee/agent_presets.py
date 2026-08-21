"""Shipped starting points for `agents:` entries.

Every value here is a *base layer* the user's own config is merged over
(see `config.resolve_agents_raw`), so a stale entry is a one-line override in
the user's config and never needs a jailbee release. Data only — no imports
from `config.py`, so `config.py` can import this at module level.

Only `claude` is exercised in production. The rest are untested templates:
package names, config paths and especially host lists are best-effort. See
docs/agents.md for how to correct one.
"""

from jailbee.constants import CLAUDE_API_HOSTS

AGENT_PRESETS: dict[str, dict[str, object]] = {
    "codex": {
        "command": "codex",
        "install": "npm i -g @openai/codex",
        "update": "npm i -g @openai/codex@latest",
        "shared": [{"subpath": "codex", "path": "~/.codex"}],
        "egress_allow": ["api.openai.com:443"],
    },
    "gemini": {
        "command": "gemini",
        "install": "npm i -g @google/gemini-cli",
        "update": "npm i -g @google/gemini-cli@latest",
        "shared": [{"subpath": "gemini", "path": "~/.gemini"}],
        "egress_allow": [
            "generativelanguage.googleapis.com:443",
            "cloudcode-pa.googleapis.com:443",
            "oauth2.googleapis.com:443",
            "accounts.google.com:443",
        ],
    },
    "aider": {
        "command": "aider",
        "install": "uv tool install --with pip aider-chat@latest",
        "update": "uv tool upgrade aider-chat",
        # Only the config file is shared. History files are per-branch working
        # state, and ~/.env is a generic filename whose shared mount would leak
        # unrelated secrets between containers. See docs/agents.md.
        "shared": [
            {
                "subpath": "aider.conf.yml",
                "path": "~/.aider.conf.yml",
                "type": "file",
            }
        ],
        "egress_allow": [],
    },
    "opencode": {
        "command": "opencode",
        "install": "npm i -g opencode-ai@latest",
        "update": "npm i -g opencode-ai@latest",
        "shared": [
            {"subpath": "opencode-config", "path": "~/.config/opencode"},
            {"subpath": "opencode-data", "path": "~/.local/share/opencode"},
        ],
        "egress_allow": [],
    },
    "grok": {
        "command": "grok",
        "install": "curl -fsSL https://x.ai/cli/install.sh | bash",
        "update": "curl -fsSL https://x.ai/cli/install.sh | bash",
        # The installer's redirect/CDN target is undocumented, so the install
        # step runs with a wider allowlist rather than guessing hosts.
        "install_network": "loose",
        "shared": [{"subpath": "grok", "path": "~/.grok"}],
        "egress_allow": ["api.x.ai:443", "x.ai:443"],
    },
}


def claude_preset() -> dict[str, object]:
    """Claude's preset. A function, not a literal, because it reads the
    bundled installer script path and the shared host tuple from config."""
    return {
        "command": "claude",
        "install": "__bundled__:ensure-claude.sh",
        "update": "__bundled__:ensure-claude.sh",
        "shared": [
            {"subpath": "claude", "path": "~/.claude"},
            {"subpath": "claude.json", "path": "~/.claude.json", "type": "file", "seed": "{}\n"},
            {"subpath": "claude-install", "path": "~/.local/share/claude"},
        ],
        "egress_allow": list(CLAUDE_API_HOSTS),
    }
