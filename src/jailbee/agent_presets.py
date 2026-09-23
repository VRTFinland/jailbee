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

# opencode's vendor installer. Not interactive (unlike codex's, which needs
# `CODEX_NON_INTERACTIVE=1`) and it never prompts, so it needs no env guard.
#
# `--no-modify-path` because its PATH edit is dead weight here: the installer
# appends `export PATH=$HOME/.opencode/bin:$PATH` to the END of ~/.bashrc, and
# Debian's ~/.bashrc returns early when the shell isn't interactive — which
# every shell jailbee runs an agent under is (`bash -lc`). The symlink below is
# what actually puts the binary on PATH.
_OPENCODE_INSTALLER = "curl -fsSL https://opencode.ai/v2/install | bash -s -- --no-modify-path"

# The installer hardcodes `INSTALL_DIR=$HOME/.opencode/bin` with no env
# override, and nothing in the golden image puts that directory on PATH — so
# without this link `command -v opencode` fails, `_check_installed` reports
# "not installed" forever (re-downloading 88MB on every `jailbee new`) and the
# autostart window dies with `opencode: not found`. ~/.local/bin is on PATH via
# /etc/profile.d/local-bin.sh, and it is per container while ~/.opencode is
# shared — so the link is re-made on every install/update, exactly as codex's
# installer re-makes its own ~/.local/bin/codex.
#
# The trailing test is the step's real verdict on an *install*: `curl … | bash`
# exits 0 when curl fails (bash just reads an empty script), so without it a
# failed download would look like a successful install step. It cannot serve an
# *update*, where the previous release is still on disk and passes the test — so
# both lines also run under `set -o pipefail`, which is what makes curl's own
# exit status the pipeline's. Only the update line's `pipefail` is testable:
# on the install line the pipe runs only when no binary exists, and then the
# `-x` test below fails anyway (`ln -sfn` makes a dangling symlink happily).
# It is there for symmetry — one spelling for both lines.
_OPENCODE_LINK = (
    'mkdir -p "$HOME/.local/bin"; '
    'ln -sfn "$HOME/.opencode/bin/opencode" "$HOME/.local/bin/opencode"; '
    '[ -x "$HOME/.local/bin/opencode" ]'
)

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
        # Sharing the whole home is deliberate beyond the binary: one login,
        # one config.toml and one session history (`/resume`, memories) serve
        # every container of the repo. The sqlite files carrying that state
        # have rotating numeric suffixes (`state_5.sqlite`,
        # `thread_history_1.sqlite`, ...), so they cannot be named as mounts
        # of their own — the directory mount is what keeps them shared.
        #
        # Two subdirectories are carved back out. Codex keeps its app-server
        # control socket and daemon pid files under $CODEX_HOME and offers no
        # way to relocate them (checked against 0.154.0: the paths are
        # assembled from CODEX_HOME in the binary, and the only related env
        # var is an internal remote-control kill switch). A *pathname* AF_UNIX
        # socket is not confined by a network namespace, so sharing that
        # directory let one container's Codex frontend connect to another
        # container's daemon; the protocol passes the working directory as a
        # string, every container clones the repo to the same path, and the
        # daemon therefore edited and committed to the wrong clone.
        "shared": [
            {
                "subpath": "codex",
                "path": "~/.codex",
                "private": ["app-server-control", "app-server-daemon"],
            }
        ],
        # Where Codex reads user-level skills (`CODEX_HOME/skills`). Inside the
        # `~/.codex` mount above, so jailbee's bundled skills land there once
        # and serve every container of the repo.
        #
        # Checked against codex-cli 0.155.1: the bundled skill installer
        # writes into `$CODEX_HOME/skills`, and `CODEX_HOME` defaults to
        # `~/.codex` (the same default the mount above relies on). A wrong
        # path here fails *silently* — the copy succeeds, nothing reads it —
        # so re-check it against the agent when bumping the preset.
        "skills_dir": "~/.codex/skills",
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
        # Where gemini-cli reads user-level skills (`~/.gemini/skills`), inside
        # the `~/.gemini` mount above.
        #
        # Taken from the upstream docs, not verified against a running
        # gemini-cli. A wrong path here fails *silently* — the copy succeeds,
        # nothing reads it — so confirm it on a host that has the agent
        # before relying on it.
        "skills_dir": "~/.gemini/skills",
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
        # The vendor's own installer, not `npm i -g opencode-ai@latest`. Same
        # reasoning as `codex` above: npm exists in the golden image only when
        # `golden.stacks.node` is on, so the npm line made enabling this preset
        # a silent no-op on every image without the node stack. The installer
        # drops a single static binary and needs no toolchain — only `curl` and
        # `tar`, both in the base image.
        #
        # Install skips the download when the shared store already holds the
        # binary (a second branch container of the same repo); update always
        # re-runs the installer, which is how it upgrades.
        "install": (
            f'set -eo pipefail; [ -x "$HOME/.opencode/bin/opencode" ] || {_OPENCODE_INSTALLER}; '
            f"{_OPENCODE_LINK}"
        ),
        "update": f"set -eo pipefail; {_OPENCODE_INSTALLER}; {_OPENCODE_LINK}",
        # The installer fetches itself from opencode.ai, reads the current
        # version from `opencode.ai/update/api/latest/cli/npm`, and pulls the
        # ~88MB platform tarball from registry.npmjs.org. Both are CDN-fronted
        # and round-robin their IPs, which is the case the ACL's
        # resolve-at-apply-time pooling handles worst on a first run — so the
        # install step gets `loose` rather than a hostname list that fails
        # intermittently. Same reasoning as `codex` and `grok`.
        "install_network": "loose",
        # `~/.opencode` is where the installer puts the binary, and sharing it
        # means the 88MB download (198MB on disk) happens once per repo rather
        # than once per branch. It holds only `bin/` today; if a future release puts a
        # control socket in there, `jailbee doctor` reports it and the fix is a
        # `private:` carve-out, exactly as for codex.
        "shared": [
            {"subpath": "opencode-install", "path": "~/.opencode"},
            {"subpath": "opencode-config", "path": "~/.config/opencode"},
            {"subpath": "opencode-data", "path": "~/.local/share/opencode"},
        ],
        # Where opencode reads user-level skills (`~/.config/opencode/skills`),
        # inside the `opencode-config` mount above. It also scans
        # Claude-compatible `~/.claude/skills`, but its own directory is the
        # canonical one — relying on claude's mount would break the moment
        # claude is not enabled.
        #
        # Taken from the upstream docs, not verified against a running
        # opencode (2.0.9's bundle assembles the path at runtime, so it
        # cannot be read off the binary). A wrong path here fails *silently*
        # — the copy succeeds, nothing reads it — so confirm it against the
        # agent before relying on it.
        "skills_dir": "~/.config/opencode/skills",
        # opencode's *own* hosts only. It is a multi-provider client, so which
        # inference host it needs depends entirely on the provider the user
        # configures — those stay the user's to add (see docs/agents.md §6).
        # `opencode.ai` covers both first-party paths: the built-in "zen"
        # gateway (`/zen/v1/...`) and the version pointer a self-update reads.
        # `models.dev` is the model catalogue opencode fetches at startup.
        "egress_allow": [
            "opencode.ai:443",
            "models.dev:443",
        ],
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
        # Where Claude Code reads user-level skills; inside the `~/.claude`
        # mount above. Checked against Claude Code 2.1.278, and the path this
        # preset has always written to.
        "skills_dir": "~/.claude/skills",
        "egress_allow": list(CLAUDE_API_HOSTS),
    }
