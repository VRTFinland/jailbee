# Agents in containers

How do I run Claude Code or another coding agent in a container?

A container is a good place for an agent: it works on its own branch, with its
own tools and (in strict mode) a short network allowlist. This page covers the
everyday path: Claude Code, the choice between plain `claude` and `claude-jb`,
picking the agent per repo and per container, and other agents. The reference
pages ([Generic agent support](agents.md), [Claude Code through
LiteLLM](litellm.md)) carry every field and edge case.

## Run Claude Code in a container

1. Enable the preset on the host. The template from `jailbee config init
   --global` already has it; otherwise add this to
   `~/.config/jailbee/global.yaml`:

   ```yaml
   agents:
     claude:
       enabled: true
       # autostart: true   # open a Claude window on every container start
   ```

2. Apply and create a container:

   ```bash
   jailbee apply
   jailbee new my-branch
   ```

3. Attach and talk to the agent:

   ```bash
   jailbee tmux my-branch
   ```

   With `autostart: true` a `claude` window is already waiting. Without it, open
   a shell (`jailbee shell my-branch`) and run `claude`, or start the
   container's agent window on demand with `jailbee agent open my-branch`
   (the container must be running).

**First run.** The first container of a repo has no login yet: complete Claude's
onboarding there. Later containers reuse it, because the login lives in a
credential group shared between containers (see
[Logins](logins.md) for how to manage groups and several accounts). If a login
already exists, JailBee seeds the new config home's onboarding and repo trust
(`seed_onboarding`), so the agent starts straight into work.

**Where it runs.** Claude runs *inside* the container, in the repo checkout the
container has for the branch. Its config home (`~/.claude`) is shared by the
repo's containers: memory, `CLAUDE.md`, skills, plugins and `/resume` carry over
between them. On `jailbee stop` / `restart` running sessions are snapshotted and
resumed in their own windows. In strict mode the Anthropic hosts are added to
the allowlist for you.

