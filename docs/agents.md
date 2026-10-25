# Generic agent support

`jailbee` wires terminal coding agents (Claude Code, and a handful of others)
into the container lifecycle through one declarative key: `agents:`. It is a
mapping keyed by agent name — `{codex: {...}, gemini: {...}}` — rather than a
list, because the deep-merge pipeline that combines `~/.config/jailbee/global.yaml`,
`<repo>/.jailbee/config.yaml` and the host-local
`~/.config/jailbee/repos/<container_prefix>.yaml` *appends* lists (see
[Merge rules](config.md#merge-rules)). As a list, a global entry and a repo
entry for the same agent would produce two duplicate entries instead of one
adjusted one; as a mapping, the repo layer can tweak a single field of an
agent the global layer already defined.

Six presets ship built in (`claude`, `codex`, `gemini`, `aider`, `opencode`,
`grok`), and you can define an agent that isn't one of them from scratch.
**Claude is the recommended integration.** The other five are best-effort
templates with varying verification coverage, from vendor-document checks
to live install and sign-in probes — see
[The five templates](#8-the-five-templates) below.

> **Recommended setup.** Run Claude Code (the `claude` preset). To use models or
> subscriptions other than Anthropic's, keep Claude Code and point it at them
> through the LiteLLM proxy with `claude-jb` — see
> [Claude Code through LiteLLM](litellm.md). That path is tested with both a
> ChatGPT (Codex) and a SuperGrok subscription. The other presets are for
> when you specifically want another vendor's agent and are ready to correct
> its template.

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
  `jailbee start`). The `install-<agent>` tmux step is bounded by
  `autostart.step_timeout` (default 600s); the preceding `install_check` and
  shared-install lock wait are not covered by that timeout. Successful
  steps close their windows. Failed steps normally retain their output for
  inspection with `jailbee tmux <container>` (`remain-on-exit failed`
  requires a compatible tmux version).
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
container's autostart tmux session itself when an install/update step needs
it. A running or failed `install-<agent>` window may therefore exist even
with `--no-autostart`; a successful step closes its window. The agent's own
launch window is a different matter: that one is an autostart step, and
`--no-autostart` skips it.

> **Install happens only at `jailbee new`.** Enabling an agent and running
> `jailbee apply` updates its mounts and egress configuration, subject to the
> normal restart requirements, but does not run its installer. If the binary
> is absent, the autostart launch will fail. Install it manually inside the
> existing container, or preserve your work and create a replacement; do not
> destroy a container just to retry installation.

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
is just an ordinary step. Generated steps normally follow your own
`on_start` steps, but `--no-wait` or an already-deferred `on_create` changes
the insertion point to the computed hand-off boundary.

Which window an attach (`jailbee tmux`, or `--attach tmux`) actually lands
on depends on whether you have been in the session before:

- **The first attach** tries to select a window **by name** — the last
  *autostarting* agent (one with `autostart: true`), `claude` sorted last
  among them. If that window does not exist yet or its launch failed,
  attach falls back to tmux's current window. Stage position controls when
  the window becomes available, not the preferred agent's name.
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

**preset → ((global + repo) + host-local, already merged)**

The global and repo configurations combine first, then the host-local
`~/.config/jailbee/repos/<container_prefix>.yaml` overlay is applied. Finally,
`resolve_agents_raw` in `config/loader.py` merges each preset under that
combined result. Agent lists follow the ordinary
[deep-merge](config.md#merge-rules) rules: scalars from the later layer win;
lists **append**; an explicit empty list (`egress_allow: []`) **resets** to
empty instead of appending. There is no replacement operation for a
non-empty list, so a shorter list does not remove a preset's hosts.

> **Ordering note.** A reset must survive into the combined user
> configuration. If global sets `egress_allow: []` but repo adds hosts, the
> result is non-empty and is then appended to the preset's hosts. Likewise,
> a host-local addition can undo a repo-level reset. Put `[]` in the last
> layer that defines this list if you want to discard the preset's entries.

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

An agent name that isn't one of the six shipped presets receives no preset
base. Ordinary global/repo/host-local deep-merge rules, including list
append/reset behavior, still apply.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. Gates the mount, egress, install/update, and doctor check. |
| `autostart` | bool | `false` | Launch `command` in a background tmux window. Requires `enabled: true` — `jailbee config validate` rejects `autostart: true` with `enabled: false`. |
| `command` | string | `""` | The binary/command line the autostart window execs, and the default source for `install_check`. Required (non-empty) when `enabled: true`. |
| `install` | string \| null | `null` | Shell command line run once at `jailbee new` time when `install_check` fails. |
| `install_check` | string \| null | `null` | Command that decides install-vs-update. Defaults to `command -v <first token of command>` — the binary's bare name, so flags in `command` don't leak into the probe. |
| `update` | string \| null | `null` | Shell command line run at `jailbee new` time when `install_check` succeeds and `auto_update` is true. |
| `auto_update` | bool | `true` | When `false`, an existing install is left untouched; a missing one is still installed. |
| `install_network` | `"strict"` \| `"loose"` | `"strict"` | `loose` temporarily widens networking for the install/update step. `strict` leaves the container's current mode unchanged; it does not force a loose container into strict mode. |
| `shared` | list of `{subpath, path, type, seed, private}` | `[]` | Bind mounts from `<shared_dir>/<subpath>` to `<path>` inside the container. `type: dir` (default) or `type: file`; `seed` (file only) is written once if the target doesn't already exist; `private` (dir only) names subpaths inside the mount that stay per container — see §5. |
| `egress_allow` | list[string] | `[]` | Hosts added to the strict-mode allowlist when this agent is enabled. Same `host[:port]`/CIDR grammar as top-level [`egress_allow`](config.md#egress_allow). |
| `env` | map[string, string] | `{}` | Literal values passed to the install/update step and autostart launch, not to ordinary shells or the PR headless invocation. No `~` or `$HOME` expansion; values are not masked by `config show`. |
| `prompt_args` | string \| null | preset | Arguments appended to `command` (one space) to start the agent with an initial prompt from `jailbee new --prompt`. The prompt text is in the file `$JAILBEE_INITIAL_PROMPT_FILE`; read it from the environment, never interpolate it. Presets set it for `claude`, `codex` and `opencode`; unset means the agent cannot take an initial prompt. See [Primary agent and initial prompt](#primary-agent-and-initial-prompt). |
| `headless` | string \| null | preset | One-shot command line that runs the agent once and prints its answer — what [`jailbee pr`](config.md#pr) uses to write PR text. Run in a `bash -lc` login shell in the repo directory; the prompt is in `$JAILBEE_PR_PROMPT` and the model (empty when none applies) in `$JAILBEE_PR_MODEL` — read both from the environment, never interpolate them. `$JAILBEE_PR_SESSION` holds a fresh UUID the agent may take as its session id, so a timed-out run's transcript can be found. Presets set it for `claude`, `codex`, `gemini` and `opencode`; leave unset for an agent with no one-shot mode, and `pr.agent: auto` skips it. |
| `skills_dir` | string \| null | preset | Container-side directory the agent reads user-level skills from (`~/.codex/skills`, …). When set and covered by a `shared` mount, `jailbee new`/`apply` copy the [bundled skills](#10-the-bundled-jailbee-skills) into the shared copy of it. Leave unset for an agent with no skills mechanism. |
| `global_instructions` | `{dir, file}` \| null | preset | Where the agent reads host-wide instructions; `dir` is mounted read-only and must not overlap a `shared` mount in either direction, nor be `/`, `/etc`, `/usr`, `/home` or the home directory. `dir` is absolute or `~`-relative without `.`/`..` segments; `file` is a bare file name. Neither may contain a NUL byte. Only Claude's preset sets it today. |
| `install_jailbee_skills` | bool | `true` | `false` keeps this agent's shared skills directory untouched by jailbee's bundled skills. Does nothing when `skills_dir` is unset or no `shared` mount covers it. A disabled agent gets nothing either way. |

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
      MY_AGENT_HOME: "/home/dev/.config/my-agent"
```

`jailbee config validate` enforces a few cross-field rules beyond the schema
itself:

- The agent name must match `[a-z0-9-]+` — it becomes a tmux window name and
  a doctor label. It does *not* become part of any Incus device name: those
  are derived from each `shared[].subpath`. Some presets use the agent name
  as a subpath; others declare separate install, config or skills subpaths.
- `enabled: true` requires a non-empty `command`.
- A `shared` subpath may not collide with a built-in shared subdir — the
  `constants.SHARED_SUBDIRS` names `jailbee` itself uses: `caches/pnpm-store`,
  `caches/gradle`, `caches/npm`, `caches/m2`, `docker-registry`, `ssh`.
  (`chrome-pool` is *not* in that list: it is a cache pool root, managed by
  `pool.py` rather than created as a shared subdir, so this guard does not
  cover it.)
- Two agents may share the exact same subpath only if they mount it to the
  same `path`/`type` — a conflicting reuse is rejected.

### Primary agent and initial prompt

The top-level [`primary_agent`](config.md#primary_agent) names the agent that
gets `jailbee new --prompt FILE|-` / `jailbee fork --prompt FILE|-`. With `auto`
it is the first enabled agent: `autostart` ones first, then the rest; `claude`
first, then by name. An explicit name must be an enabled agent, else the problem
is reported and nothing falls back. An explicit name also steers `pr.agent: auto`.

The agent's `prompt_args` are appended to its `command` with one space; the
prompt is read from `$JAILBEE_INITIAL_PROMPT_FILE`, never interpolated. Without
`prompt_args` the agent cannot take a prompt and `--prompt` exits 2. The preset
values pass the prompt as a bare argument, so a prompt whose first character is
`-` may be parsed as an option by the agent CLI and then lost: start prompts with
text, not `-` or `---`.

- The prompt must be non-empty and at most 65536 bytes. `--prompt -` reads all
  of stdin, so for an existing-branch `jailbee new` also pass `-y` (the confirm
  prompt would hit EOF).
- It is stored in the container as `/home/dev/.jailbee/initial-prompt.md`, label
  `user.jailbee.initial_prompt` = `pending`. When the agent launches, the launch step
  renames the file to `initial-prompt.consumed.md`; the label flips to `consumed`
  lazily, on the next autostart run that finds the file gone.
- A pending prompt launches the primary agent's window even if its `autostart`
  is off. `--prompt` conflicts with `--no-autostart` (exit 2).
- A restart after consumption relaunches the plain `command`.

## 5. Which paths to share

Prefer sharing the agent's **auth and settings surface only**, rather than
caches, histories or logs, and avoid generically named files. Some shipped
presets share an entire agent home, including history and logs; those files
are consequently readable and writable by every container using that mount.
Treat this as deliberate shared state, not an isolation boundary.

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

A login is worth sharing only when the agent keeps it apart from per-session
state. opencode does not: its sessions, auth tokens included, live in one
SQLite database under `~/.config/opencode` / `~/.local/share/opencode`, so the
preset shares neither (only its skills directory) and each container logs in
on its own. A shared database would also have several containers writing one
SQLite file through a bind mount.

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

`jailbee doctor` checks for socket files visible on the host in enabled
agents' shared directories, excluding paths configured as `private`. It
does not detect PID/lock files or verify that an existing container has
actually acquired its private mounts. A clean check is not proof of runtime
isolation: restart existing containers through jailbee after adding a
carve-out. The `gemini` and `grok` presets also share their agent config homes;
check their runtime state when adopting or upgrading them.

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

If it works in `loose` but fails in `strict`, investigate the denied
connection rather than guessing at more hosts. Missing hosts, stale DNS/IP
pools, ports and proxy behavior can all matter; a retry alone does not
refresh the allowlist. Use the [troubleshooting guide](troubleshooting.md)
for diagnostics.

## 7. Authentication in a container

A browser-based OAuth or device-code sign-in is awkward inside a container:
the login state usually lives in a path that's per-container by default, so
it's lost on rebuild unless that path is explicitly shared. Claude's
integration shares the repo's config home and, when a credential group is
selected, uses a separate shared credential directory (see
[Claude](#9-claude) below). Where a vendor offers an API key, that can avoid a
browser sign-in, but the key still needs secure storage and delivery to the
specific process that uses it.

| Preset | Env var | Notes |
|---|---|---|
| `claude` | — (shared-login setup) | This integration shares the saved login: repo-local in `~/.claude`, or in `~/.claude-creds` when a credential group is selected. See [Claude](#9-claude) and [LiteLLM](litellm.md) for the supported wiring. |
| `codex` | `OPENAI_API_KEY` | The ChatGPT login (`codex login`, a device-code flow) works too: the preset allows the hosts it needs — `auth.openai.com` for the code and the token refreshes, `chatgpt.com` for the backend it then talks to. The credentials land in the shared `~/.codex`, so one login covers every container of the repo. |
| `gemini` | `GEMINI_API_KEY` | API-key option. The preset also includes best-effort OAuth/Code Assist hosts (see the table below); their presence does not guarantee sign-in works with every version. |
| `aider` | provider-dependent (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) | Aider proxies whichever model backend you configure; the key follows that backend, not aider itself. |
| `opencode` | provider-dependent, via `opencode auth login` | **Per container, not shared.** opencode keeps its whole session, auth tokens included, in a SQLite database in its config/data homes, so sharing them would hand every container the others' credentials. Each container configures and logs in on its own. To run ChatGPT or other providers' models across containers with one login, use `claude-jb` ([Claude Code through LiteLLM](litellm.md)) instead. |
| `grok` | `XAI_API_KEY` | Verified against vendor docs. A third-party guide claims `GROK_CODE_XAI_API_KEY` instead — the vendor's own spelling wins; this exact discrepancy is why presets are templates, not guarantees. |

Authentication support varies by vendor version. The verification notes
below distinguish vendor-document checks from recorded live probes; neither
is a complete current compatibility test.

A key can be passed to an agent's install/update and autostart commands via
`agents.<name>.env`. Do not put a real secret in a committed repo config.
Use a host-only file such as `~/.config/jailbee/global.yaml` or the per-repo
`~/.config/jailbee/repos/<container_prefix>.yaml`, protect its file permissions,
and keep it out of version control. These values are **not masked by
`jailbee config show`**. They are not added to ordinary container shells or
the `jailbee pr` headless invocation; those need their own authentication
source, such as the agent's saved login.

```yaml
# ~/.config/jailbee/global.yaml
agents:
  codex:
    env:
      OPENAI_API_KEY: sk-...
```

## 8. The five templates

Package names, config paths, and — especially — host lists in the five
non-`claude` presets are **best-effort**. The checks below record what was
verified when each template was added or updated; they do not guarantee
compatibility with later vendor releases. Use the override mechanism in
sections 2–4 when your agent version needs different commands or hosts.

| Preset | Install | Config paths | Egress | Verification status |
| --- | --- | --- | --- | --- |
| `codex` | `curl -fsSL https://chatgpt.com/codex/install.sh \| CODEX_NON_INTERACTIVE=1 sh` — **not npm** | `~/.codex` (dir — config, auth, sessions, logs, **and the binary**), with `app-server-control/` and `app-server-daemon/` private per container | `api.openai.com:443` (API-key path); `auth.openai.com:443` (device-code sign-in + token refresh); `chatgpt.com:443` (the ChatGPT-plan backend a signed-in CLI talks to, `/backend-api/codex/...`). `install_network: loose` for the installer's own hosts (`chatgpt.com`, `releases.openai.com`, with an `api.github.com` / `github.com` release fallback) | Install verified end-to-end in a container with no Node.js: the binary lands in `~/.local/bin/codex` as a symlink into `~/.codex/packages/standalone/current`. Sign-in hosts are undocumented upstream and were read off a live strict-mode container instead: with `api.openai.com` alone, `codex login` hangs on "Requesting a one-time code..." and ends in `failed to request device code` against `auth.openai.com/api/accounts/deviceauth/usercode`. Telemetry (`ab.chatgpt.com`) is left out on purpose. |
| `gemini` | `npm i -g @google/gemini-cli` | `~/.gemini` (dir) | `generativelanguage.googleapis.com:443` (API-key path), `cloudcode-pa.googleapis.com:443` (OAuth / Code Assist path), `oauth2.googleapis.com:443`, `accounts.google.com:443`, `www.googleapis.com:443` (the account lookup after sign-in) | Install + config dir checked when the template was added; no authoritative complete host list was found in that review. Upstream issue #4552 was open without a list, and the reviewed Code Assist network doc named only `cloudcode-pa.googleapis.com`. |
| `aider` | `uv tool install --with pip aider-chat@latest` | `~/.aider.conf.yml` (**file** type) and nothing else | provider-dependent | Install + config filename + HOME surface verified. |
| `opencode` | `curl -fsSL https://opencode.ai/v2/install \| bash -s -- --no-modify-path` — **not npm** — followed by a `~/.local/bin/opencode` symlink | `~/.opencode` (dir — **the binary**) and `~/.config/opencode/skills` (dir) are shared; `~/.config/opencode` and `~/.local/share/opencode` are deliberately **not** — they hold the session database, auth tokens included, so each container keeps its own | `opencode.ai:443` (the built-in "zen" gateway at `/zen/v1/...`, and the version pointer a self-update reads); `models.opencode.ai:443` (the model catalogue fetched at startup; `models.dev:443`, which served it before, for older builds). **Provider hosts are yours to add** — opencode is a multi-provider client, so which inference host it needs follows the provider you configure, not opencode itself. `install_network: loose` for the installer's own hosts (`opencode.ai`, `registry.npmjs.org`) | Install verified end-to-end against the live installer (v2.0.9): it runs non-interactively, drops a 198MB static binary in `~/.opencode/bin`, and the preset's `~/.local/bin/opencode` link resolves to it; the `~/.local/bin` link, the already-installed short-circuit and the failed-download check are covered by unit tests. Used with a real account, which is how the shared session database was found to carry auth tokens; the runtime host list is still best-effort like the rest of this table. |
| `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash` — **not npm** | `~/.grok` (dir — `config.toml`, `auth.json`) | `api.x.ai:443` (API-key path); `x.ai:443` (installer); `auth.x.ai:443` (OIDC device-code + refresh); `cli-chat-proxy.grok.com:443` (SuperGrok inference and hosted web_search). `install_network: loose` because the installer's redirect target is undocumented. This list is runtime hosts only — it does not open arbitrary HTTPS for `web_fetch`. | Install + config dir verified against vendor docs. SuperGrok hosts checked against a live device-auth session in a strict-mode container: without the chat proxy, inference retries `https://cli-chat-proxy.grok.com/v1/responses` until it fails. API key env var is `XAI_API_KEY` per vendor docs; a third-party guide claims `GROK_CODE_XAI_API_KEY` — the vendor spelling wins, and that discrepancy is exactly why presets are templates. |

Source of truth for the exact values: `src/jailbee/agent_presets.py`.

### A preset's install command needs a toolchain the image may not have

An `install:` line is a shell command run inside the container, without a
separate toolchain prerequisite check. A failed install is non-fatal:
`jailbee new` warns on the host and continues. A missing tool can therefore
leave an otherwise created container without its agent; autostart may fail
too. Inspect the failed tmux window for detailed installer output:

```bash
jailbee exec <container> -- tmux capture-pane -p -t autostart:install-gemini
```

| Preset | Needs | Which is present when |
| --- | --- | --- |
| `gemini` | `npm` | [`golden.stacks.node`](config.md#stacks-goldenstacks) is on |
| `aider` | `uv` | your own `install.d/` snippet installs it — jailbee's golden image does not ship `uv` |
| `claude`, `codex`, `opencode`, `grok` | vendor installer, curl and shell | no Node.js/npm/uv requirement in the preset command; the standard golden image supplies curl and the shells, but vendor-specific platform requirements still apply |

For `gemini`, add the stack and rebuild the base image:

```yaml
golden:
  stacks:
    node: true      # or a major version, e.g. 22
```

```bash
jailbee base build
```

Then create a new container from that image. Rebuilding the base does not
add Node.js to existing containers; install the prerequisite there manually
if you need to keep using them.

`codex` and `opencode` used to be in the npm row and no longer are. If you
added the node stack solely to get one of them working, you can drop it again;
and if a container already has the npm install, remove it (`npm uninstall -g
@openai/codex`, `npm uninstall -g opencode-ai`), because `/etc/profile.d` puts
`~/.npm-global/bin` ahead of `~/.local/bin` and the old copy would shadow the
new one.

### Pinning an install step back to strict

`codex`, `opencode` and `grok` default to `install_network: loose` so their
installers are not limited to the preset's runtime hosts and currently
resolved addresses. This widens networking for the whole container during
the step, not just for the installer process.

`install_network: strict` disables that temporary override; it **preserves
the container's current mode**, rather than forcing strict mode. To install
under strict restrictions, create the container in strict mode and configure
all required installer hosts first. The following are starting points, not
complete host lists; redirects or DNS changes may require additions and an
allowlist refresh, not merely an installer retry:

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
configuration (global, repo and host-local layers) is a `ConfigError` naming both
spellings; pick one, and prefer `agents.claude`.

Claude carries every generic field from the table in
[Writing your own agent](#4-writing-your-own-agent) — `enabled`,
`autostart`, `command`, `install`/`update`, `auto_update`, `install_network`,
`shared`, `egress_allow`, `env`, `headless`, `prompt_args`, `skills_dir`,
`install_jailbee_skills`, `global_instructions` — plus Claude-only fields for its deeper integration (plugin marketplace
egress, agent view, onboarding seeding):

- `plugins_enabled`
- `agent_view`
- `seed_onboarding`

The AI-written PR descriptions are not Claude-only any more: they are the
agent-neutral [`pr:` block](config.md#pr), whose `pr.agent` picks the agent.
`auto` prefers enabled autostart agents with a `headless` command, then other
enabled headless-capable agents; within each set Claude comes first, then
other names alphabetically. Run `jailbee pr` on the host: the AI command
runs inside the container's repo directory, while GitHub publication uses
the host's `gh` credentials. Selecting `pr.agent: claude-jb` runs Claude's
headless command through the LiteLLM wrapper.

Full field-by-field descriptions for these live in the
[`claude` section of Configuration reference](config.md#claude) — that
section stays the authoritative reference for the Claude-only fields; this
page covers the generic `agents:` mechanism they sit on top of.

Claude's preset also declares `global_instructions: {dir: /etc/claude-code,
file: CLAUDE.md}`. The host's `~/.config/jailbee/AGENTS.md` reaches every repo's
containers there as read-only managed-policy memory, before the shared and
project CLAUDE.md files. Edits reach new sessions after a successful refresh,
not sessions already running; a failed host-file read leaves the previous
staged copy in place. See [Agent-wide instructions](config.md#agent-wide-instructions-configjailbeeagentsmd)
for first-time apply, refresh commands and the host-wide opt-out.

### What the containers of a repo share

With Claude enabled and the default mount layout, each container of a repo
mounts the same `<shared_dir>/claude` as `~/.claude`. Memory
(`projects/<path>/memory/`), transcripts and `/resume` across containers,
settings, `CLAUDE.md`, skills and plugins are shared. The default
`CLAUDE_CONFIG_DIR` also places `.claude.json` inside this shared directory.
Three subdirectories are **per container**, mounted over the shared ones
from `<shared_dir>/.private/<container>/claude/`:

- `sessions/` — Claude Code's registry of running sessions, which peer
  messaging and the dashboard's AGENT column read;
- `daemon/`, `jobs/` — the agent-view daemon's roster, dispatch queue and
  background-job state.

Shared, these let one container's daemon adopt, run twice or declare dead
another container's jobs. The daemon's top-level `daemon.lock` cannot be made
per container (it is a file), so **agent view is off by default**:
containers get `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, which turns off
`claude agents`, `--bg`, `/background` and the daemon. Set
`agents.claude.agent_view: true` to keep it; daemons in two running
containers of the repo will still contend for the lock, and `claude
--continue` in one container may pick a session running in the background in
another.

The per-container mounts land when a container starts, so after upgrading run
`jailbee apply` and then restart each running container through jailbee
(`jailbee restart`, or `jailbee stop` then `jailbee start`). A plain
`incus restart` never runs the mount attach. Until a container is restarted
the dashboard's AGENT column shows `—` for it.

The old shared `sessions/`, `daemon/` and `jobs/` directories under
`<shared_dir>/claude/` are left in place — a container not yet restarted
still uses them — and can be deleted once every container of the repo has
been restarted. The top-level `daemon.lock`, `daemon.status.json` and
`daemon.log` can be deleted only when `agent_view` is `false` (or no daemon is
running): with `agent_view: true` they are still live and shared.

### Shared credential groups (`credentials`)

Several repos on one host can share a single login per agent instead of
each holding its own. Configuration is host-level only, never committed — see
[`credentials` in the Configuration reference](config.md#credentials)
for the `global.yaml` default, the per-repo choice in the host-local file,
the join/leave flow, and `jailbee doctor`'s report. One group name is shared by every enabled agent, but each agent keeps
its own credential in the group; the mechanism below is Claude's, and is what
the feature rests on today.

`CLAUDE_SECURESTORAGE_CONFIG_DIR` is the environment variable jailbee sets
on a member repo's `<prefix>-base` profile. `ClaudeAdapter.wiring` in
`accounts/adapters/claude.py` returns it, together with the `claude-creds`
disk device that bind-mounts the group's host directory at the container
path `~/.claude-creds`. The two halves land in different profiles:
`profiles.base_profile_yaml` renders every pooled adapter's `Wiring.env`
into `<prefix>-base`, and `profiles.binds_profile_yaml` its
`Wiring.devices` into `<prefix>-binds`. Both are the adapter's to name, not
the profile renderer's — a second pooled agent declares its own wiring
rather than adding a branch to `profiles.py`. The facts below were measured
against **Claude Code 2.1.247** by observing its behavior — none of them are
documented by Anthropic:

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
  this as a hard rule: `ClaudeAdapter.wiring` omits the key from its
  `Wiring.env` rather than returning an empty value, and
  `profiles.base_profile_yaml` drops the key outright if a `container.env`
  override would otherwise render it empty.
- Account identity comes from the credential: in the observed Claude
  version, a member repo with a fresh `~/.claude` populates its own
  `oauthAccount` in `.claude.json` when Claude Code runs. Jailbee does not
  seed that identity block. It does, by default, seed onboarding completion
  and repo trust when the resolved credential file exists; existence is not
  a validity check. Set `seed_onboarding: false` to disable that seeding.
- Deleting the `oauthAccount` block from an **established** `.claude.json`
  — one whose onboarding is already complete — is repaired silently at the
  next start. Interactive `claude` (not just `claude -p`) repopulates the
  block from the credential before the first user turn: no login prompt, no
  onboarding flow, and `accountUuid` / `emailAddress` / `organizationUuid`
  came back identical in that probe. The observed repair did not rotate the
  credential: `.credentials.json` kept its mtime and size, and no
  `.oauth_refresh.lock` was created. Jailbee invalidates this identity block
  when switching pooled accounts, but the vendor's repair behavior is a
  version-specific observation, not a guaranteed API.
- A **stale** `oauthAccount` in `.claude.json` does not break
  authentication, and jailbee does not correct it — only the credential
  in `CLAUDE_SECURESTORAGE_CONFIG_DIR` authenticates.
- The group shares the credential directory, not the whole Claude config
  home. With separate repo shared directories and the default mount layout,
  each repo keeps its own history, MCP config and onboarding state. Running
  session registries are per container once its private mounts are attached
  (see above); custom mounts and environment overrides can change this.

## 10. The bundled jailbee skills

jailbee ships seven skills — `jailbee-usage` (day-to-day commands),
`jailbee-repo-setup` (first-time repo configuration), `jailbee-pr-review`
(staging PR review comments, replies and description rewrites),
`jailbee-issue-management` (staging GitHub issue actions),
`jailbee-bug-report` (staging a bug report against jailbee itself),
`jailbee-orchestration` (an orchestrator agent starting and managing child
containers) and `jailbee-orchestration-child` (a child agent working under an
orchestrator; see [orchestration](orchestration.md#skills)) — and installs
them for every enabled agent that has a skills mechanism, not just Claude:

| Agent | Skills directory (in-container) | Shared subpath it lands under |
|---|---|---|
| `claude` | `~/.claude/skills` | `claude` |
| `codex` | `~/.codex/skills` | `codex` |
| `gemini` | `~/.gemini/skills` | `gemini` |
| `opencode` | `~/.config/opencode/skills` | `opencode-skills` (the skills directory itself) |
| `aider`, `grok` | — (no skills mechanism) | — |

The copy happens on the *host* side at the shared-mount location corresponding
to `skills_dir`: usually `<shared_dir>/<subpath>/skills/`, but directly in
`<shared_dir>/opencode-skills/` for OpenCode. One host-side copy is visible
to every container using that mount — no `incus exec` or per-container copy.
`jailbee new` and `jailbee apply` both synchronize it, so a successful sync
updates the files visible to existing containers.

Sync replaces each bundled skill directory wholesale, preserving unrelated
skills. Keep customizations in separately named skills; edits inside a
bundled skill are overwritten. Opting out stops copies through that agent,
but does not remove existing copies or prevent another enabled agent sharing
the same destination from updating them.

Two per-agent fields govern it (both in the
[§4 table](#4-writing-your-own-agent)): `skills_dir` names the
container-side directory (the presets set it for the four agents above;
set it yourself on a from-scratch agent whose mount layout differs), and
`install_jailbee_skills: false` opts one agent out. Both spellings a mount
path accepts work on either side — `~/.mine/skills` is covered by a mount
on `/home/dev/.mine` and the other way round. A `skills_dir` that no
`shared` mount covers is a config mistake: `jailbee new` warns and skips
that agent rather than failing. So is one that falls inside a
[`private`](#a-shared-directory-must-not-carry-a-socket) carve-out — the
per-container directory mounted over it would hide the copy from every
agent, so that too is warned and skipped.

The agents' own compatibility is what makes this one table: all four read
the same `SKILL.md` frontmatter format, and opencode additionally scans
Claude-compatible `~/.claude/skills` — jailbee still writes each agent's
own directory, so the skills survive an agent being disabled or removed.

For the *host's* own agents (not the containers), the same skills are
opt-in: see [`install_host_skills`](config.md#install_host_skills) in the
global config. The pre-1.0 key `claude.install_gie_skills` was retired in
1.1.0: a config still using it fails to load with an error naming
`install_jailbee_skills`.
