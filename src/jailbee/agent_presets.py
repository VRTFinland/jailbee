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
        # The vendor's own installer, not `npm i -g @openai/codex`. npm exists
        # in the golden image only when `golden.stacks.node` is on, so the npm
        # line made enabling this preset a silent no-op on every image without
        # the node stack: the install step died with `npm: command not found`
        # (a warning `_ensure_one` swallows) and the autostart window then died
        # with `codex: not found`. The installer is a static binary drop and
        # needs no toolchain at all.
        #
        # Install and update are the same command line — the script decides
        # which it is from what's already on disk.
        #
        # `CODEX_NON_INTERACTIVE=1` is load-bearing, not tidiness: the script
        # ends every run (install *and* update) with a `Start Codex now? [y/N]`
        # prompt read from /dev/tty, and the install step runs in a tmux window
        # that has one. Without it every `jailbee new` blocks on that prompt
        # until `autostart.step_timeout`.
        "install": "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh",
        "update": "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh",
        # The installer fetches itself from chatgpt.com, then the release from
        # releases.openai.com, falling back to api.github.com + the
        # github.com release redirect. All four are CDN-fronted and
        # round-robin their IPs, which is exactly the case the ACL's
        # resolve-at-apply-time pooling handles worst on a first run — so the
        # install step gets `loose` rather than a hostname list that fails
        # intermittently. Same reasoning as `grok` below. docs/agents.md
        # carries the recipe for pinning this back to strict.
        "install_network": "loose",
        # Also where the installer puts the binary: ~/.local/bin/codex is a
        # per-container symlink into ~/.codex/packages/standalone/current,
        # so the ~300MB payload is downloaded once per repo, not per branch.
        "shared": [{"subpath": "codex", "path": "~/.codex"}],
        # Runtime hosts, all three needed by an ordinary signed-in session:
        # `api.openai.com` is the API-key path (`/v1/responses`, `/auth`),
        # `auth.openai.com` is the sign-in itself — the device-code flow posts
        # to `/api/accounts/deviceauth/usercode` and every later token refresh
        # goes to `/oauth/token` — and `chatgpt.com` is the backend a
        # ChatGPT-plan login actually talks to (`/backend-api/codex/...`).
        # Only the first was here originally, which made `codex` install and
        # start fine in a strict container and then fail at login with
        # `failed to request device code`, the request timing out against the
        # ACL. Telemetry (`ab.chatgpt.com`) is deliberately left out.
        "egress_allow": [
            "api.openai.com:443",
            "auth.openai.com:443",
            "chatgpt.com:443",
        ],
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
        "egress_allow": [
            "api.x.ai:443",
            "x.ai:443",
            "auth.x.ai:443",
            "cli-chat-proxy.grok.com:443",
        ],
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
            {"subpath": "claude-install", "path": "~/.local/share/claude"},
        ],
        "egress_allow": list(CLAUDE_API_HOSTS),
    }
