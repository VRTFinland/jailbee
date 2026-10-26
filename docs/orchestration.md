# Container orchestration

A human can give one container the right to **orchestrate**: its agent then
starts child containers, talks to them, brings their commits into its own
repo and cleans up after them, without asking anyone each time. The grant is
set on the host and bounds everything the agent can do.

## Overview

The motivating workflow is a bug backlog. An orchestrator container reads the
tickets from GitHub (the container's `gh` is read-only, which is enough),
starts one child per bug, each with its own prompt, and waits for the
children's "done" messages. For each one it imports the child's branch,
merges it, and destroys the child to free a slot for a queued one. At the end
it offers the combined work as a pull request through the existing
[PR outbox](commands.md), which a human publishes.

The container never gets host access. Every action is a manifest the agent
writes into an outbox directory in the container; a host-side
[broker](#the-broker) validates it against the grant and acts. Manifests are
narrow, validated schemas, never shell strings.

Roles:

- **Orchestrator**: a container that holds a grant. It may do what the grant
  permits.
- **Child**: a container the broker created for an orchestrator. It can only
  send messages to that orchestrator. A child never orchestrates.
- **Ordinary container**: neither. Nothing changes for it.

Child containers are not a substitute for an agent's own subagents. The
bundled `jailbee-orchestration` skill tells the orchestrator to keep ordinary
delegation (research, review, plan tasks) on its subagent tool and to spawn
children only when the user asks for them: a grant permits orchestration, it
does not request it. This matters most when `default_profile` grants every
new container.

## Grants

A grant is stored in the orchestrator's Incus config
(`user.jailbee.orchestration.grant`), which the container cannot write, so it
cannot widen its own grant. It holds:

```yaml
max_running_children: 4
operations: [spawn, message, import, restart, stop, destroy]
grantable:
  mounts: [data, docs]                  # names of the repo's optional mounts
  loose: false                          # may set a child to loose network
  egress: ["pypi.org", "*.npmjs.org"]   # extra egress hosts for children
```

`spawn` and `message` are implied by any grant. A new grant without
`--profile` starts from the configured
[`orchestration.default_profile`](config.md#orchestration), so the defaults are
yours to set. Without one, fields you omit default to 4 running children,
operations `spawn message import restart stop destroy`, nothing grantable.
Leave `destroy` out with `--deny destroy`.

```bash
jb orchestrate grant bugs --children 4 --mount data --egress pypi.org
jb orchestrate grant bugs --profile bugfixer     # start from a configured profile
jb orchestrate show bugs      # the grant, what this repo leaves of it, running children
jb orchestrate ls             # every container of this repo with a grant (--json)
jb orchestrate revoke bugs
```

`grant` takes `--profile`, `--children`, `--allow OP` / `--deny OP`,
`--mount M` / `--clear-mounts`, `--loose` / `--no-loose` and `--egress H` /
`--clear-egress`; each repeatable flag can be given several times. Fields you
do not name keep their current value (or the profile's, or the defaults).
Granting again edits the grant. A missing container is picked
interactively, as for every command; `revoke` always shows the picker.
`jb orchestrate grant` also makes sure the [broker service](#the-broker) is
running.

Over remote SSH, `grant` needs the host's `remote.ssh.orchestration` setting
(`off` by default). `ssh` limits the grantable mounts, loose network and egress
to what the session could do itself (`remote.ssh.mounts`, `remote.ssh.network`;
`mount_roots` never); `orchestrator` limits them to the orchestrator's
permanent rights. The child limit and operations are always adjustable,
narrowing is always allowed, and elements the stored grant already holds are
kept. The check happens when the grant is made, so later turning the remote
settings off does not narrow stored grants. See
[Security](security.md#remote-ssh) and [config](config.md#remotessh).

Revoking stops new spawns and other operations and drops queued spawns.
Existing children are untouched and stay children.

### Profiles

Named profiles and an optional default live in the `orchestration:` block of
the global config and the host-local config (`jb config edit --local`):

```yaml
orchestration:
  profiles:
    bugfixer:
      max_running_children: 5
      grantable: { mounts: [data] }
  default_profile: bugfixer   # applied to every new container when set
```

A profile is copied into the grant when it is applied. Editing the profile
later does not change grants already given; running `jb orchestrate grant
--profile` again does. `default_profile` is applied when a container is
created, never to a child, and not to a mount-mode container. Where a local
profile has the same name as a global one, the local one wins.

The repo layer (`.jailbee/config.yaml`) is different: it can only **narrow**.
It cannot define profiles or a default, but may set:

```yaml
orchestration:
  allowed: false              # no container of this repo may orchestrate
  max_running_children: 2     # cap
  mounts: [docs]              # only these mounts may ever be granted to children
  egress: ["pypi.org"]        # only these egress entries
  loose: false                # children may never be loose in this repo
```

The reason is that a mount-mode container can write the host checkout's repo
config, so a widening there would let a container raise its own rights. The
cap applies when the grant is made and again at every broker check;
`jb orchestrate show` prints the effective grant when it differs.

A grant is refused for a mount-mode container (it can neither fork nor
import), for a child, and for a container of a repo that is not registered
(`jb apply` registers it).

### Child rights

When the orchestrator spawns a child it names the child's rights: `mounts`,
`loose` and `egress`. The broker refuses the spawn if any right exceeds the
grant:

- each mount must be in `grantable.mounts` and be one of the repo's optional
  mounts;
- `loose` only if `grantable.loose` is true;
- each egress host must match a `grantable.egress` entry (the same matching as
  `egress_allow`).

All of it is also subject to the repo's cap. A child gets exactly these rights
and nothing else: it inherits only git commits from the orchestrator. Mounts
are attached persistently and loose is its network mode, with no timeout.

The child's own head is treated as untrusted. If the branch it starts from
contains an [autostart](config.md) step that wants `network: loose` or a
mount, the spawn is accepted only when the child's rights already include that
mount, and, for a loose step, `loose`. Otherwise the spawn is refused with the
reasons, so an orchestrator cannot commit its way to more than its grant.

## Primary agent and initial prompt

A child starts its work from an initial prompt. The prompt goes to the repo's
**primary agent** as its first message, on the command line at launch. It is
sent once: later starts of the container run the plain agent command, so a
restart never repeats a task.

- `primary_agent: auto` (default) or an agent name picks which agent that is.
- `agents.<name>.prompt_args` says how the agent takes a first message. The
  presets for `claude`, `codex` and `opencode` set it.
- `jb new <branch> --prompt FILE|-` and `jb fork <source> <branch> --prompt
  FILE|-` use the same mechanism by hand.
- The prompted agent launches only after every autostart stage, deferred
  ones included, has run, so a child never starts its task before its
  container is ready. A failed stage stops the run before the agent
  launches, and the spawn is reported `failed`.

An agent without `prompt_args` cannot take a prompt; the request is refused
up front. See [Primary agent and initial
prompt](agents.md#primary-agent-and-initial-prompt) and the
[`primary_agent`](config.md#primary_agent) key. For a child, the broker
prepends a short fixed preamble naming its orchestrator and the child skill,
and sets `JAILBEE_ROLE=child` and `JAILBEE_ORCHESTRATOR=<name>` in the
agent's environment.

## Runner: which agent a child runs

A `spawn` or `restart` manifest may carry an optional `runner`: `agent`
(`claude` or `claude-jb`), `profile` (a LiteLLM profile, `claude-jb` only) and
`model` (`fable`, `opus`, `sonnet` or `haiku`). It rewrites the Claude
command of the repo's `agents.claude.command` for that child. Omitted, the
child runs the repo's primary agent as configured. On `restart`, an absent
`runner` keeps the child's previous one and `"runner": null` clears it. The
runner is stored on the child as a label and shown on its dashboard card.

A runner that cannot be honoured (Claude not enabled, no `prompt_args`, a
command that is not `claude`/`claude-jb`, LiteLLM disabled, an unserved
profile or a tier the profile lacks) is refused with the reason; it never falls
back to the default. Orchestrators discover the options with
`jailbee-inbox runners`, and are told to omit `runner` unless the user (or
the user's own instructions) asked for a specific agent, profile or model.
The default agent is reported only when it is known (it is set at agent launch
in a container holding a grant); otherwise `jailbee-inbox runners` prints
`default: omit runner (unknown)` and the orchestrator simply omits `runner`.

## The broker

The broker is a systemd user service, `jailbee-broker.service`. `jb init` /
`jb setup` install and enable it, and `jb orchestrate grant` starts it if it
is not active. It is always on: every ~5 seconds it checks each running
orchestrator and child for new manifests, and with no running orchestrator or
child it slows to one check every 60 seconds. `jb doctor` reports whether the
unit is active. Container-creation workers it starts outlive a broker restart.

For each manifest the broker validates it, acts on it at most once, deletes
it, and answers with a `status` message in the sender's inbox: `queued`,
`started`, `done`, `failed` or `refused`, with a detail. A spawn ends with
`done` (``child `<short>` is ready``) or `failed`; a restart reports `done`
or `started` and no final status. A child whose prompted agent exits 1 to 127
sends its orchestrator a `blocked` message ``[jailbee] the agent `<name>`
exited with status <rc>``. At most 32 manifests per
container are read per round.

The broker is non-interactive. Anything that would prompt the operator (the
autostart escalation gate for an untrusted source, an autofetch that needs a
hardware-key touch) is refused with that reason; the broker never answers a
prompt for you. Messages for a stopped recipient wait in the broker's on-disk
queue and are delivered when it runs again, also across broker restarts.

Inside each orchestrator and child, `/home/dev/.jailbee/orchestration/` holds
`outbox/` (container to host), `inbox/` (host to container) and, for an
orchestrator, `grant.json`, a read-only copy of its limits. The golden image
ships `jailbee-inbox` (`wait`, `ls`, `read`, `runners`, `send`) so agents need not write
JSON by hand; rebuild with `jb base build` to get it.

## Lifecycle

- **Spawn.** `kind: fork` (from the orchestrator's committed work) or `kind:
  new` (from a base), plus the prompt and rights. Only **running** children
  count against `max_running_children`; a child being created counts as
  running. Beyond the limit the spawn is queued in order, reported as
  `queued at position N`, and started when a slot frees. A queued fork pins
  its source commit when it is queued.
- **Done.** A child reports `done` when finished. If its work tree is dirty or
  its HEAD is detached, the broker refuses with `uncommitted changes` (or
  `detached HEAD`) and does not relay it. Otherwise the broker fetches the
  branch to the host and relays the message. The child keeps running.
- **Import.** `import` brings a running child's branch into the orchestrator
  at `refs/jailbee/from/<child>/<branch>`, without merging. The orchestrator
  merges or cherry-picks from that ref itself; conflicts are its to solve.
  A stopped child is refused ("restart it first"). The orchestrator needs a
  clean tree and must not be a mount-mode container.
- **Stop** a child; **restart** a stopped child with a new prompt (it counts
  against the limit and is queued if the limit is full). A running child is
  refused ("stop it first").
- **Destroy** (needs `destroy` in the grant) works on a running or stopped
  child (stopping first is the recommended order) and is refused while the
  child has commits never imported: its HEAD must equal the last import. A
  stopped child's HEAD is the one recorded when the broker stopped it; a
  child stopped any other way is refused until restarted.
  `force: true` overrides the guard.
- **Messages** go both ways with replies (`note`, `question`, `blocked`,
  `done`). A child can only message its orchestrator, and an orchestrator only
  its own children.

If the orchestrator is stopped or destroyed, nothing happens to its children.
They keep running, and their messages wait until it returns. This is an
error state for a human to resolve.

## Typical workflows

In both, one child takes one whole unit of work: a ticket or a plan. Inside
its own container the child is free to use its own subagents for the steps
of that unit; the orchestrator never splits a plan's tasks across children.

**A bug backlog.** The user asks the orchestrator to work through clear bug
tickets on GitHub. The orchestrator reads them with the read-only `gh`,
spawns one child per ticket (`fix/issue-<n>`) whose prompt carries the
ticket text and the acceptance tests, and lets each child investigate, fix
and test. As each reports `done` it imports and merges the branch, then
stops (or destroys) the child so the next queued ticket starts. Forking
each new child from the orchestrator's latest merged HEAD keeps later
fixes from conflicting with earlier ones.

**A series of plans.** The user hands over a higher-level plan that orders
several implementation plans (for example written with the superpowers
`writing-plans` skill), some independent and some depending on others. The
orchestrator spawns a child per plan whose dependencies are met, one per
free slot. When a child is done it imports and merges the result, resolves
any conflicts itself, commits, and only then forks the plans that depended
on it, so they start from the merged work. It continues step by step until
the higher-level plan is complete.

A child inherits only git commits, so a plan must reach it either committed
in the repo or in a directory every container sees through a host mount
(`host_mounts`), such as a shared `.local/`. A plan in a gitignored
directory without such a mount is not there in the child; it would have to
travel in the prompt, which is capped at 64 KiB. When children share such a
directory, each should write only its own files in it.

## Skills

Two skills teach the agents the protocol, so they cooperate without help:

- `jailbee-orchestration` for the orchestrator: reading `grant.json`, writing
  a good child prompt, choosing child rights, the inbox loop, importing,
  merging and destroying children, and finishing with a PR.
- `jailbee-orchestration-child` for the child: that it is a worker, how to send
  `note`, `question`, `blocked` and `done`, and that everything must be
  committed before `done`.

They are bundled in `docs/skills/` and copied to every enabled agent by
`jb apply` and `jb new`, like the other [bundled
skills](agents.md#10-the-bundled-jailbee-skills).
