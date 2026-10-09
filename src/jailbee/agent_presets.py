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
        # One-shot mode for `jailbee pr`. `exec` runs a single non-interactive
        # turn and prints the final message; the permissions flag is the
        # equivalent of what the claude preset passes — the container is the
        # sandbox. Taken from the codex-cli docs, not run against a live
        # codex: re-check it against the agent when bumping the preset.
        "headless": (
            'codex exec ${JAILBEE_PR_MODEL:+--model "$JAILBEE_PR_MODEL"} '
            "--dangerously-bypass-approvals-and-sandbox "
            '"$JAILBEE_PR_PROMPT"'
        ),
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
        # One-shot mode for `jailbee pr`: `-p` is the non-interactive prompt
        # flag and `--yolo` skips tool confirmations (the container is the
        # sandbox). From the upstream docs, not run against a live gemini-cli.
        "headless": (
            'gemini ${JAILBEE_PR_MODEL:+--model "$JAILBEE_PR_MODEL"} --yolo -p "$JAILBEE_PR_PROMPT"'
        ),
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
        # `tar`, both in the base image. Unlike codex's, it never prompts.
        #
        # Install and update are the same script, which decides from the store
        # and `JAILBEE_AUTO_UPDATE` — see its header.
        "install": "__bundled__:ensure-opencode.sh",
        "update": "__bundled__:ensure-opencode.sh",
        # One-shot mode for `jailbee pr`: `opencode run` takes the message as an
        # argument and exits when the turn ends. `--model` wants
        # `provider/model`, which is the user's to put in `pr.model`. From the
        # upstream docs, not run against a live opencode.
        "headless": (
            'opencode run ${JAILBEE_PR_MODEL:+--model "$JAILBEE_PR_MODEL"} "$JAILBEE_PR_PROMPT"'
        ),
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
        #
        # Neither `~/.config/opencode` nor `~/.local/share/opencode` is shared:
        # opencode keeps its whole session state, auth tokens included, in a
        # SQLite database there, so one shared copy would hand every container
        # of the repo the others' sessions and credentials, and concurrent
        # writers to one SQLite file over a bind mount are not safe. Each
        # container therefore configures and logs in on its own — a real cost,
        # and the reason `claude-jb` (Claude Code through `jailbee litellm`)
        # is the supported way to run other providers' models. Only the skills
        # subdirectory is shared, so jailbee's skills still reach every
        # container without exposing anything else under the config home.
        "shared": [
            {"subpath": "opencode-install", "path": "~/.opencode"},
            {"subpath": "opencode-skills", "path": "~/.config/opencode/skills"},
        ],
        # Where opencode reads user-level skills (`~/.config/opencode/skills`),
        # the `opencode-skills` mount above. It also scans
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
    "pi": {
        "command": "pi",
        # A Node app installed with npm into the shared `pi-install` store, not
        # through its vendor installer: `pi.dev/install.sh` opens /dev/tty and
        # prompts (install Node? edit PATH? start pi now?), with no env switch
        # to skip them, and the install step's tmux window has a tty — so
        # `jailbee new` would block on the first prompt until
        # `autostart.step_timeout`. Without a tty it installs nothing either: it
        # needs Node >= 22.19 already on PATH, which is `golden.stacks.node`.
        #
        # Install and update are the same script, which decides from the store
        # and `JAILBEE_AUTO_UPDATE` — see its header for the release layout.
        "install": "__bundled__:ensure-pi.sh",
        "update": "__bundled__:ensure-pi.sh",
        # registry.npmjs.org is CDN-fronted and round-robins its IPs — the
        # case the ACL's resolve-at-apply-time pooling handles worst — and is on
        # the strict allowlist only incidentally, when claude's plugins are on.
        "install_network": "loose",
        # One-shot mode for `jailbee pr`. `-p` prints the final answer and
        # exits; `--no-approve` keeps a PR branch's `.pi/` extensions and
        # settings from loading, since print mode cannot show the trust prompt.
        # pi accepts a caller-chosen `--session-id`, so a timed-out run leaves
        # its transcript under the id jailbee reports. Run against pi 1.0.0.
        "headless": (
            'pi -p --no-approve ${JAILBEE_PR_MODEL:+--model "$JAILBEE_PR_MODEL"} '
            '--session-id "$JAILBEE_PR_SESSION" "$JAILBEE_PR_PROMPT"'
        ),
        # `~/.pi/agent` is pi's whole user home — settings.json, models.json,
        # auth.json, sessions/, skills/ — shared like codex's `~/.codex`, so
        # one /login, one provider setup and one session history serve every
        # container of the repo. Unlike opencode's SQLite store this is safe
        # to share: sessions are one JSONL file each, and auth.json is
        # written under a proper-lockfile lock, a mkdir-based lock with
        # mtime staleness that holds across containers (no PID in it).
        # Checked against pi 1.0.0: nothing in the directory is a socket.
        "shared": [
            {"subpath": "pi", "path": "~/.pi/agent"},
            {"subpath": "pi-install", "path": "~/.local/share/pi"},
        ],
        # `<agent-dir>/skills`, inside the `~/.pi/agent` mount above. From pi
        # 1.0.0's docs/configuration.md, not verified against a running pi. A
        # wrong path here fails *silently* — the copy succeeds, nothing reads
        # it — so confirm it against the agent before relying on it.
        "skills_dir": "~/.pi/agent/skills",
        # pi's own host: the latest-version check and install telemetry.
        # Provider hosts follow the provider the user configures, as for
        # opencode; a local model on the host needs `host_ports`, not egress.
        "egress_allow": ["pi.dev:443"],
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
        # One-shot mode for `jailbee pr`.
        #
        # `claude` lives at ~/.local/bin/claude, which is not on the default
        # `incus exec --user` PATH; the command runs through a login shell
        # (`bash -lc`) so ~/.profile puts it there. `${VAR:+...}` drops the whole
        # --model flag when the var is empty, which is how an explicit
        # `pr.model: null` inherits the container's own default model.
        #
        # `--session-id` is chosen by jailbee, not read from Claude's reply: with
        # `--output-format json` nothing reaches stdout until the run ends, so a
        # timeout — the one failure where the transcript is worth reading — is
        # exactly the case where the reply, and the id in it, never arrive.
        "headless": (
            'claude ${JAILBEE_PR_MODEL:+--model "$JAILBEE_PR_MODEL"} '
            '--session-id "$JAILBEE_PR_SESSION" '
            '-p "$JAILBEE_PR_PROMPT" --output-format json --dangerously-skip-permissions'
        ),
        "shared": [
            # Claude Code keeps per-process runtime state inside its config
            # home: the live-session registry (`sessions/`) and the background
            # daemon's roster, dispatch queue and job state (`daemon/`,
            # `jobs/`). Shared across containers, one container's daemon
            # adopted, ran twice or declared dead another container's jobs.
            # The top-level `daemon.lock` cannot be carved out (a file), which
            # is why `agents.claude.agent_view` defaults to off. See
            # docs/agents.md.
            {
                "subpath": "claude",
                "path": "~/.claude",
                "private": ["sessions", "daemon", "jobs"],
            },
            {"subpath": "claude-install", "path": "~/.local/share/claude"},
        ],
        # Where Claude Code reads user-level skills; inside the `~/.claude`
        # mount above. Checked against Claude Code 2.1.278, and the path this
        # preset has always written to.
        "skills_dir": "~/.claude/skills",
        # Claude Code's managed-policy memory file on Linux
        # (https://code.claude.com/docs/en/memory.md): loaded in every
        # session before ~/.claude/CLAUDE.md and cannot be excluded. Outside
        # the shared ~/.claude mount, so the repo's own CLAUDE.md there is
        # untouched. See agent_instructions.py.
        "global_instructions": {"dir": "/etc/claude-code", "file": "CLAUDE.md"},
        "egress_allow": list(CLAUDE_API_HOSTS),
    }
