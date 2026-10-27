# Claude Code through LiteLLM

`claude-jb` runs Claude Code on non-Anthropic models through a LiteLLM proxy in
the dedicated `jailbee-litellm` Incus container. Plain `claude` keeps its normal
authentication and environment; the wrapper does not redirect it. With a native
Claude login and no separate gateway settings, it uses your Claude subscription.
Both commands can run side by side in the same dev container. Gateway sessions
share Claude Code's per-repo configuration and history; the wrapper changes only
its own process environment, not the calling shell's.

This is the recommended way to use models or subscriptions other than
Anthropic's in JailBee. Earlier live checks used ChatGPT (Codex) and SuperGrok
subscriptions; provider compatibility and entitlements can change.

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
   containers; new containers receive them at `jailbee new`. For a container
   whose image predates the wrapper, preserve its work before replacing it with
   one from the rebuilt image: `apply` does not install binaries into existing
   containers.
4. Inside a dev container, run `claude-jb` instead of `claude`. Select a tier
   with Claude Code's `--model` flag, for example `claude-jb --model haiku`.
   Use `jailbee litellm status` on the host to inspect health and login state.

`jailbee litellm down` stops the proxy and withdraws its dev-container ACL
allowance. The container and state volume (logins and settings) are kept;
`jailbee litellm up` starts it again. Dev-container settings remain, so
`claude-jb` reports the proxy as unreachable. `apply` neither starts the stopped
proxy container nor removes its payload just because it is stopped.