**It knows JailBee.** The in-container agent gets JailBee's own skills
(`jailbee-container`, `jailbee-usage`, `jailbee-pr-review`,
`jailbee-issue-management` and more), so it can start services, read logs,
stage issues and pull requests in the outbox and ask for network access
without you explaining the commands. See
[The bundled jailbee skills](agents.md#10-the-bundled-jailbee-skills).

Handing out an unattended agent is a separate decision from enabling it; read
[Running an agent without prompts](security.md#running-an-agent-without-prompts)
before using `--dangerously-skip-permissions`.

## `claude` or `claude-jb`?

Both commands exist in every container and can run side by side. They are the
same Claude Code; they differ in where requests go.

| | `claude` | `claude-jb` |
|---|---|---|
| Models | Anthropic's, through your Claude login | Other providers' models (ChatGPT/Codex subscription, xAI Grok, OpenRouter, any LiteLLM provider) presented to Claude Code as its Fable/Opus/Sonnet/Haiku tiers |
| Needs | A Claude login (credential group) | `litellm.enabled: true` on the host, `jailbee litellm up` and a provider login or key (`jailbee litellm login`), and a golden image that contains the wrapper (`jailbee base build`) plus `jailbee apply` for running containers |
| How it connects | Claude's normal authentication and environment | The wrapper points Claude at the LiteLLM proxy in the `jailbee-litellm` container (`ANTHROPIC_BASE_URL` and a per-container key), then `exec`s `claude`. It only changes its own process environment, never your shell's |
| Choosing the model | Claude Code's own `--model` / `/model` | A **profile** (`-P NAME`, `$JAILBEE_LITELLM_PROFILE`, else `litellm.default_profile`) maps each tier to a route; `--model haiku` still picks a tier, and `-C 1m` picks the context window |
| If the gateway is down | Not affected | Refuses to start (`proxy … is unreachable`); it never falls back to native Claude |
| Pick it when | You use an Anthropic subscription or API access | You want ChatGPT, Grok or other models behind the same Claude Code interface, with one login shared by all containers |

Sessions, history and settings are shared between the two, since both use the
same Claude config home. Inspect profiles with `jailbee litellm ls` on the host
or `claude-jb --help` in a container. Setup, providers and profiles are in
[Claude Code through LiteLLM](litellm.md#setup).

## Choose the agent for a repo and for one container

The **repo default** decides what autostart launches in all of a repo's
containers. Set it from the host, in the repo:

```bash
jailbee agent use claude/opus      # plain claude on the Opus tier
jailbee agent use grok/sonnet      # claude-jb on the "grok" profile, Sonnet tier
jailbee agent use grok             # claude-jb on the profile's default tier
jailbee agent show                 # the effective choice and where it comes from
```

The argument is `PROFILE` or `PROFILE/MODEL`: the profile `claude` means plain
Claude, any other profile means `claude-jb` on that LiteLLM profile. Left out,
`jailbee agent use` asks, offering `claude` plus every profile the repo is
served. The choice is written to the repo's host-local config
(`agents.default_runner`); containers' `claude-jb` default profile is resynced,
and running agent sessions are not touched. `jailbee agent use --clear` removes
the local choice: a repo or global `agents.default_runner` then applies, and if
none is set, the older `litellm.autostart` / `litellm.default_profile` path. A `claude-jb` default is ignored (with a warning in the autostart log)
while `litellm.enabled` is off.

A **container override** changes one container only, for its next session:

```bash
jailbee agent use claude/haiku --container my-branch
jailbee agent use --container my-branch --clear   # follow the repo default again
jailbee agent open my-branch                      # start the effective agent now
```

`agent open` starts it in a new tmux window of a running container right away
and leaves running agent windows alone. With an override set, an initial
`--prompt` and `service refresh` go to `claude` whatever `primary_agent` says.

In the terminal dashboard the same choices are menu entries: the repo menu's
`Agent…` (key `g`) asks for a profile and then a tier, with the current choice
marked; the container menu has `Agent for this container…` (with a "repo
default" row that removes the override) and `Open a new agent window now`. The
repo row shows the effective agent as `profile/model`, for example
`claude/opus`; with nothing set it shows the older launch in parentheses. They
run the commands above, so nothing here is dashboard-only. See
[`agents.default_runner`](config.md#agentsdefault_runner) for the full
resolution order and [`jailbee agent`](commands.md#jailbee-agent) for the
command reference.

## Other agents

Six presets ship: `claude`, `codex`, `gemini`, `aider`, `opencode` and `grok`.
Claude is the recommended and best-verified one; the others are best-effort
templates you may need to adjust. Two lines enable one:

```yaml
agents:
  codex:
    enabled: true
    autostart: true
```

The preset supplies the install command, the shared config mount (`~/.codex`)
and the egress hosts. Authentication differs per agent: `codex` can use a
ChatGPT login or `OPENAI_API_KEY`, `opencode` keeps its login **per container**
(so sharing it would share credentials), and so on; the table is in
[Authentication in a container](agents.md#7-authentication-in-a-container).
With several agents autostarting, `primary_agent` decides which one receives a
`jailbee new --prompt` first prompt ([details](agents.md#primary-agent-and-initial-prompt)).

To run `claude-jb` in its own window beside plain Claude, add it as a second
agent ([Several Claude launchers](agents.md#several-claude-launchers)).

### Add your own

An agent name that is not a preset starts from nothing: give it a `command`, an
`install` command, the hosts it needs in `egress_allow` and the paths to
share:

```yaml
agents:
  myagent:
    enabled: true
    autostart: true
    command: myagent
    install: npm install -g myagent
    egress_allow: [api.example.com]
```

[Writing your own agent](agents.md#4-writing-your-own-agent) lists every field
and [Finding an agent's hosts](agents.md#6-finding-an-agents-hosts) shows how to
discover the egress entries it needs under strict mode.

## More

- [Logins and credential groups](logins.md) — several accounts, sharing a login between repos
- [Generic agent support](agents.md) — every `agents:` field, presets, Claude specifics
- [Claude Code through LiteLLM](litellm.md) — `claude-jb`, providers, profiles
- [Commands](commands.md#jailbee-agent) — `jailbee agent`
- [Getting started](getting-started.md#claude-code-in-the-container)
