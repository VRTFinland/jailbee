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
  `jailbee start`).
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

> **Install happens only at `jailbee new`.** Enabling an agent for a
> container that already exists and then running `jailbee apply` attaches the
> mount and widens egress, but never installs the binary — so the autostart
> window fails with exit 127 ("command not found") until the container is
> recreated. Destroy and re-create the container after enabling an agent, or
> install it by hand inside the container.

> **`<agent>` and `install-<agent>` are effectively reserved tmux window
> names.** Both windows are killed and re-created on each run, so an
> `autostart.on_start` step you name `codex` or `install-codex` will have its
> window killed out from under it when the `codex` agent runs. Nothing checks
> for the collision — pick a different step name.

## 2. Enabling a preset

Most presets need only two lines — the config below is enough to get
`codex` installed and started in the autostart tmux session:

```yaml
agents:
  codex:
    enabled: true
    autostart: true
```

Everything else — the npm install command, the `~/.codex` shared mount, the
`api.openai.com:443` egress entry — comes from the preset. `claude` ships
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

**Worked override — fixing a renamed package.** Say the `codex` npm package
were renamed upstream. Nothing about the mount, the egress host, or the
command name needs to change — override just the two scalar fields:

```yaml
agents:
  codex:
    install: "npm i -g @openai/codex-cli"
    update: "npm i -g @openai/codex-cli@latest"
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
| `shared` | list of `{subpath, path, type, seed}` | `[]` | Bind mounts from `<shared_dir>/<subpath>` to `<path>` inside the container. `type: dir` (default) or `type: file`; `seed` (file only) is written once if the target doesn't already exist. |
| `egress_allow` | list[string] | `[]` | Hosts added to the strict-mode allowlist when this agent is enabled. Same `host[:port]`/CIDR grammar as top-level [`egress_allow`](config.md#egress_allow). |
| `env` | map[string, string] | `{}` | Env vars passed to the install/update step *and* the autostart launch step. |

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
- A `shared` subpath may not collide with a built-in shared subdir (the
  `caches/*`, `chrome-pool/*`, `docker-registry`, `ssh` names `jailbee`
  itself uses).
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
| `codex` | `OPENAI_API_KEY` | The ChatGPT-login sign-in hosts are undocumented upstream; the API-key path is the one with a documented host list. |
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
| `codex` | `npm i -g @openai/codex` (Node ≥ 22) | `~/.codex` (dir — config, auth, sessions, logs) | `api.openai.com:443` | Install + config dir verified against vendor docs; the ChatGPT-login sign-in hosts are **undocumented** upstream, so the API-key path is the documented one. |
| `gemini` | `npm i -g @google/gemini-cli` | `~/.gemini` (dir) | `generativelanguage.googleapis.com:443` (API-key path), `cloudcode-pa.googleapis.com:443` (OAuth / Code Assist path), `oauth2.googleapis.com:443`, `accounts.google.com:443` | Install + config dir verified; **no authoritative complete host list exists** — upstream issue #4552 is open with no list, and Google's own Code Assist network doc names only `cloudcode-pa.googleapis.com`. |
| `aider` | `uv tool install --with pip aider-chat@latest` | `~/.aider.conf.yml` (**file** type) and nothing else | provider-dependent | Install + config filename + HOME surface verified. |
| `opencode` | `npm i -g opencode-ai@latest` | `~/.config/opencode` (dir), `~/.local/share/opencode` (dir, holds `auth.json`) | provider-dependent | Verified. |
| `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash` — **not npm** | `~/.grok` (dir — `config.toml`, `auth.json`) | `api.x.ai:443`; `x.ai:443` for the installer, with `install_network: loose` because the installer's redirect target is undocumented | Install + config dir verified against vendor docs. API key env var is `XAI_API_KEY` per vendor docs; a third-party guide claims `GROK_CODE_XAI_API_KEY` — the vendor spelling wins, and that discrepancy is exactly why presets are templates. |

Source of truth for the exact values: `src/jailbee/agent_presets.py`.

## 9. Claude

`agents.claude` is the preferred spelling. A top-level `claude:` block is
still accepted as a **legacy alias** — it's translated into `agents.claude`
at config-load time, before validation. Defining **both** in the same
merged config (global + repo combined) is a `ConfigError` naming both
spellings; pick one, and prefer `agents.claude`.

Claude carries every generic field from the table in
[Writing your own agent](#4-writing-your-own-agent) — `enabled`,
`autostart`, `command`, `install`/`update`, `auto_update`, `install_network`,
`shared`, `egress_allow`, `env` — plus Claude-only fields for its deeper
integration (AI-generated PR descriptions, plugin marketplace egress, the
bundled jailbee skills):

- `plugins_enabled`
- `install_jailbee_skills`
- `ai_pr_description`
- `ai_pr_branch`
- `pr_prompt`
- `ai_pr_model`
- `ai_pr_timeout`

Full field-by-field descriptions for these live in the
[`claude` section of Configuration reference](config.md#claude) — that
section stays the authoritative reference for the Claude-only fields; this
page covers the generic `agents:` mechanism they sit on top of.