`jailbee litellm up --recreate` deletes and rebuilds the proxy container with
its state volume attached, interrupting active requests. `jailbee litellm down
--purge` also deletes discovered state volumes and their logins; preserve any
state you need first. After purging, run `jailbee apply` in each affected repo
to remove stale proxy settings from its **running** dev containers. Start any
stopped dev containers before applying if their settings need cleanup too.
`claude-jb` then reports missing configuration, never silently falling back to
native Claude. To restore access, run `jailbee litellm up`, log in again if
needed, and re-apply.

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
    # while it maps chatgpt/ routes, only the account a mapped route names waits for a ChatGPT login.
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
X Premium+ subscription can serve routes too — see
[xAI subscription](#xai-subscription).

## xAI subscription

A SuperGrok or X Premium+ subscription can serve routes through LiteLLM's xAI
OAuth provider. Mark the route `oauth: true` and select its profile:

```yaml
litellm:
  enabled: true
  default_profile: grok
  routes:
    grok: {model: xai/grok-4.3, oauth: true, context_window: 256000}
  profiles:
    grok: {fable: grok, opus: grok, sonnet: grok, haiku: grok}
    codex: {fable: grok, opus: grok, sonnet: grok, haiku: grok}   # no ChatGPT subscription
```

Remapping `codex` removes its ChatGPT login requirement. A missing ChatGPT
login holds the account's instance stopped; a missing xAI login does not block
startup, but its OAuth routes cannot work until you log in.

Run `jailbee litellm up`, then `jailbee litellm login --provider xai` on the
host: it prints a URL; open it in a browser on the same machine and approve.
For the length of the login, jailbee forwards the host's `127.0.0.1:56121` to
the proxy, where LiteLLM waits for the browser's callback (180 seconds). If the
browser shows an authorization code instead, paste that bare code into the
waiting terminal and press Enter; input is hidden. A complete callback URL is
also accepted, but only for this login's loopback address and matching state.
Do not paste tokens or put the code in a command argument. Ctrl-C cancels the
login; cancellation and timeout restore the terminal and remove the temporary
forwarding. This keeps LiteLLM's PKCE exchange and token storage unchanged;
a real xAI subscription login has not been verified for this fallback. The
token is stored in the state volume beside the account's ChatGPT login, so one
account and one profile can use both.

Without an xAI login the account's proxy still starts: only its `oauth` routes
fail, and `jailbee litellm up`, `status` and `doctor` say so. `--provider` may
be left out here, since no route of `default` needs a ChatGPT login; an account
whose routes use both providers needs `--provider chatgpt` or `--provider xai`.

Earlier live checks exercised LiteLLM's xAI OAuth provider with a SuperGrok
subscription, not every login fallback or account entitlement. In that
integration, Grok models absent from LiteLLM's local reasoning-model map dropped
the `effort` setting; a fixed route effort does not guarantee provider support.
`oauth` is refused together with `api_key` or `api_base`, and secret names
starting with `XAI_` are reserved (an `XAI_API_KEY` in the proxy would silently
take precedence over the subscription).

### One profile, two subscriptions

The recipes above send every tier to one provider. A profile of your own can
mix them instead, which keeps the built-in `codex` profile untouched. `mix`
below sends fable and opus to the ChatGPT subscription and sonnet and haiku to
the SuperGrok one.

A new profile inherits no tier mappings; name a route for each tier you want to
use (at least one is required). The routes
themselves are different: the five ChatGPT routes (`astra`, `sol-high`,
`sol-medium`, `terra-high`, `luna-high`) are built in and exist without being written here —
naming one is enough. Only `grok` is new, so only it needs a `model`. The
built-in routes and the models and efforts they carry are listed in
[Routes and profiles](#routes-and-profiles).

Both logins belong to the same account (`default`, the only one here), which
is the one constraint: a profile is served by one proxy instance, so its
subscription routes cannot span two accounts.

```yaml
litellm:
  enabled: true
  default_profile: mix
  routes:
    grok: {model: xai/grok-4.3, oauth: true, context_window: 256000}
  profiles:
    mix:
      fable: astra       # built-in route: chatgpt/gpt-6-astra
      opus: sol-high     # built-in route: chatgpt/gpt-6.1-sol
      sonnet: grok       # the route above: xai/grok-4.3
      haiku: grok
```

Written out in full — the same configuration, with the built-in routes repeated
instead of relied on. A same-named route overlays the built-in one field by
field, so restating `model` and `effort` changes nothing; it only makes the
file readable on its own:

```yaml
litellm:
  enabled: true
  default_profile: mix
  routes:
    astra:     {model: chatgpt/gpt-6-astra, effort: high}             # built in
    sol-high:  {model: chatgpt/gpt-6.1-sol, effort: high}             # built in
    grok:      {model: xai/grok-4.3, oauth: true, context_window: 256000}
  profiles:
    mix:
      fable: astra
      opus: sol-high
      sonnet: grok
      haiku: grok
```

The two ChatGPT routes need no `context_window`: jailbee already knows those
models' window (272,000). `grok` does, because `xai/grok-4.3` is not in that
table. `sol-medium`, `terra-high` and `luna-high` are built in too, but `mix` maps no tier to
them, so there is nothing to repeat.

`jailbee litellm up`, then both logins — `mix`'s routes use both providers, so
each needs its flag:

```sh
jailbee litellm login --provider chatgpt
jailbee litellm login --provider xai
jailbee litellm up
```

`claude-jb --model opus` then runs on the ChatGPT subscription and
`claude-jb --model haiku` on the xAI one; with `default_profile: mix` that is
what a plain `claude-jb` uses. `jailbee litellm ls` shows which model each tier
resolves to. The profile's window is the smaller of its routes', so `mix`'s
ceiling is `grok`'s 256,000 tokens, not the ChatGPT routes' 272,000 — see
[Choosing the context window](#choosing-the-context-window). The built-in
`codex` profile is still there, all on ChatGPT, via `claude-jb -P codex`.

## Routes and profiles

The built-in `codex` profile maps Claude Code tiers to these routes:

| Tier | Route | Model | Effort |
|---|---|---|---|
| Fable | `astra` | `chatgpt/gpt-6-astra` | Fixed `high` |
| Opus | `sol-high` | `chatgpt/gpt-6.1-sol` | Fixed `high` |
| Sonnet | `terra-high` | `chatgpt/gpt-6.1-terra` | Fixed `high` |
| Haiku | `luna-high` | `chatgpt/gpt-6-luna` | Fixed `high` |

`sol-medium` (`chatgpt/gpt-6.1-sol`, fixed `medium`) is built in too, but no
tier of `codex` maps to it; name it in a profile to use it.

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
    fast: {sonnet: luna-floor, haiku: luna-floor, effort: low}
```

A subscription route (`chatgpt/`, or `oauth: true`) is served by the first
account in `litellm.accounts` unless it sets `account`; API-key routes are
served by every account. A profile may mix providers freely, but its
subscription routes must all belong to one account, since one proxy instance
serves a session.

For Anthropic Messages requests, `effort` **fixes** the reasoning effort and
`min_effort` raises requests below its floor. They cannot be set together on a route. The
profile's `effort` sets the session's default `--effort` unless supplied by the
user. Claude Code's request-level effort can override a LiteLLM deployment's
`reasoning_effort`; JailBee's callback applies the fixed value or floor to
Anthropic Messages requests instead. On a fixed-effort tier, `/effort` does not
change that route's requested effort. Other API endpoints bypass this callback.

An explicitly `null` tier removes that mapping. Unmapped `claude-*` requests may
hit an instance's host-level catch-all, not the repo's default profile. It uses
the host default profile if that instance serves all its routes, otherwise the
first host profile by name bound to that account. It picks the first mapped
tier in Haiku, Sonnet, Opus, Fable order, without comparing model prices.
Without a suitable host profile there is no catch-all.

Profile selection order is `claude-jb -P NAME` (or `--profile NAME`), then
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

Selecting a larger window prints a cost warning on stderr. It permits more
context per request; actual billing or subscription usage depends on the
provider and tokens sent, not just the selected limit. The configured ceiling
is each route's `max_context_window`: 1,050,000 for the built-in
`chatgpt/gpt-6-astra`, `gpt-6.1-sol`, `gpt-6.1-terra` and `gpt-6-luna` routes, and for any other
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
raised. The ceiling reaches running containers with `jailbee apply`. The flag itself
requires an image containing the updated wrapper: `jailbee base build` updates
the image for future containers, not existing ones. An updated wrapper reading
an older config without a ceiling accepts only windows at or below the default
and suggests `jailbee apply` when a larger value is requested.

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
and, after a successful proxy reload, subsequent requests use the route the
profile now maps that tier to. Renaming, replacing or remapping a route therefore
does not require a new session solely to change its model name. Existing
sessions retain their original context window, instructions and environment;
restart them if those must change. Reload/restart and login caveats are described
under [Per-repo overrides](#per-repo-overrides). Renaming or removing the profile
itself can still break a session, as can unmapping the tier or
moving the profile to another account — which happens when every one of its
subscription routes moves to that account, since a profile is served by one
account's instance. The levels do not
repeat Claude
family names because Claude Code reads `opus`, `haiku` and similar from a model
name and changes its requests to match.

The proxy also exposes eligible host routes as `jb-default-<route>` for
`/model` and sessions started before tier names existed. Subscription routes
appear only on their account's instance and only when a profile maps them;
API-key routes are served by every instance. Profile names
follow the same rule as route names (`[a-z0-9][a-z0-9_-]{0,63}`).

The built-in models default to a **272,000-token context window** (`astra`,
`sol-*`, `luna-high`). This is JailBee's configured default, not a live check of
your subscription's input limit. The higher opt-in ceilings described above
are configuration limits too; they do not guarantee that a provider accepts
that many tokens. Verify the backend's current input and output limits before
raising the window, or requests may fail before Claude Code compacts them.
A model absent from JailBee's built-in table needs an explicit `context_window`.
The wrapper exports `CLAUDE_CODE_MAX_CONTEXT_TOKENS` as the **smallest** default
window among the selected profile's mapped routes, unless `--context` selects
another value within the profile's ceiling. A higher default on just one route
does not raise the profile's default.

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
   ```

   or add a route and remap the tier:

   ```yaml
   litellm:
     routes:
       nova: {model: chatgpt/gpt-7-nova, effort: high, context_window: 400000}
     profiles:
       codex: {opus: nova}   # other tiers keep their routes
   ```

4. **Apply it**: `jailbee apply` in any valid repo attempts to load route and
   profile edits into the shared running proxy without a restart. After a
   successful reload, open sessions using unchanged tier names use the new
   route on subsequent requests; their context window and environment do not
   change. Missing ChatGPT logins, reload failures and restart-only changes
   can interrupt access ([details](#per-repo-overrides)). A new route to another
   provider updates the egress allowlist in the same run, and referenced new
   secrets are loaded too. An egress-only edit needs `jailbee litellm up`.
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
    fast: {sonnet: luna-high, haiku: luna-high, effort: low}
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
shared proxy configuration and syncs the repo's running containers. Edits to
routes and profiles (a model, `context_window`, `effort`, `api_base`, a new route,
a new or changed API key) normally use a **hot reload without a restart**. A
successful hot reload does not deliberately terminate in-flight streams; new
requests use the updated routes. It does not change the window or environment
of an already-running Claude Code process.

Changes the proxy only reads at start (`extra` outside its `model_list`, any
secret that section names, proxy settings or the callback itself) restart the
changed instance. If a reload is refused or not confirmed within about ten
seconds, `apply` falls back to a restart and says why. These proxy restarts do
not use the dev-container restart confirmation. `jailbee apply --no-restart`
still attempts hot reloads, but leaves restart-dependent instances pending with
a warning. An unconfirmed reload is not a rollback: the running proxy may have
loaded some or all of the change. A later plain `jailbee apply` retries it.

**Login exception:** if a changed instance now serves a `chatgpt/` route but
has no ChatGPT login, it is stopped and disabled, even with `--no-restart`.
This interrupts every route on that account, including API-key and xAI routes.
Log in and run `jailbee litellm up` to start it again. Missing xAI login alone
does not stop the instance.

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

A malformed override is skipped with a warning naming its file when
host-level commands collect repo scopes. `jailbee litellm up` and `jailbee
litellm ls` can still run from that repo; commands that load its configuration,
including `jailbee apply`, fail until it is fixed. An `apply` from another valid
repo can update the shared proxy while skipping the broken override.

Breaking the host file does not immediately change existing sessions: they
may keep using the last applied override. The next successful proxy update
omits the broken scope, so requests using its scoped aliases can then fail.
Fix the file and re-apply in the affected repo to restore its configuration.

`jailbee config edit --local` offers the four keys in the repo's local layer.

## Autostart

`litellm.autostart: true` (in `global.yaml`, overridable per repo as above)
starts the Claude autostart window with `claude-jb` instead of `claude`. It
needs `litellm.enabled` and an enabled Claude agent with `autostart` on. Only a
command starting with `claude`, or a path whose basename is `claude`, followed
by a space or the end of the command is rewritten; its flags are kept.
Anything else (`env X=1 claude`, a wrapper script) is left as configured. The
profile is `claude-jb`'s own selection, so the repo's `default_profile` applies
unless a flag or environment variable overrides it.

This setting also redirects a Claude headless command selected by
`pr.agent: auto` through the same rewrite, even when no Claude autostart window
is enabled. An explicit `pr.agent` uses its own selection instead; see
[PR configuration](config.md#pr).

## Accounts

Each entry in `litellm.accounts` is one LiteLLM process
(`jailbee-litellm@<account>`, its own port) and holds one ChatGPT login and one
xAI login side by side. A subscription route names the account whose login
serves it; without `account` it uses the first one, so a single-account setup
never mentions accounts. A profile is served by its routes' account, and all
its subscription routes must share one. To use one model on two
subscriptions, give each its own route:

```yaml
litellm:
  enabled: true
  accounts: [personal, work]
  routes:
    sol-high-work: {model: chatgpt/gpt-6.1-sol, effort: high, account: work}
    luna-work: {model: chatgpt/gpt-6-luna, effort: high, account: work}
  profiles:
    codex-work: {fable: sol-high-work, opus: sol-high-work, sonnet: sol-high-work, haiku: luna-work}
```

Log each account in once: `jailbee litellm login personal`, `jailbee litellm
login work`. `claude-jb --profile codex-work` then runs on the work
subscription. A subscription route is served only by its own account's
instance, and only when a profile maps it. Removing an account from the list stops its instance on the
next `jailbee litellm up` (which prints `Stopped <account>`); its login is kept
in the state volume until `jailbee litellm down --purge`. `logout`, `login` and
`logs` accept only accounts in the list, so log an account out before removing
it.

Earlier releases set the account on the profile (`profiles.<p>.account`); that
key is now rejected with a pointer here.

## Other providers and API keys

Any LiteLLM model string works as a route ([quick starts](#quick-start-by-provider)
for OpenRouter and xAI). API keys live in
`~/.config/jailbee/litellm/secrets.env` (mode `0600`, one `NAME=value` per line).
`export NAME=value` is accepted, and matching surrounding single or double
quotes are stripped. The resulting value may not contain a single quote,
backslash or NUL. The config names the variable, never the key:

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

A profile of API-key routes needs no `account` on those routes; every instance serves API-key
routes, and a subscription route with no `account` uses the first account's instance
when a profile maps it (an unmapped one is served nowhere). The proxy's
egress allowlist follows the routes: jailbee knows the hosts of `chatgpt/`,
`openai/`, `openrouter/`, `xai/`, `gemini/` and `deepseek/`. Any other provider
needs `api_base` (whose host replaces the provider's default hosts) or
`egress: [host[:port]]`. Only the secrets that routes (or `extra`) reference are
handed to the proxy, and `jailbee litellm up` refuses to start while one is
missing or the file has any group or other permission bit set. A new or changed secret is loaded into the running
instances by the next `up` or `apply`, without a restart, unless only `extra` outside its `model_list` names it.

## Raw LiteLLM configuration

`litellm.extra: ~/.config/jailbee/litellm/extra.yaml` names a LiteLLM-native
fragment. `jailbee litellm up` deep-merges it into every instance's config
last: mappings merge, every list appends to jailbee's list of the same key (for
example `model_list`, `litellm_settings.callbacks` or
`router_settings.fallbacks`), and other values replace jailbee's. `up` refuses a
fragment that defines model names starting with `jb-` or `jb.`, or the exact
catch-all name `claude-*`, sets
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

- The proxy's state (ChatGPT and xAI OAuth tokens, rendered configs and the
  provider keys it was given) lives in the Incus custom volume
  `jailbee-litellm-state`, attached by JailBee only to the proxy container. This
  is Incus-managed storage, not a host-directory bind mount; it is still
  accessible to the host's administrators. The proxy maps no host user via
  `raw.idmap`. JailBee writes rendered files through `incus exec`'s standard
  input. New volumes use `defaults.storage_pool` from `global.yaml`, falling back to the
  default Incus profile's root pool; changing the setting does not migrate an
  existing volume. Discovery checks those candidate pools, not every pool;
  see [Storage migration](storage.md#making-jailbee-use-it).
- `jailbee litellm down` preserves the volume and `--recreate` reattaches it.
  `down --purge` deletes the container and discovered state volumes, including
  their logins. It does **not** remove host configuration or provider keys in
  `~/.config/jailbee/litellm/secrets.env`. Host-side state in
  `~/.local/share/jailbee/litellm/` also remains: the port map, account proxy
  keys (`0600`) and `applied.sha256` / `applied-hot.sha256` stamps. Dev containers
  receive gateway configuration and proxy keys, not provider tokens: one
  `/etc/jailbee/litellm-<account>.key` (`0640`, readable by the dev group) per
  served account. Those keys authorize use of the shared proxy; they are not
  per-repo spending or access boundaries. `jailbee litellm logout [ACCOUNT]
  [--provider chatgpt|xai]` deletes that account's stored token for the provider,
  rather than removing the host's API-key source.
- The proxy has default-deny egress restricted to the hosts the routes need
  (see [Other providers](#other-providers-and-api-keys)) plus `litellm.egress`
  after installation. During installation and reinstall,
  only PyPI and Ubuntu package hosts are permitted; the proxy is stopped and
  the state volume is detached until package access is removed.
  Dev containers can reach its static address through the
  `jailbee-services` ACL; a proxy key is not a provider token.
- The default LiteLLM installation is pinned to version `1.106.0` and a
  hash-locked requirements file. Setting `litellm.version` bypasses the hash
  lock and emits a warning. Prompt/message logging and the remote model-cost
  map fetch are disabled. Treat this as risk reduction, not a guarantee that
  vendor traffic or the running service cannot expose sensitive prompts.
- Earlier integration checks found non-streaming ChatGPT requests failed;
  Claude Code uses streaming. Tool-call quality depends on the model, and
  large tool arguments have been reported to corrupt on Codex routes. Treat
  these as version-dependent observations, not checks performed by `up`.
- Do not rely on Claude Code's claude.ai connectors in gateway sessions; earlier
  checks found them unavailable. The wrapper sets the gateway environment but
  does not independently enforce a connector policy. Resuming a native session
  with signed Opus thinking blocks through `claude-jb` remains unverified.
- Reloads and restarts affect the shared account instance, not just the repo
  running `up` or `apply`. Restart-only changes and failed reloads can interrupt
  every container's streams on that account. `apply --no-restart` defers those
  restarts, but not the missing-ChatGPT-login stop described under
  [Per-repo overrides](#per-repo-overrides). An instance serving no routes needs
  a restart to take its first route.

## Troubleshooting

`claude-jb` fails rather than falling back to native:

| Error | Remedy |
|---|---|
| `no LiteLLM proxy configured for this container` | On the host run `jailbee litellm up`, then `jailbee apply` in this repo; rebuild the golden image if the wrapper itself is absent. |
| `cannot read ... (not valid JSON)` or `cannot read the proxy key` | Run `jailbee apply` in the repo on the host. |
| `unknown profile` | The error lists every profile and its window. Select one of those names with `-P`/`--profile`, or fix `JAILBEE_LITELLM_PROFILE`. |
| `-P needs a name` or `--profile needs a name` | Pass the name (`-P grok`, `--profile grok`) or remove the flag. |
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
| `No route of account ... uses an xAI subscription` | Mark a route of that account `oauth: true`, run `jailbee litellm up`, then log in. |
| `profile(s) ... have no proxy instance yet` (from `jailbee apply` or `jailbee new`) | Run `jailbee litellm up`, then `jailbee apply`. |

On the host, use `jailbee litellm ls` (profiles, tiers, routes, efforts and
context windows, globally and per repo override), `jailbee litellm status`,
`jailbee litellm logs [ACCOUNT] [-f]`, and `jailbee doctor`. Doctor reports a broken repo override (one `litellm repo override` row
per skipped file), unreadable LiteLLM inputs (`secrets.env`,
`extra`), and, when the proxy container is running, a `litellm version` failure
row if the installed version differs from the configured one. It also reports
account instance and login state when the proxy is available for inspection.
A missing login fails doctor only for the account that serves the default
profile, and only when that profile maps a `chatgpt/` route (missing ChatGPT login) or
an `oauth` xAI route (missing xAI login); for another account
whose profiles need a login the row passes and names those profiles. A login
state that cannot be read always fails. Doctor also checks the services ACL and
upstream reachability; `jailbee litellm up` re-resolves the proxy's provider
allowlist when upstream addresses change. `jailbee litellm login [ACCOUNT] [--provider chatgpt|xai]`
refreshes a missing login.
