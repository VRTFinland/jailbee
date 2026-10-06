# Claude Code through LiteLLM

`claude-jb` runs Claude Code on non-Anthropic models through a LiteLLM proxy in
the dedicated `jailbee-litellm` Incus container. Plain `claude` remains native
and uses your Claude subscription; both commands can run side by side in the
same dev container. Gateway sessions share Claude Code's per-repo configuration
and history, but neither command changes the other's environment.

Several ChatGPT accounts can run side by side (one proxy instance each), and
routes can use any LiteLLM provider with an API key. LiteLLM settings belong in
the host's `global.yaml`; a repo may override routes and profiles in its
host-local file ([Per-repo overrides](#per-repo-overrides)), never in a
committed `.jailbee/config.yaml`. See [Configuration](config.md#litellm) and
[Commands](commands.md).

## Setup

1. On the host, set this in `~/.config/jailbee/global.yaml`:

   ```yaml
   litellm:
     enabled: true
   ```

2. Run `jailbee litellm up`, then `jailbee litellm login [ACCOUNT]`, then
   `jailbee litellm up` again. The first `up` sets up the container but leaves an
   account whose routes need a ChatGPT login stopped until it has one (LiteLLM
   would wait in its own device-code prompt and never answer its health probe);
   the second starts it. An account that serves only API-key routes needs no
   login and starts on the first `up`. `login`
   starts LiteLLM's interactive ChatGPT device-code login for that account (the
   name may be omitted while there is only one account, `default` by default).
3. Run `jailbee base build` in each repo that needs `claude-jb` (the wrapper is
   installed in the golden image). Run `jailbee apply` in each such repo to
   populate `/etc/jailbee/litellm.json` and its proxy key in existing running
   containers; new containers receive them at `jailbee new`. Recreate any old
   container whose image predates the wrapper: `apply` does not install binaries
   into existing containers.
4. Inside a dev container, run `claude-jb` instead of `claude`. Select a tier
   with Claude Code's `--model` flag, for example `claude-jb --model haiku`.
   Use `jailbee litellm status` on the host to inspect health and login state.

`jailbee litellm down` deletes the proxy container but keeps its state volume
(logins and settings); `jailbee litellm down --purge` deletes the volume too.
Run `jailbee apply` in each affected repo afterward: it
removes stale dev-container proxy settings, and `claude-jb` then fails clearly
instead of silently falling back to native Claude. Bring the proxy back with
`jailbee litellm up` and re-apply to restore access.

## Quick start by provider

Each recipe goes into the host's `~/.config/jailbee/global.yaml` and is
followed by the [Setup](#setup) steps; check the result with `jailbee litellm
ls` (what `claude-jb` will use) and `jailbee litellm status`.

### ChatGPT subscription (Codex)

The built-in `codex` profile needs nothing but `enabled: true`:

```yaml
litellm:
  enabled: true
```

`jailbee litellm up`, `jailbee litellm login`, `jailbee litellm up`, then in a
container `claude-jb -p 'say hi' --model haiku`. The tiers it maps are in
[Routes and profiles](#routes-and-profiles); a second ChatGPT account is in
[Accounts](#accounts).

### OpenRouter

Put the key in `~/.config/jailbee/litellm/secrets.env` and `chmod 600` it:

```sh
OPENROUTER_API_KEY=sk-or-...
```

Then name it in a route; the config holds the variable's name, never the key:

```yaml
litellm:
  enabled: true
  default_profile: openrouter
  routes:
    kimi:
      model: openrouter/moonshotai/kimi-k3   # openrouter/<model id from openrouter.ai>
      context_window: 262144                 # the model's input limit
      api_key: OPENROUTER_API_KEY
  profiles:
    openrouter: {fable: kimi, opus: kimi, sonnet: kimi, haiku: kimi}
    # Without a ChatGPT subscription, point the built-in profile here as well:
    # while it maps chatgpt/ routes, its account waits for a ChatGPT login.
    codex: {fable: kimi, opus: kimi, sonnet: kimi, haiku: kimi}
```

`jailbee litellm up` starts the proxy straight away (no login), then
`jailbee base build` and `jailbee apply` per repo as in [Setup](#setup).
Different OpenRouter models per tier are just more routes.

### xAI (Grok) with an API key

Same shape, with the key from console.x.ai:

```sh
GROK_API_KEY=xai-...
```

```yaml
litellm:
  enabled: true
  default_profile: grok
  routes:
    grok:
      model: xai/grok-4.3        # xai/<model name from docs.x.ai>
      context_window: 256000     # the model's input limit, from its model page
      api_key: GROK_API_KEY
  profiles:
    grok: {fable: grok, opus: grok, sonnet: grok, haiku: grok}
    codex: {fable: grok, opus: grok, sonnet: grok, haiku: grok}   # no ChatGPT subscription
```

The request is billed to the API key at xAI's token prices. A SuperGrok or
X Premium+ subscription can serve routes too, as
[an experimental option](#xai-subscription-experimental).

## xAI subscription (experimental)

A SuperGrok or X Premium+ subscription can serve routes through LiteLLM's xAI
OAuth provider. Mark the route `oauth: true` and give it an account:

```yaml
litellm:
  routes:
    grok: {model: xai/grok-4.3, oauth: true, context_window: 256000}
  profiles:
    grok: {account: default, fable: grok, opus: grok, sonnet: grok, haiku: grok}
    codex: {fable: grok, opus: grok, sonnet: grok, haiku: grok}   # no ChatGPT subscription
```

Remapping `codex` keeps the built-in `default` account from also needing a
ChatGPT login: an account is held back until it has a login for every provider
its profiles use.

Run `jailbee litellm up`, then `jailbee litellm login --provider xai` on the
host: it prints a URL; open it in a browser on the same machine and approve.
For the length of the login, jailbee forwards the host's `127.0.0.1:56121` to
the proxy, where LiteLLM waits for the browser's callback (180 seconds). The
token is stored in the state volume beside the account's ChatGPT login, so one
account and one profile can use both.

Without an xAI login the account's proxy still starts: only its `oauth` routes
fail, and `jailbee litellm up`, `status` and `doctor` say so. `--provider` may
be left out here, since no route of `default` needs a ChatGPT login; an account
whose profiles use both providers needs `--provider chatgpt` or `--provider xai`.

This is experimental: it follows LiteLLM's xAI OAuth provider and has not yet
been verified by jailbee against a live subscription. Grok models LiteLLM's
local model map does not list as reasoning models drop the `effort` setting.
`oauth` is refused together with `api_key` or `api_base`, and secret names
starting with `XAI_` are reserved (an `XAI_API_KEY` in the proxy would silently
take precedence over the subscription).

## Routes and profiles

The built-in `codex` profile maps Claude Code tiers to these routes:

| Tier | Route | Model | Effort |
|---|---|---|---|
| Fable | `astra` | `chatgpt/gpt-6-astra` | Fixed `high` |
| Opus | `sol-high` | `chatgpt/gpt-6.1-sol` | Fixed `high` |
| Sonnet | `sol-medium` | `chatgpt/gpt-6.1-sol` | Fixed `medium` |
| Haiku | `luna-high` | `chatgpt/gpt-6-luna` | Fixed `high` |

Override only the fields you need in the host's `global.yaml`. A same-named
route overlays the built-in route field by field; a same-named profile overlays
its tiers field by field. New routes need a `model`, and profiles name routes:

```yaml
litellm:
  enabled: true
  default_profile: fast
  routes:
    sol-high: {effort: max} # keeps the built-in chatgpt/gpt-6.1-sol model
    luna-floor: {model: chatgpt/gpt-6-luna, min_effort: medium}
  profiles:
    codex: {haiku: luna-floor} # other codex tiers keep their defaults
    fast: {account: default, sonnet: luna-floor, haiku: luna-floor, effort: low}
```

A profile that maps a `chatgpt/` route must name the `account` that serves it
(the built-in `codex` profile names `default`), so a new profile of ChatGPT
routes carries `account:` as above.

`effort` **fixes** the reasoning effort on every request; `min_effort` only
raises requests below its floor. They cannot be set together on a route. The
profile's `effort` sets the session's default `--effort` unless supplied by the
user. Claude Code always sends a session effort, so a LiteLLM deployment's
`reasoning_effort` alone cannot differentiate Opus and Sonnet when both use Sol:
fixed route efforts do. On a fixed-effort tier, Claude Code's `/effort` has no
effect. An explicitly `null` tier removes that mapping; unmapped requests may
hit the proxy's `claude-*` catch-all, which uses the default profile's cheapest
mapped tier.

Profile selection order is `claude-jb --profile NAME`, then
`JAILBEE_LITELLM_PROFILE`, then `litellm.default_profile` (default `codex`).
`--profile` is consumed by the wrapper, not passed to Claude Code. Use plain
`claude` for native access, not `--profile native`. `claude-jb --help` prints the
wrapper's own options and the profiles with their windows, then Claude Code's help.

### Choosing the context window

`claude-jb -C 1m` (or `--context 1m`, `--context=1m`) sets the session's window.
The value is `272k`, `1m` (1,000,000), a token count, `max` (the profile's
ceiling) or `default`; the last flag wins. It is `-C`, not `-c`: lowercase is
Claude Code's `--continue`, which passes through. Without the flag the session
uses the profile's default window, so a larger one is always a choice.

A larger window costs more per request, and `claude-jb` says so on stderr. The
ceiling is each route's `max_context_window`: 1,050,000 for the built-in
`chatgpt/gpt-6-astra`, `gpt-6.1-sol` and `gpt-6-luna` routes, and for any other
route its `context_window` unless you set one. A profile's ceiling is the smallest
of its routes', since Claude Code takes one window per session; a value above it
is refused with the ceiling named. Set `max_context_window` on a route to allow
more or fewer tokens than the built-in table says:

```yaml
litellm:
  routes:
    nova: {model: chatgpt/gpt-7-nova, context_window: 272000, max_context_window: 400000}
```

`jailbee litellm ls` shows `272000 tokens (up to 1050000)` for a route that can be
raised. The ceiling reaches containers with `jailbee apply`; the flag itself needs
`jailbee base build`. Until then `claude-jb` accepts only the default and tells you
to re-run `jailbee apply`.

### Profile instructions

A profile may carry `instructions`: model-policy text that `claude-jb` appends
to Claude Code's system prompt in sessions of that profile. Use it for guidance
that depends on what the tiers map to. The host-wide `~/.config/jailbee/AGENTS.md`
cannot do this, because plain `claude` reads it too.

```yaml
litellm:
  profiles:
    codex:
      instructions: |
        Every tier here runs a GPT-6 model, not a Claude one. Use the opus tier
        for planning and review, sonnet for edits and haiku for lookups.
```

- A higher layer replaces the text whole (a repo's `litellm.profiles.codex.instructions`
  beats the host's); `null` removes it. Texts are never concatenated across layers.
- It is at most 64 KiB, and an empty string is rejected.
- It reaches sessions started **after `jailbee apply`**; unlike `AGENTS.md`, it is
  rendered into `/etc/jailbee/litellm.json` only by `apply` and `new`.
- Order in the prompt: the host-wide `AGENTS.md` (as managed memory), then the
  profile's text, then your own `--append-system-prompt` /
  `--append-system-prompt-file`. `claude-jb` merges your flags into one argument
  after the profile's text; with no profile text they pass through unchanged.
  Everything after `--` is passed to Claude Code untouched.
- Plain `claude` and profiles without `instructions` are unaffected. It is
  guidance to the model, not enforcement: the text is set on the host and
  cannot be changed through the host's config from inside the container, but a
  user in the container (who has `sudo`) can edit the container's copy, and
  `jailbee apply` rewrites that copy from the host's config.
- `jailbee config edit` edits it in a multi-line prompt (Ctrl-S commits).

`claude-jb` gives Claude Code one model name per tier of the selected profile,
and the name comes from the profile and tier, not from the route:
`jb.<profile>.<level>`, where the level is `most-capable` (Fable), `capable`
(Opus), `standard` (Sonnet) or `cheap` (Haiku). The built-in profile's Opus
tier is `jb.codex.capable`. A running session keeps the names it started with,
and the proxy answers each one with whichever route the profile maps that tier
to now. So after `jailbee apply` you can rename, replace or remap routes, and
every open session continues on the new route. What still breaks an open
session is renaming or removing the profile itself, unmapping the tier, or
moving the profile to another `account`. The levels do not repeat Claude
family names because Claude Code reads `opus`, `haiku` and similar from a model
name and changes its requests to match.

The proxy also serves every route under its own name, `jb-default-<route>`,
for `/model` and for sessions started before tier names existed. Profile names
follow the same rule as route names (`[a-z0-9][a-z0-9_-]{0,63}`).

The built-in models default to a **272,000-token context window** (`astra`,
`sol-*`, `luna-high`): the ChatGPT subscription backend's maximum input, not
the API's 1.05M total. Claude Code compacts a fixed reserve below the window it
is told about, so a larger value would compact only after the backend had
refused the prompt. A new model needs an explicit `context_window`. Claude Code
takes one window for the whole session, whichever tier answers, so the wrapper
exports `CLAUDE_CODE_MAX_CONTEXT_TOKENS` as the **smallest** window among the
selected profile's mapped routes; a larger window on one route takes effect
only in a profile whose every route has it.

## Adding a new model

New models usually appear before a jailbee release knows them, and nothing
waits for one: a route is a LiteLLM model string, so any model LiteLLM can
reach is a config edit away.

1. **Find the model string**: `<provider>/<model>`, as in LiteLLM's
   [provider list](https://docs.litellm.ai/docs/providers), with the model name
   the provider publishes (`chatgpt/…`, `openrouter/<vendor>/<model>`,
   `xai/…`).
2. **Find its context window**: the most *input* the backend accepts, which
   for a subscription backend can be well below the API's advertised total.
   A model jailbee does not know needs `context_window`; too large a value makes
   Claude Code compact only after the backend has refused the prompt.
3. **Point a tier at it.** Either give a built-in route a new model, which keeps
   the route's name and effort:

   ```yaml
   litellm:
     routes:
       sol-high: {model: chatgpt/gpt-6.2-sol, context_window: 272000}
       sol-medium: {model: chatgpt/gpt-6.2-sol, context_window: 272000}
   ```

   or add a route and remap the tier:

   ```yaml
   litellm:
     routes:
       nova: {model: chatgpt/gpt-7-nova, effort: high, context_window: 400000}
     profiles:
       codex: {opus: nova}   # other tiers keep their routes
   ```

4. **Apply it**: `jailbee apply` in any repo loads route and profile edits into
   the running proxy without a restart, and open `claude-jb` sessions continue
   on the new model, since they hold tier names, not route names. A new route
   to another provider widens the egress allowlist in the same run, and a new
   secret restarts the instance; only an edit that changes nothing but
   `egress` needs `jailbee litellm up` ([details](#per-repo-overrides)).
5. **Check it**: `jailbee litellm ls` shows each tier's model, effort and
   window; `claude-jb -p 'say hi' --model opus` sends one request.

If LiteLLM itself does not know the model or provider yet, a newer LiteLLM
may: set `litellm.version` and run `jailbee litellm up`. That installs without
the hash lock and warns every time. When a jailbee release later changes a
built-in route, your override still wins; drop it once `jailbee litellm ls`
shows the built-in has caught up.

## Per-repo overrides

A repo can change routes and profiles for its own containers in its host-local
file, `~/.config/jailbee/repos/<prefix>.yaml` (`<prefix>` is the repo's
`container_prefix`). The `litellm:` block there accepts four keys: `routes`,
`profiles`, `default_profile` and `autostart`. `enabled`, `version`, `accounts`,
`egress` and `extra` describe the shared proxy and are refused. A `litellm:`
block in a committed `.jailbee/config.yaml` is refused too.

```yaml
# ~/.config/jailbee/repos/myrepo.yaml
litellm:
  default_profile: fast
  routes:
    luna-high: {effort: low}          # this repo's Haiku tier thinks less
  profiles:
    fast: {account: default, sonnet: luna-high, haiku: luna-high, effort: low}
  autostart: true
```

The layers stack as built-in, then `global.yaml`, then the repo. The merge is
per route field and per profile tier: a route or profile named like an existing
one overrides only the fields it sets, and a field set in the repo replaces the
value below it whole. `params` and `egress` are replaced, not appended, unlike
list keys elsewhere in jailbee's config. An explicit `null` tier unmaps it. Switching a route between `effort` and `min_effort`
needs the old key cleared explicitly (`effort: low` next to `min_effort: null`, or the
reverse), because the merge keeps the value below and the two cannot be set together. The
merged result must be valid as a whole (known routes, accounts and so on),
otherwise the repo's config fails to load.

The proxy serves the two scopes under different model names. A repo whose
override sets non-empty `routes` or `profiles` gets its own:
`jb-<prefix>.<profile>.<level>` for its tiers and `jb-<prefix>.<route>` for its
routes. The host's are `jb.<profile>.<level>` and `jb-default-<route>`. A repo
that only sets `default_profile` or `autostart` has no scope of its own and
uses the host's names. Route and profile names contain no dots, and neither do
container prefixes, so no two of these forms can collide. Inspect the result
with `jailbee litellm ls`.

After editing an override, run `jailbee apply` in that repo. It re-renders the
proxy configuration and syncs the repo's running containers. Edits to routes
and profiles (a model, `context_window`, `effort`, `api_base`, a new route that
needs no new secret) are **loaded into the running proxy without a restart**:
open `claude-jb` sessions keep going, and a stream in flight finishes on the
route it started on. Anything the proxy only reads at start (a secret, `extra`
outside its `model_list`, the proxy's settings, the callback itself) restarts the instance that changed,
saying which. If the proxy does not confirm a reload within about ten seconds,
or refuses it, `apply` restarts the instance instead and says why.
`jailbee apply --no-restart` still applies reloads, since they interrupt
nothing, and leaves an instance that needs a restart (or whose reload was not
confirmed) pending, with a warning; a later plain `jailbee apply` restarts it.
`jailbee new` does not update the proxy, with one exception: the first container in a
scratch directory runs an `apply` to create its profiles, and that run can apply a pending
route reload (waiting up to about ten seconds per instance) but never restarts an instance.

`apply` sees an edit through the rendered instance files, and the proxy's egress
allowlist is not part of them. An edit that changes **only** egress (a route's
`egress` list on an existing route, or `litellm.egress` in `global.yaml`) is
therefore not applied by `jailbee apply`; run `jailbee litellm up`, which rewrites
the allowlist. An edit that also changes the rendered files, such as an override
that adds a route (with or without a new egress host), makes `apply` rewrite the
allowlist along with reloading or restarting the instance. If the proxy needs more than a
restart (a different LiteLLM version, a new account, an unattached state
volume), `apply` says so and points at `jailbee litellm up`.

A broken override does not stop the proxy: `jailbee litellm up`, `jailbee
litellm ls` and `jailbee apply` run from another repo skip it with a warning
naming the file, and `claude-jb` in the broken repo's containers cannot use the
override until it is fixed. Every command run in the broken repo itself,
`jailbee apply` included, fails to load its config and names the same file, so
fix the file first.

`jailbee config edit --local` offers the four keys in the repo's local layer.

## Autostart

`litellm.autostart: true` (in `global.yaml`, overridable per repo as above)
starts the Claude autostart window with `claude-jb` instead of `claude`. It
needs `litellm.enabled` and the Claude agent with `autostart` on. Only a command
whose first word is `claude`, or a path ending in `claude`, is rewritten and its
flags are kept; anything else (`env X=1 claude`, a wrapper script) is left as
configured. The profile is `claude-jb`'s own selection, so the repo's
`default_profile` applies.

## Accounts

Each entry in `litellm.accounts` is one LiteLLM process
(`jailbee-litellm@<account>`, its own port) and holds one ChatGPT login and one
xAI login side by side. A profile names the account that
serves it; the built-in `codex` profile uses `default`, so renaming that
account means rebinding `codex`:

```yaml
litellm:
  enabled: true
  accounts: [personal, work]
  profiles:
    codex: {account: personal}
    codex-work: {account: work, fable: astra, opus: sol-high, sonnet: sol-medium, haiku: luna-high}
```

Log each account in once: `jailbee litellm login personal`, `jailbee litellm
login work`. `claude-jb --profile codex-work` then runs on the work
subscription. A `chatgpt/` route is served only by the instances whose
profiles map it. Removing an account from the list stops its instance on the
next `jailbee litellm up` (which prints `Stopped <account>`); its login is kept
in the state volume until `jailbee litellm down --purge`. `logout`, `login` and
`logs` accept only accounts in the list, so log an account out before removing
it.

## Other providers and API keys

Any LiteLLM model string works as a route ([quick starts](#quick-start-by-provider)
for OpenRouter and xAI). API keys live in
`~/.config/jailbee/litellm/secrets.env` (mode `0600`, `NAME=value` lines; `export NAME=value` and quoted values are
accepted, but a value may not contain a quote, backslash or NUL); the config
names the variable, never the key:

```yaml
litellm:
  routes:
    kimi:
      model: openrouter/moonshotai/kimi-k3
      context_window: 262144
      api_key: OPENROUTER_API_KEY
  profiles:
    kimi: {opus: kimi, sonnet: kimi, haiku: kimi}
```

A profile of API-key routes needs no `account`; every instance serves API-key
routes, and an unset `account` means the first account's instance. The proxy's
egress allowlist follows the routes: jailbee knows the hosts of `chatgpt/`,
`openai/`, `openrouter/`, `xai/`, `gemini/` and `deepseek/`. Any other provider
needs `api_base` (whose host replaces the provider's default hosts) or
`egress: [host[:port]]`. Only the secrets that routes (or `extra`) reference are
handed to the proxy, and `jailbee litellm up` refuses to start while one is
missing or the file has any group or other permission bit set. Changing a secret restarts the
instances on the next `up`.

## Raw LiteLLM configuration

`litellm.extra: ~/.config/jailbee/litellm/extra.yaml` names a LiteLLM-native
fragment. `jailbee litellm up` deep-merges it into every instance's config
last: mappings merge, every list appends to jailbee's list of the same key (for
example `model_list`, `litellm_settings.callbacks` or
`router_settings.fallbacks`), and other values replace jailbee's. `up` refuses a
fragment that defines `jb-*` or `claude-*` models, sets
`general_settings.master_key`, makes `general_settings`, `litellm_settings`,
`router_settings` or `environment_variables` a non-mapping, or makes
`model_list` or `litellm_settings.callbacks` a non-list. Secrets it needs are
referenced as `os.environ/NAME` and read from `secrets.env`; the names used as
`environment_variables` keys or `os.environ/NAME` values must not be `PORT`,
`PATH` or `HOME`, nor start with `LITELLM_`, `JAILBEE_`, `CHATGPT_`, `XAI_`, `PYTHON` or
`LD_`, or `up` refuses. Hosts its deployments reach go in `litellm.egress`. Setting
`litellm_settings.turn_off_message_logging: false` there turns prompt logging
back on; that is your choice.

## Security and limitations

- The proxy's state (ChatGPT OAuth tokens, rendered configs and the API keys
  it was given) lives in the Incus custom volume `jailbee-litellm-state` on the
  default storage pool, mounted only in the proxy container. It is never on the
  host filesystem, and the proxy container maps no host user (`raw.idmap`).
  Jailbee writes the rendered files into the volume through `incus exec`'s
  standard input. `jailbee litellm down` keeps the volume, so logins survive a
  rebuild; `jailbee litellm down --purge` deletes it. On the host, only
  `~/.local/share/jailbee/litellm/` remains, holding the port map, each
  account's proxy key (`0600`) and its `applied.sha256` and `applied-hot.sha256` stamps. Dev containers get only the proxy keys, one
  `/etc/jailbee/litellm-<account>.key` (`0640`, readable by the dev group) per
  account. `jailbee litellm logout [ACCOUNT] [--provider chatgpt|xai]` deletes that
  account's token for that provider.
- The proxy has default-deny egress restricted to the hosts the routes need
  (see [Other providers](#other-providers-and-api-keys)) plus `litellm.egress`
  after installation. During installation and reinstall,
  only PyPI and Ubuntu package hosts are permitted; the proxy is stopped and
  the state volume is detached until package access is removed.
  Dev containers can reach its static address through the
  `jailbee-services` ACL; a proxy key is not a provider token.
- The default LiteLLM installation is pinned to version `1.104.0` and a
  hash-locked requirements file. Setting `litellm.version` bypasses the hash
  lock and emits a warning. Prompt/message logging and the remote model-cost
  map fetch are disabled. Treat this as risk reduction, not a guarantee that
  vendor traffic or the running service cannot expose sensitive prompts.
- Non-streaming requests to the ChatGPT backend fail with the current LiteLLM
  integration; Claude Code streams. Tool-call quality depends on the model;
  large tool arguments have been reported to corrupt on Codex routes.
- Claude Code's claude.ai connectors are disabled in gateway sessions.
  Resuming a native session with signed Opus thinking blocks through
  `claude-jb` is untested.
- Changing a route reloads it into the account's running instance; changing a
  secret, `litellm.extra` outside its `model_list` or the proxy's own settings restarts it, interrupting
  every container's streams on that account (`jailbee litellm up`, or
  `jailbee apply` in any repo). `apply --no-restart` defers the restart.
  An instance that serves no route at all cannot take its first route by
  reload and is restarted.

## Troubleshooting

`claude-jb` fails rather than falling back to native:

| Error | Remedy |
|---|---|
| `no LiteLLM proxy configured for this container` | On the host run `jailbee litellm up`, then `jailbee apply` in this repo; rebuild the golden image if the wrapper itself is absent. |
| `cannot read ... (not valid JSON)` or `cannot read the proxy key` | Run `jailbee apply` in the repo on the host. |
| `unknown profile` | Check the names in `litellm.profiles`, in `global.yaml` and in this repo's override (`jailbee litellm ls` lists both); select a valid `--profile` or fix `JAILBEE_LITELLM_PROFILE`. |
| `--profile needs a name` | Pass `--profile NAME` or remove the flag. |
| `--context needs a value` or `... is not a size` | Pass `272k`, `1m`, a token count, `max` or `default`. |
| `--context ... above the ...-token ceiling of profile` | Choose a smaller window or `max`; to allow more, raise `max_context_window` on the profile's routes and run `jailbee apply` on the host. |
| `proxy key ... is empty` | Run `jailbee apply` on the host to resync the key. |
| `proxy ... is unreachable` | Run `jailbee litellm up` on the host, then `jailbee apply` in this repo. |

On the host, `jailbee litellm up`, `login` and `jailbee apply` can report:

| Error | Remedy |
|---|---|
| `... does not define NAME`, `... does not exist` or `... has insecure permissions` (about `secrets.env`) | Add `NAME=value` to `~/.config/jailbee/litellm/secrets.env`, `chmod 600` it, run `jailbee litellm up`. When a repo override adds a route or changes a route's `api_key` to the missing secret, the message adds `(named by .../repos/<prefix>.yaml)`; a secret named only in `global.yaml` is not attributed to any repo file. A missing secret there still blocks `up` and the proxy update in `apply` for every repo, since the proxy is shared. |
| `cannot read ...` (about `secrets.env` or the `extra` file) | Make the file readable by your user and plain UTF-8 text, then run `jailbee litellm up`. |
| `missing LiteLLM account; pass it explicitly, or run in a terminal to choose. Candidates: ...` | Several accounts are configured and there is no terminal to choose from: pass the account name, e.g. `jailbee litellm login work`. |
| `Cannot listen on 127.0.0.1:56121` (from `login --provider xai`) | Another program on the host uses the port; stop it and retry. |
| `No route of account ... uses an xAI subscription` | Mark a route of that account's profiles `oauth: true`, run `jailbee litellm up`, then log in. |
| `profile(s) ... have no proxy instance yet` (from `jailbee apply` or `jailbee new`) | Run `jailbee litellm up`, then `jailbee apply`. |

On the host, use `jailbee litellm ls` (profiles, tiers, routes, efforts and
context windows, globally and per repo override), `jailbee litellm status`,
`jailbee litellm logs [ACCOUNT] [-f]`, and `jailbee doctor`. Doctor reports a broken repo override (one `litellm repo override` row
per skipped file), unreadable LiteLLM inputs (`secrets.env`,
`extra`), one `litellm version` row for the installed-versus-configured
version, and per account an instance row (not set up, or unhealthy) and a login
row. A missing login fails doctor only for the account that serves the default
profile, and only when that profile maps a `chatgpt/` route (missing ChatGPT login) or
an `oauth` xAI route (missing xAI login); for another account
whose profiles need a login the row passes and names those profiles. A login
state that cannot be read always fails. Doctor also checks the services ACL and
upstream reachability; `jailbee litellm up` re-resolves the proxy's provider
allowlist when upstream addresses change. `jailbee litellm login [ACCOUNT] [--provider chatgpt|xai]`
refreshes a missing login.
