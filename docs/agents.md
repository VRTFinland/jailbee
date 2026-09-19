# Generic agent support

`jailbee` wires terminal coding agents (Claude Code, and a handful of others)
into the container lifecycle through one declarative key: `agents:`. It is a
mapping keyed by agent name — `{codex: {...}, gemini: {...}}` — rather than a
list, because the deep-merge pipeline that combines `~/.config/jailbee/global.yaml`
and `<repo>/.jailbee/config.yaml` *appends* lists (see
[Merge rules](config.md#merge-rules)). As a list, a global entry and a repo
entry for the same agent would produce two duplicate entries instead of one
adjusted one; as a mapping, the repo layer can tweak a single field of an
agent the global layer already defined.

Six presets ship built in (`claude`, `codex`, `gemini`, `aider`, `opencode`,
`grok`), and you can define an agent that isn't one of them from scratch.
**Only `claude` is exercised in production.** The other five are untested
templates — see [The five templates](#8-the-five-templates) below.

## 1. What this does

For every agent with `enabled: true`, `jailbee`:

- **Mounts** its declared `shared` paths from `<shared_dir>` into the
  container, so credentials and settings survive a container rebuild and are
  shared across the repo's containers (`init_command.py`, `lifecycle.py`).
- **Extends egress.** Its `egress_allow` hosts are folded into the
  strict-mode allowlist (`Config.effective_egress_allow`).
- **Installs or updates it** at `jailbee new` time: `install_check` decides
  whether the binary is already present; if not, `install` runs; if it is,
  `update` runs only when `auto_update` is true (`agents.ensure_agents`,
  called once from `lifecycle.new_container` — not from `jailbee apply` or
  `jailbee start`). The step is named `install-<agent>` and is bounded by
  `autostart.step_timeout` (default 600s), so a stuck installer costs that
  much rather than hanging `jailbee new`; its output stays in the tmux window
  afterwards, so `jailbee tmux <container>` is where you read what happened.
- **Launches it** in a background tmux window when `autostart: true`
  (`autostart.agent_autostart_steps`).
- **Checks its shared dirs** as part of `jailbee doctor`'s `shared_dir tree`
  check.

Install, update, and the autostart launch itself all run through the
autostart step pipeline, which starts each in a fresh `bash -lc` login
shell. The autostart launch runs directly in that shell; install and update
run in a `bash -c` child of it (`agents._ensure_one`). Either way,
`~/.local/bin` and `~/.npm-global/bin` end up on `PATH`: the login shell
sources `/etc/profile.d` with `export`, and the `bash -c` child inherits
that exported PATH — which is also why `agents.<name>.env` reaches all
three.

Installs are infrastructure rather than user autostart steps, so they run
under `jailbee new --no-autostart` as well — `ensure_agents` starts the
container's autostart tmux session itself when nothing else has yet. That is
why `jailbee tmux` on a `--no-autostart` container finds a session with an
`install-<agent>` window in it rather than nothing. The agent's own launch
window is a different matter: that one is an autostart step, and
`--no-autostart` skips it.

> **Install happens only at `jailbee new`.** Enabling an agent for a
> container that already exists and then running `jailbee apply` attaches the
> mount and widens egress, but never installs the binary — so the autostart
> window fails with exit 127 ("command not found") until the container is
> recreated. Destroy and re-create the container after enabling an agent, or
> install it by hand inside the container.

> **`<agent>` and `install-<agent>` are effectively reserved tmux window
> names.** Both windows are killed and re-created on each run, so an
> `autostart.on_start` step you name `codex` or `install-codex` will have its
> window killed out from under it when the `codex` agent runs. `jailbee
> config validate` catches the `<agent>` collision — a step named the same
> as an autostarting agent is a config error, since with parallel chains
> (see [Stages and chains](config.md#stages-and-chains)) it could kill a
> *running* sibling step, not just an idle window. `install-<agent>` is not
> checked (installs run outside the trigger's own steps) — still pick a
> different name.

### Where the generated launch steps go

Every autostarting agent's launch step (the `exec <command>` window above)
is placed into `on_start` for you — you never write it by hand. In the
**stage form** of `autostart` (see [Stages and chains](config.md#stages-and-chains)),
`jailbee` uses a reserved stage named `agents` to hold them: write one
yourself to control its position, `network`, `mounts` or `detach`, or leave
it out and `jailbee` inserts it at the latest point *within `on_start`*
that still runs before the session is handed to you. That positioning is
moot, though, once `on_create` has already deferred anything: the whole of
`on_start`, agents stage included, then runs after the hand-off regardless
of where it sits or what its own `detach` says. Full rules — what an
explicit `stage: agents` may and may not carry, why it's a no-op under
`on_create`, and the `on_create`-already-detached case — live in
[The reserved `agents` stage](config.md#the-reserved-agents-stage). In the
legacy flat form there is no reserved slot: a step literally named `agents`
is just an ordinary step, and the generated steps are appended after all of
your own `on_start` steps regardless.

Which window an attach (`jailbee tmux`, or `--attach tmux`) actually lands
on depends on whether you have been in the session before:

- **The first attach** picks a window **by name** — the last *autostarting*
  agent (one with `autostart: true`), `claude` sorted last among them — not
  by tmux's own "most recently created" default. So the `agents` stage's
  position only controls *when* the agent's window comes up relative to the
  hand-off, not which window ends up focused once you're in.
- **Every attach after that** lands on the window you detached from. tmux
  tracks that itself; jailbee simply stops overriding it once
  `#{session_last_attached}` says a client has been there. Switch to `codex`,
  detach, and the next `jailbee tmux` puts you back in `codex`.

Restarting the container resets this — the tmux server dies with it, so the
next attach is a first attach again and lands on the agent window.

## 2. Enabling a preset

Most presets need only two lines — the config below is enough to get
`codex` installed and started in the autostart tmux session:

```yaml
agents:
  codex:
    enabled: true
    autostart: true
```

Everything else — the install command, the `~/.codex` shared mount, the
OpenAI egress entries — comes from the preset. `claude` ships
enabled with `autostart: false` by default in the `jailbee config init --global`
template; see [Claude](#9-claude) below for its own switches.

## 3. Presets are starting points

A preset is a base layer, not a fixed answer. Resolution order per agent is:

**preset → (global + repo, already merged)**

Two merges, not three: `global.yaml` and the repo config combine with each
other first, and the preset is merged under that single combined result —
which is what the ordering note below is about. Both steps use the same
[deep-merge](config.md#merge-rules) rules used
everywhere else in `jailbee`'s config: scalars from the later layer win;
lists **append**; an explicit empty list (`egress_allow: []`) **resets** to
empty instead of appending. Resetting is the only operation `deep_merge`
offers besides append — there's no "replace with a different non-empty list"
primitive, so to drop a preset's hosts entirely you set `egress_allow: []`
(at repo layer, ordinarily — see the note below) rather than trying to list a
smaller replacement set.

> **Ordering note.** The presets are merged in *after* the global and repo
> layers have already been combined with each other (`resolve_agents_raw` in
> `config.py` runs once, on the merged global+repo dict). So an
> `egress_allow: []` written only in `global.yaml`, with the repo layer later
> appending its own hosts, does not stick — the repo's non-empty list makes
> the combined global+repo value non-empty again, and that non-empty value
> then *appends onto* the preset instead of replacing it. Put the reset in
> whichever layer has the last word for that agent — usually the repo layer,
> since repo is applied after global in every other case too.

This resolution happens at the raw-dict level, before Pydantic validation, so
a partial override (just one field) validates against the preset's completed
shape rather than failing on missing required fields.

**Worked override — fixing a renamed package.** Say the `gemini` npm package
were renamed upstream. Nothing about the mount, the egress host, or the
command name needs to change — override just the two scalar fields:

```yaml
agents:
  gemini:
    install: "npm i -g @google/gemini-cli-next"
    update: "npm i -g @google/gemini-cli-next@latest"
```

`command`, `shared`, and `egress_allow` still come from the preset unchanged.

**Seeing what actually resolved.** `jailbee config show` prints the merged
`agents:` section — preset fields included, whether or not your own config
mentions them:

```bash
jailbee config show | less   # look for the `agents:` block
```

That's the supported way to answer "what did my preset resolve to" instead
of re-deriving it by hand from `agent_presets.py`.

## 4. Writing your own agent

An agent name that isn't one of the six shipped presets skips the preset
merge entirely — your config is used as-is, no base layer, no forced
append/reset semantics.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. Gates the mount, egress, install/update, and doctor check. |
| `autostart` | bool | `false` | Launch `command` in a background tmux window. Requires `enabled: true` — `jailbee config validate` rejects `autostart: true` with `enabled: false`. |
| `command` | string | `""` | The binary/command line the autostart window execs, and the default source for `install_check`. Required (non-empty) when `enabled: true`. |
| `install` | string \| null | `null` | Shell command line run once at `jailbee new` time when `install_check` fails. |
| `install_check` | string \| null | `null` | Command that decides install-vs-update. Defaults to `command -v <first token of command>` — the binary's bare name, so flags in `command` don't leak into the probe. |
| `update` | string \| null | `null` | Shell command line run at `jailbee new` time when `install_check` succeeds and `auto_update` is true. |
| `auto_update` | bool | `true` | When `false`, an existing install is left untouched; a missing one is still installed. |
| `install_network` | `"strict"` \| `"loose"` | `"strict"` | Network mode for the install/update step only — widen it when the installer's own hosts aren't known (see `grok` below). |
| `shared` | list of `{subpath, path, type, seed, private}` | `[]` | Bind mounts from `<shared_dir>/<subpath>` to `<path>` inside the container. `type: dir` (default) or `type: file`; `seed` (file only) is written once if the target doesn't already exist; `private` (dir only) names subpaths inside the mount that stay per container — see §5. |
| `egress_allow` | list[string] | `[]` | Hosts added to the strict-mode allowlist when this agent is enabled. Same `host[:port]`/CIDR grammar as top-level [`egress_allow`](config.md#egress_allow). |
| `env` | map[string, string] | `{}` | Env vars passed to the install/update step *and* the autostart launch step. |
| `skills_dir` | string \| null | preset | Container-side directory the agent reads user-level skills from (`~/.codex/skills`, …). When set and covered by a `shared` mount, `jailbee new`/`apply` copy the [bundled skills](#10-the-bundled-jailbee-skills) into the shared copy of it. Leave unset for an agent with no skills mechanism. |
| `install_jailbee_skills` | bool | `true` | `false` keeps this agent's shared skills directory untouched by jailbee's bundled skills. Does nothing when `skills_dir` is unset or no `shared` mount covers it. |

A full custom entry:

```yaml
agents:
  my-agent:
    enabled: true
    autostart: true
    command: my-agent
    install: "npm i -g my-agent-cli"
    update: "npm i -g my-agent-cli@latest"
    auto_update: true
    shared:
      - { subpath: my-agent, path: "~/.config/my-agent" }
      - { subpath: my-agent.json, path: "~/.my-agent.json", type: file, seed: "{}\n" }
    egress_allow:
      - api.my-agent.example:443
    env:
      MY_AGENT_HOME: "~/.config/my-agent"
```

`jailbee config validate` enforces a few cross-field rules beyond the schema
itself:

- The agent name must match `[a-z0-9-]+` — it becomes a tmux window name and
  a doctor label. It does *not* become part of any Incus device name: those
  are derived from each `shared[].subpath`. The two coincide for every
  shipped preset only because each preset names its subpath after the agent.
- `enabled: true` requires a non-empty `command`.
- A `shared` subpath may not collide with a built-in shared subdir — the
  `constants.SHARED_SUBDIRS` names `jailbee` itself uses: `caches/pnpm-store`,
  `caches/gradle`, `caches/npm`, `caches/m2`, `docker-registry`, `ssh`.
  (`chrome-pool` is *not* in that list: it is a cache pool root, managed by
  `pool.py` rather than created as a shared subdir, so this guard does not
  cover it.)
- Two agents may share the exact same subpath only if they mount it to the
  same `path`/`type` — a conflicting reuse is rejected.

## 5. Which paths to share

Share the agent's **auth and settings surface only** — never its caches,
histories, or logs, and never a generically-named file.

The `aider` preset is the worked example. Aider writes four things into
`HOME`:

- `~/.aider.conf.yml` — settings. **This is the only one `jailbee` mounts.**
- `~/.aider.input.history`, `~/.aider.chat.history.md` — per-branch working
  state. These default to the working directory anyway, and even if they
  didn't, they belong to one branch's session, not to every container of the
  repo.
- `~/.env` — **must never be mounted**, in this preset or any other you
  write. `.env` is a generic filename: dozens of unrelated tools read a file
  by that exact name in `HOME` or a project root. A shared mount at `~/.env`
  would silently hand every container's copy of some other tool's secrets to
  whichever agent happens to read it, and vice versa — a cross-container leak
  with no relation to the agent you meant to configure.

When you write your own `agents.<name>.shared` list, ask "does this file hold
something I'd lose by re-authenticating, or is it a cache/history/log the
agent would happily regenerate?" Only the former belongs in `shared`.

### A shared directory must not carry a socket

The rule above is about secrets. There is a second one, about control.

`agents.<name>.shared` bind-mounts one host directory into **every**
container of the repo. That is what makes a single login, a single settings
file and a single session history serve every branch. It also means the
directory must not contain an IPC socket, a pid file or a lock:

- A **pathname** AF_UNIX socket is not confined by a network namespace.
  `connect()` resolves the path to an inode, the inode lives in the shared
  mount, and container A therefore reaches a listener running inside
  container B.
- A **PID** means nothing across a PID namespace, so the usual "is the daemon
  still alive?" check reads as true against an unrelated process — or against
  nothing at all.
- Agent daemons take the working directory as a **string**. Every container
  clones the repo to the same path, so the daemon resolves it against its own
  rootfs and edits the wrong checkout.

Codex is the case this was found on. It keeps its app-server control socket
under `$CODEX_HOME` and offers no way to relocate it, so a Codex session
driven from one container edited files and committed in another container's
clone.

Name such subpaths in `private` and each is mounted over with a
per-container directory:

```yaml
agents:
  codex:
    shared:
      - subpath: codex
        path: ~/.codex
        private: [app-server-control, app-server-daemon]
```

`private` is valid on `type: dir` mounts only, and each entry is a relative
path inside the mount. The directories start empty, are never seeded, and are
removed when the container is destroyed. Existing containers pick the
carve-out up on their next `jailbee start` — there is no `jailbee apply` to
run, because the devices are per-container rather than part of the binds
profile.

`jailbee doctor` reports any socket it finds in a shared agent mount that is
not already carved out. The `gemini`, `opencode` and `grok` presets share a
whole home directory too and ship unverified (see §3); that doctor row is
what tells you if one of them grows a daemon.

## 6. Finding an agent's hosts

`jailbee` keeps **no egress-denial log** — a strict-mode ACL drop is silent
at the kernel level, so there is no file to grep for "what got blocked." A
preset's `egress_allow` list is a best-effort starting point, not a promise
that it's complete or minimal. To find the real list for an agent:

```bash
jailbee net loose <container>     # open egress fully
# ... exercise the agent inside the container: log in, run a real task ...
```

Narrow `egress_allow` in your config based on what you observed the agent
actually reach (vendor docs, if it publishes a host list, plus your own
observation — e.g. `tcpdump`/`ss` inside the container while loose), then
confirm it still works:

```bash
jailbee net strict <container>
jailbee apply --no-restart   # push the narrowed egress_allow live
# ... exercise the agent again ...
```

If it breaks under `strict`, you missed a host — go back to `loose` and look
harder rather than guessing at what to add.

## 7. Authentication in a container

A browser-based OAuth or device-code sign-in is awkward inside a container:
the login state usually lives in a path that's per-container by default, so
it's lost on every rebuild unless that path is explicitly shared. Claude
solves this by sharing the `~/.claude` directory (see [Claude](#9-claude)
below) across every container of the repo. Where a vendor also offers a plain
API key, that's the simpler and more portable path — no shared login-state
file needed, and it works the same whether the container was just created or
has been running for weeks.

| Preset | Env var | Notes |
|---|---|---|
| `claude` | — | Browser/device flow; solved via the shared `~/.claude` directory, not an API key. |
| `codex` | `OPENAI_API_KEY` | The ChatGPT login (`codex login`, a device-code flow) works too: the preset allows the hosts it needs — `auth.openai.com` for the code and the token refreshes, `chatgpt.com` for the backend it then talks to. The credentials land in the shared `~/.codex`, so one login covers every container of the repo. |
| `gemini` | `GEMINI_API_KEY` | API-key path only — the OAuth/Code Assist path uses a different set of hosts (see the table below) and has no key. |
| `aider` | provider-dependent (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) | Aider proxies whichever model backend you configure; the key follows that backend, not aider itself. |
| `opencode` | provider-dependent, via `opencode auth login` → `~/.local/share/opencode/auth.json` | |
| `grok` | `XAI_API_KEY` | Verified against vendor docs. A third-party guide claims `GROK_CODE_XAI_API_KEY` instead — the vendor's own spelling wins; this exact discrepancy is why presets are templates, not guarantees. |

Only the `grok` row above was checked directly against vendor documentation
as part of building this feature; the rest follow standard per-vendor
convention and should be treated with the same "untested template" caution
as the rest of that preset.

A key can be passed to the container via `agents.<name>.env`, but don't put
a real secret in a repo's committed `.jailbee/config.yaml` — put it in
`~/.config/jailbee/global.yaml` instead, which is never committed:

```yaml
# ~/.config/jailbee/global.yaml
agents:
  codex:
    env:
      OPENAI_API_KEY: sk-...
```

## 8. The five templates

Package names, config paths, and — especially — host lists in the five
non-`claude` presets are **best-effort**. The maintainer holds no accounts
with any of these vendors; each preset is a documented starting point for
whoever adopts it to correct, not a maintained integration. The override
path in sections 2–4 above is what makes shipping them acceptable.

| Preset | Install | Config paths | Egress | Verification status |
| --- | --- | --- | --- | --- |
| `codex` | `curl -fsSL https://chatgpt.com/codex/install.sh \| CODEX_NON_INTERACTIVE=1 sh` — **not npm** | `~/.codex` (dir — config, auth, sessions, logs, **and the binary**), with `app-server-control/` and `app-server-daemon/` private per container | `api.openai.com:443` (API-key path); `auth.openai.com:443` (device-code sign-in + token refresh); `chatgpt.com:443` (the ChatGPT-plan backend a signed-in CLI talks to, `/backend-api/codex/...`). `install_network: loose` for the installer's own hosts (`chatgpt.com`, `releases.openai.com`, with an `api.github.com` / `github.com` release fallback) | Install verified end-to-end in a container with no Node.js: the binary lands in `~/.local/bin/codex` as a symlink into `~/.codex/packages/standalone/current`. Sign-in hosts are undocumented upstream and were read off a live strict-mode container instead: with `api.openai.com` alone, `codex login` hangs on "Requesting a one-time code..." and ends in `failed to request device code` against `auth.openai.com/api/accounts/deviceauth/usercode`. Telemetry (`ab.chatgpt.com`) is left out on purpose. |
| `gemini` | `npm i -g @google/gemini-cli` | `~/.gemini` (dir) | `generativelanguage.googleapis.com:443` (API-key path), `cloudcode-pa.googleapis.com:443` (OAuth / Code Assist path), `oauth2.googleapis.com:443`, `accounts.google.com:443` | Install + config dir verified; **no authoritative complete host list exists** — upstream issue #4552 is open with no list, and Google's own Code Assist network doc names only `cloudcode-pa.googleapis.com`. |
| `aider` | `uv tool install --with pip aider-chat@latest` | `~/.aider.conf.yml` (**file** type) and nothing else | provider-dependent | Install + config filename + HOME surface verified. |
| `opencode` | `curl -fsSL https://opencode.ai/v2/install \| bash -s -- --no-modify-path` — **not npm** — followed by a `~/.local/bin/opencode` symlink | `~/.opencode` (dir — **the binary**), `~/.config/opencode` (dir), `~/.local/share/opencode` (dir, holds `auth.json`) | `opencode.ai:443` (the built-in "zen" gateway at `/zen/v1/...`, and the version pointer a self-update reads); `models.dev:443` (the model catalogue fetched at startup). **Provider hosts are yours to add** — opencode is a multi-provider client, so which inference host it needs follows the provider you configure, not opencode itself. `install_network: loose` for the installer's own hosts (`opencode.ai`, `registry.npmjs.org`) | Install verified end-to-end against the live installer (v2.0.9): it runs non-interactively, drops a 198MB static binary in `~/.opencode/bin`, and the preset's `~/.local/bin/opencode` link resolves to it; the `~/.local/bin` link, the already-installed short-circuit and the failed-download check are covered by unit tests. Not exercised against a real account — the runtime host list is best-effort like the rest of this table. |
| `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash` — **not npm** | `~/.grok` (dir — `config.toml`, `auth.json`) | `api.x.ai:443` (API-key path); `x.ai:443` (installer); `auth.x.ai:443` (OIDC device-code + refresh); `cli-chat-proxy.grok.com:443` (SuperGrok inference and hosted web_search). `install_network: loose` because the installer's redirect target is undocumented. This list is runtime hosts only — it does not open arbitrary HTTPS for `web_fetch`. | Install + config dir verified against vendor docs. SuperGrok hosts checked against a live device-auth session in a strict-mode container: without the chat proxy, inference retries `https://cli-chat-proxy.grok.com/v1/responses` until it fails. API key env var is `XAI_API_KEY` per vendor docs; a third-party guide claims `GROK_CODE_XAI_API_KEY` — the vendor spelling wins, and that discrepancy is exactly why presets are templates. |

Source of truth for the exact values: `src/jailbee/agent_presets.py`.

### A preset's install command needs a toolchain the image may not have

An `install:` line is just a shell command run inside the container. Nothing
checks that what it invokes exists, and a failed install step is only a
warning `jailbee new` prints once and walks past — so enabling a preset whose
installer is missing its toolchain is a **silent** no-op. The install step
dies with `<tool>: command not found`, and the agent's autostart window then
dies with `<agent>: not found`. The evidence lives in the container, not on
the host:

```bash
jailbee exec <container> -- tmux capture-pane -p -t autostart:install-gemini
```

| Preset | Needs | Which is present when |
| --- | --- | --- |
| `gemini` | `npm` | [`golden.stacks.node`](config.md#stacks-goldenstacks) is on |
| `aider` | `uv` | your own `install.d/` snippet installs it — jailbee's golden image does not ship `uv` |
| `claude`, `codex`, `opencode`, `grok` | nothing | always — each installs a static binary through the vendor's own installer |

For `gemini`, add the stack and rebuild the base image:

```yaml
golden:
  stacks:
    node: true      # or a major version, e.g. 22
```

```bash
jailbee base build
```

`codex` and `opencode` used to be in the npm row and no longer are. If you
added the node stack solely to get one of them working, you can drop it again;
and if a container already has the npm install, remove it (`npm uninstall -g
@openai/codex`, `npm uninstall -g opencode-ai`), because `/etc/profile.d` puts
`~/.npm-global/bin` ahead of `~/.local/bin` and the old copy would shadow the
new one.

### Pinning an install step back to strict

`codex`, `opencode` and `grok` ask for `install_network: loose` because their
installers' hosts are CDN-fronted and rotate their IPs, which is the case the
strict ACL's resolve-at-apply-time pooling handles worst on a first run. To
keep a step strict instead, name the hosts yourself and accept that the first
attempt may need a retry while the pool fills:

```yaml
agents:
  codex:
    install_network: strict
    egress_allow:
      - chatgpt.com:443
      - releases.openai.com:443
  opencode:
    install_network: strict
    egress_allow:
      - opencode.ai:443
      - registry.npmjs.org:443
```

Those entries join the container's runtime allowlist too — `egress_allow`
appends, it has no install-only scope.

## 9. Claude

`agents.claude` is the preferred spelling. A top-level `claude:` block is
still accepted as a **legacy alias** — it's translated into `agents.claude`
at config-load time, before validation. Defining **both** in the same
merged config (global + repo combined) is a `ConfigError` naming both
spellings; pick one, and prefer `agents.claude`.

Claude carries every generic field from the table in
[Writing your own agent](#4-writing-your-own-agent) — `enabled`,
`autostart`, `command`, `install`/`update`, `auto_update`, `install_network`,
`shared`, `egress_allow`, `env`, `skills_dir`, `install_jailbee_skills` —
plus Claude-only fields for its deeper integration (AI-generated PR
descriptions, plugin marketplace egress, onboarding seeding):

- `plugins_enabled`
- `ai_pr_description`
- `ai_pr_branch`
- `pr_prompt`
- `ai_pr_model`
- `ai_pr_timeout`

Full field-by-field descriptions for these live in the
[`claude` section of Configuration reference](config.md#claude) — that
section stays the authoritative reference for the Claude-only fields; this
page covers the generic `agents:` mechanism they sit on top of.

### Shared credential groups (`claude_credentials`)

Several repos on one host can share a single Claude Code login instead of
each holding its own. Configuration is host-level only — see
[`claude_credentials` in the Configuration reference](config.md#claude_credentials)
for the `global.yaml` block, the join/leave flow, and `jailbee doctor`'s
report. This section documents the mechanism the feature rests on.

`CLAUDE_SECURESTORAGE_CONFIG_DIR` is the environment variable jailbee sets
on a member repo's `<prefix>-base` profile: `profiles.claude_securestorage_dir_env`
computes the `(key, value)` pair, and `profiles.base_profile_yaml` is what
renders it into the profile. The container path is `~/.claude-creds`,
bind-mounted from the group's host directory as the `claude-creds` disk
device. The facts below were measured against **Claude Code 2.1.247** by
observing its behavior — none of them are documented by
Anthropic:

- `CLAUDE_SECURESTORAGE_CONFIG_DIR` resolves **both** `.credentials.json`
  and the rotation lock `.oauth_refresh.lock`, independently of
  `CLAUDE_CONFIG_DIR` (which still points at the per-repo `~/.claude`).
  Because the lock travels with the credential, containers of different
  repos rotating the same token stay mutually excluded instead of racing.
- The mount is a **directory**, never a file. Claude Code rewrites
  `.credentials.json` atomically (write new inode, rename over the old),
  so a file-level bind would leave the container holding a handle to the
  old, now-unlinked inode after the first rotation. This is the same
  fragility the earlier `.claude.json` relocation to a directory mount
  removed for the config file.
- An **empty** `CLAUDE_SECURESTORAGE_CONFIG_DIR` is not equivalent to an
  unset one — Claude Code falls back to `~/.claude` for it. jailbee treats
  this as a hard rule: `claude_securestorage_dir_env` returns `None`
  rather than an empty string, and `profiles.base_profile_yaml` drops the
  key outright if a `container.env` override would otherwise render it
  empty.
- Account identity comes from the credential, not from seeding: a member
  repo with a fresh `~/.claude` populates its own `oauthAccount` in
  `.claude.json` the first time Claude Code runs, without jailbee writing
  anything.
- Deleting the `oauthAccount` block from an **established** `.claude.json`
  — one whose onboarding is already complete — is repaired silently at the
  next start. Interactive `claude` (not just `claude -p`) repopulates the
  block from the credential before the first user turn: no login prompt, no
  onboarding flow, and `accountUuid` / `emailAddress` / `organizationUuid`
  come back identical. The repair reads the credential without rotating it
  — `.credentials.json` kept its mtime and size, and no
  `.oauth_refresh.lock` was created — so it cannot disturb the other
  members of a credential group. This is what makes deleting the block a
  safe way to point a member repo at a changed group account.
- A **stale** `oauthAccount` in `.claude.json` does not break
  authentication, and jailbee does not correct it — only the credential
  in `CLAUDE_SECURESTORAGE_CONFIG_DIR` authenticates.
- Only the credential is shared. Each repo keeps its own `~/.claude`, so
  project history, MCP config, sessions and onboarding state never cross
  repos.

## 10. The bundled jailbee skills

jailbee ships three skills — `jailbee-usage` (day-to-day commands),
`jailbee-repo-setup` (first-time repo configuration), `jailbee-pr-review`
(publishing an in-container agent's staged review comments) — and installs
them for every enabled agent that has a skills mechanism, not just Claude:

| Agent | Skills directory (in-container) | Shared subpath it lands under |
|---|---|---|
| `claude` | `~/.claude/skills` | `claude` |
| `codex` | `~/.codex/skills` | `codex` |
| `gemini` | `~/.gemini/skills` | `gemini` |
| `opencode` | `~/.config/opencode/skills` | `opencode-config` |
| `aider`, `grok` | — (no skills mechanism) | — |

The copy happens on the *host* side, into `<shared_dir>/<subpath>/skills/`:
each agent's config home is already a shared bind mount, and `raw.idmap` is
1:1, so one host-side copy is visible in every container of the repo — no
`incus exec`, no per-container work. `jailbee new` and `jailbee apply` both
run it, so a jailbee upgrade reaches existing containers on the next
`apply`.

Two per-agent fields govern it (both in the
[§4 table](#4-writing-your-own-agent)): `skills_dir` names the
container-side directory (the presets set it for the four agents above;
set it yourself on a from-scratch agent whose mount layout differs), and
`install_jailbee_skills: false` opts one agent out. A `skills_dir` that no
`shared` mount covers is a config mistake: `jailbee new` warns and skips
that agent rather than failing.

The agents' own compatibility is what makes this one table: all four read
the same `SKILL.md` frontmatter format, and opencode additionally scans
Claude-compatible `~/.claude/skills` — jailbee still writes each agent's
own directory, so the skills survive an agent being disabled or removed.

For the *host's* own agents (not the containers), the same skills are
opt-in: see [`install_host_skills`](config.md#install_host_skills) in the
global config. The pre-1.0 key `claude.install_gie_skills` was retired in
1.1.0: a config still using it fails to load with an error naming
`install_jailbee_skills`.
