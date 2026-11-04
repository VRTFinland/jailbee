# Container orchestration

A human can give one container the right to **orchestrate**: its agent then
starts child containers, talks to them, brings their commits into its own
repo and cleans up after them, without asking anyone each time. The grant is
set on the host and bounds everything the agent can do.

## Child GitHub proposals

Children cannot stage GitHub writes, including after adoption when the role
marker identifies them as children. They send the exact repository, target and
content to their orchestrator with `jailbee inbox send message --text "..."`
(or `--kind question` when a result is needed). The orchestrator stages the
proposal in its own outbox with `jailbee issue` or `jailbee pr`; the human
reviews and publishes on the host. Never write GitHub outbox files or create
outbox directories by hand. See [container commands](commands.md#in-container-outbox-commands).

On done, pending child GitHub proposals cause the broker to keep the child and
report the leftovers even when its policy is already keep. Broker stop records
the pending count; an externally stopped child without a recorded count has
unknown outbox state and is conservatively kept for inspection.

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
- **Child**: a container the broker created for an orchestrator or the host
  user adopted into its topology. It can only send messages to that
  orchestrator. A child never orchestrates.
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
operations: [spawn, message, import, restart, stop, destroy, rights, refresh]
grantable:
  mounts: [data, docs]                  # names of the repo's optional mounts
  loose: false                          # may set a child to loose network
  egress: ["pypi.org", "*.npmjs.org"]   # extra egress hosts for children
```

`spawn` and `message` are implied by any grant. A new grant without
`--profile` starts from the configured
[`orchestration.default_profile`](config.md#orchestration), so the defaults are
yours to set. Without one, fields you omit default to 4 running children,
operations `spawn message import restart stop destroy rights refresh`, nothing grantable.
Leave `destroy` out with `--deny destroy`.

```bash
jb orchestrate grant bugs --children 4 --mount data --egress pypi.org
jb orchestrate grant bugs --profile bugfixer     # start from a configured profile
jb orchestrate show bugs      # the grant, what this repo leaves of it, running children
jb orchestrate ls             # every container of this repo with a grant (--json)
jb orchestrate revoke bugs
jb orchestrate adopt bugs worker1 worker2
jb orchestrate release worker1
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
(`ssh` by default; `off` refuses it). `ssh` limits the grantable mounts, loose network and egress
to what the session could do itself (`remote.ssh.mounts`, `remote.ssh.network`;
`mount_roots` never); `orchestrator` limits them to the orchestrator's
permanent rights. The child limit and operations are always adjustable,
narrowing is always allowed, and elements the stored grant already holds are
kept. The check happens when the grant is made, so later turning the remote
settings off does not narrow stored grants. See
[Security](security.md#remote-ssh) and [config](config.md#remotessh).

Revoking stops new spawns and other operations and drops queued spawns.
Existing children are untouched and stay children.

### Publish the orchestrator's own outboxes

Publishing is opt-in, separate from child `operations`. A grant without
`publish` has no publish rights; children never receive them. The broker uses
the host user's GitHub/git credentials and the same locks, freshness checks,
expected fields, receipts and journals as manual publication.

```yaml
publish:
  issues: [create, edit, comment, labels, assign_self, state]  # own repo and declared submodules
  foreign_issues:
    repos: [someorg/upstream-lib]               # case-insensitive owner/name, or ["*"]
    actions: [comment]
  pull_requests: true
  pr_reviews: false
  max_actions_per_hour: 30                      # 1..500
```

```bash
jb orchestrate grant bugs --publish-issues comment,state --publish-prs
jb orchestrate grant bugs --publish-foreign-issues someorg/upstream-lib:comment
jb orchestrate grant bugs --publish-pr-reviews --publish-rate 10
jb orchestrate grant bugs --no-publish
```

Assignment has two kinds. `assign_self` covers only assignees changes whose
logins all equal the publishing account, compared case-insensitively after
`@me` is resolved. That includes removal: `assign_self` lets the orchestrator
unassign the host user (`remove: ["@me"]`), also from issues the user
claimed by hand. `assign` covers any login and implies `assign_self`. A
repo cap can narrow `assign` to `assign_self`. Publishing needs `assign` when
an action adds or removes another login, or when no publisher login is known.
Over remote SSH, adding either kind, or going from `assign_self` to `assign`,
counts as widening.

`--publish-issues none` clears issue rights. Repeatable
`--publish-foreign-issues REPO:KINDS` replaces the foreign block; every listed
repository must use the same kinds. `--publish-prs/--no-publish-prs` and
`--publish-pr-reviews/--no-publish-pr-reviews` toggle their rights.
`--no-publish` clears everything and is exclusive with other publish flags.
A bare `grant` keeps existing publish rights. `show` and `ls` display them.
Profiles may carry the same block; the repo cap intersects rights and takes
the smaller rate, while `publish: false` forbids publication.

Every publication the broker performs is recorded in the Sent log with publisher
`orchestrator` (`jb outbox sent ls`; see
[git-bridge](git-bridge.md#the-sent-log)).

After staging a manifest in its own issue or PR outbox, the orchestrator sends:

```bash
jailbee inbox send publish --issue close-12.json --wait
jailbee inbox send publish --pr imported-branch.json --wait
```

The version-1 protocol request is
`{"version":1,"type":"publish","id":"publish-unique","outbox":"issue","manifest":"close-12.json"}`.
The target is a bare JSON file name, never a path or `.progress.json` file.
The broker checks every pending action before acceptance and again under the
domain lock. Any uncovered kind or repository refuses the whole request.

Acceptance replies `started`; a later `done` lists result URLs and created
issue numbers in manifest order. `--wait` can return at `started`: read the
final status for the same ref through the ordinary inbox. A worker failure
replies `failed`. An uncertain outcome, including a broker restart during
publication, is never retried automatically: ask the human to inspect
`jb outbox` on the host. Use a new request id for a deliberate retry.

Source publication cannot force-push, use the default/base branch as head,
or overwrite an existing upstream head unless it belongs to a PR bound to
this container. New PRs use the default base; bound updates keep that PR's
base. Updates require an authored `BranchPrMap` binding created by this
container's source publication; review-only PR/stacked labels and adopted
bindings do not authorize pushes. PRs opened here retain authorship for later
updates. Updating any PR opened some other way remains a host step. Review comments and replies target only bound PRs in the own repository,
never foreign repositories or submodules. Description rewrites additionally
require an authored, non-adopted source-PR binding; review-only labels do not
authorize editing someone else's title or body.

Only one publish runs per orchestrator. Its full pending-action count is
reserved against a sliding-hour cap before starting, including failed or
partially applied attempts; reservations are never refunded. One source
publish costs one action. Revocation or `--no-publish` refuses new requests;
an already accepted worker finishes and reports its result.
The broker writes finished requests, action kinds/repos, URLs and outcome to
`publish-log.jsonl` under its state root (`~/.local/state/jailbee/orchestration/`).
Existing outbox receipts and history remain available through `jb outbox`.
A worker shutdown that leaves a review outcome uncertain also leaves a
host-side block for that instance and manifest name: further broker requests
for it are refused until the human resolves the outbox on the host. A manual
host publication is not subject to this broker block. Do not rename and
resubmit an uncertain manifest. The broker waits up to 25 seconds for workers
on shutdown/upgrade; later recovery never automatically retries a cut request.

Over SSH, granting publish rights requires the session's command policy to
permit the equivalent manual operation: `issue apply`, `pr push
--source-manifest`, or `review apply`. The same router policy checks these
permissions; enabling `orchestrate grant` alone does not unlock publishing.

### Adopt running containers

The host user can adopt existing containers into an orchestrator's topology,
including stopped containers. The orchestrator must hold an effective grant;
each child must belong to the same repo and must not be mount-mode, hold a
grant, have children, or already belong to a different orchestrator. Adoption is
refused if the resulting number of running children exceeds the effective cap.
An existing child of the same orchestrator is left unchanged.

Adoption clears stale child-state labels, records the parent and sets the
on-done policy to keep. It sends an `adopted` inbox message to each side, but
does not create an import baseline or runner, relaunch an agent, or start
supervision. Destruction therefore requires an import first or `--force`.
To supervise a hand-started agent, ask the user to close it before using
`jailbee inbox send restart --child C --agent-only`; otherwise a second agent
may be launched. `JAILBEE_ROLE=child` is set on the next agent launch, not in
the existing session.

`jb orchestrate release` removes the relationship, child-state labels, watch
and pending state. It revokes orchestrator-owned rights but leaves user-owned
rights intact, and refuses while an autostart run is live. Release removes the
child role marker before clearing the parent label. A stopped child retains
its relationship: start it and retry release so marker cleanup can be verified.
Cleanup failures likewise retain the parent for retry. An agent launched with
`JAILBEE_ROLE=child` must be relaunched after release to drop its old environment.
If no child is
named, release always prompts and is refused without an interactive terminal.
Over remote SSH, adoption requires `remote.ssh.orchestration` to be enabled;
release is allowed as a narrowing action without that setting.

### Profiles

Named profiles and an optional default live in the `orchestration:` block of
the global config and the host-local config (`jb config edit --local`):

```yaml
orchestration:
  profiles:
    bugfixer:
      max_running_children: 5
      grantable: { mounts: [data] }
      default_runner: { agent: claude, model: opus }
  default_profile: bugfixer   # applied to every new container when set
```

A profile may set `default_runner` for children of its holder; the runner is
copied into the container preference along with the profile's rights.
A profile is copied into the grant when it is applied. Editing the profile
later does not change grants already given; running `jb orchestrate grant
--profile` again does. `default_profile` is applied when a container is
created, never to a child, and not to a mount-mode container. Where a local
profile has the same name as a global one, the local one wins.

Save a container's stored grant as a profile with `jb orchestrate profile save
bugs reviewer`. The profile is written to `global.yaml`; `--force` replaces an
existing global profile. Saving is refused while `repos/<prefix>.yaml` defines
that name, because the host-local profile would override it.

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
  `egress_allow`), or effective `grantable.loose` must be true and the entry
  covered by the repo's egress cap (if one is set). Loose implies narrower
  egress permission for both spawn and runtime grants.

All of it is also subject to the repo's cap. A child gets exactly these rights
and nothing else: it inherits only git commits from the orchestrator. Mounts
are attached persistently and loose is its network mode, with no timeout.

The child's own head is treated as untrusted. If the branch it starts from
contains an [autostart](config.md) step that wants `network: loose` or a
mount, the spawn is accepted only when the child's rights already include that
mount, and, for a loose step, `loose`. Otherwise the spawn is refused with the
reasons, so an orchestrator cannot commit its way to more than its grant.

### Change runtime rights

The `rights` operation (included in new grants by default) lets an orchestrator
change its own child's rights, never its own. Granting uses the current grant
and repo cap, just like spawn; revoking requires an active grant but not the
`rights` operation. A removed grant disables both.

```bash
jailbee inbox send rights --child fix-12 --egress pypi.org --for 5m --wait
jailbee inbox send rights --child fix-12 --mount aws --for never --wait
jailbee inbox send rights --child fix-12 --revoke-mount aws --wait
```

Every grant requires `--for`: a positive duration, at most 24h, or `never`.
One duration covers all granted items. Prefer an egress host over loose, a
bounded duration over `never`, and revoke when the need ends. The existing
60-second refresh timer takes timed rights back, including timed egress;
clock deadlines survive host restarts. Stopped containers keep the changes
for their next start, and mounts are attached as instance devices.

Revocation touches only rights orchestration gave, not rights a human added
by hand; a right whose live expiry no longer matches the recorded deadline
counts as the human's. A grant never touches a right held by hand (it is
reported as `held by hand; left as is`). A timed grant never shortens a right
orchestration owns: the later deadline wins, and `never` removes an expiry. Spawn and permanent
runtime rights live in the child rights label; timed provenance is separate.
There is no restart-time escalation check. Requests are preflighted before
changes, revoke before grant, and roll back on application failure (best effort).
Applied requests are journalled; no human notification is sent. Existing
network/mount views reflect the changes.

A child may send a revoke-only `rights` manifest for itself (omit `--child`
or name itself). It cannot include `grant` or name another child; the refusal
is `children may only revoke their own rights`. Successful relinquishment
sends its orchestrator a note. Requests for additional access stay `question`
messages.

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

## Fork warm paths

Fork spawns seed the source's Git-ignored build outputs from `fork.warm_paths`
using btrfs reflinks, before mounts, caches and autostart. They seed HEAD forks
only: a dirty source, a source that moved past the pinned fork commit, a non-HEAD
`--ref`, or another storage driver is skipped with an advisory. A reflink failure
never falls back to a full copy and never fails the fork. While warming runs,
the source's entire repository snapshot is visible read-only to the child,
including ignored secrets such as `.env`; fork only to trusted children.
Paths equal to or below later mount targets are skipped rather than copied
into storage that would immediately be hidden.

For `jailbee inbox send spawn`, repeatable `--warm PATH` replaces the configured
list; `--no-warm` disables it. The flags are mutually exclusive and are refused
with `--new` or `--pr`. The version-2 fork manifest field is `warm_paths`:
omitted uses configuration, `[]` disables seeding, and a non-empty list replaces
configuration. Patterns are repo-relative globs of ignored paths (`*` does not
cross `/`; `**` matches zero or more segments). Unmatched patterns get a note.
See [`fork`](config.md#fork).

## Autostart stage selection

`spawn` and `restart` accept optional `autostart_profile: "NAME"`, also
available as `jailbee inbox send spawn|restart --autostart-profile NAME`.
Names are validated against the host repo's [autostart profiles](config.md#profiles).
Spawn passes the choice to creation; restart stores it before booting, runs
missing one-shot stages, then the selected start stages in a background boot
worker. Ordinary restarts return `started` with a log path, keeping the
broker responsive and reserving the child's capacity while it boots. A later
setup failure is also recorded in the job and log and reaches the orchestrator
as a second status, `failed` (``restart of child `x` failed: ...``), for the same ref;
it does not free a running child's slot.
Queued requests
remember the selection and revalidate it before starting. An unknown name is
refused with ``autostart profile `NAME` is not defined``. This field selects
stages, not the agent or its LiteLLM profile; omit it to use the creation
default or keep a child's existing choice on restart.

For a light child, `jailbee inbox send spawn --branch task --services web --wait`
starts only `web` and its dependencies; restart also accepts `--services`.
The equivalent manifest field is `services: ["web"]` (at most 32 names).
An empty list or `--services ''` starts none. Omission uses the profile or all
services on spawn and preserves an explicit selection on restart. An explicit
selection overrides the profile's service set only; nonservice stages still follow
the profile. Services are prepared once per container and service, including
newly selected dependencies. Child preparation cannot exceed its stored rights.
For ordinary containers, preparation cannot exceed the elevation approved at
creation; legacy containers fall back to host-effective declarations. Re-assess
the configuration and use an accepted host or orchestrator refresh to widen
approval for successfully prepared services. An in-place profile switch starts safe services but
defers unprepared elevated services and their dependents until the next restart. Neither `services` nor `--services` can be combined with
`agent_only` / `--agent-only`.

## Refresh a child's services

After `update --merge` changes dependencies or migrations, or when a child asks,
use `jailbee inbox send refresh --child C --services db,web --wait`. The
`refresh` operation must be granted. For an existing grant without it, run
`jb orchestrate grant ORCHESTRATOR --allow refresh` on the host; a bare grant
preserves the existing operations. Final status reports per-service outcomes,
including cascaded restarts. The
accepted plan may contain child-edited commands: the orchestrator authorizes
their execution within the grant and repository cap. Restricted SSH cannot run
host service refresh, even with network and mount unlocks.

If a managed agent is running, an elevated refresh requires its known session and
idle state, pauses it, restores the network and mounts, then resumes the same
session, even if prepare failed. A failed prepare skips later prepares and the
restart; the resumed agent receives the outcomes and a continuation prompt.
Unknown agent state or session, insufficient rights, a busy child or a foreign
live agent causes refusal. Use `--force` only after the child confirms
its work is paused or stopped; it bypasses only the idle check. It never permits
interrupting a working child. On the host `--yes` only consents to foreign
agents remaining active during elevation; it does not force the idle check.

If restoration cannot be confirmed, resume is withheld and the failure reports
recovery commands such as `jb net strict` and, when needed, mount removal. If
supervision registration fails after restoration, resume is also withheld; the
failure preserves service outcomes and reports a recovery command. Once safe,
recover supervision with `jailbee inbox send restart --child C --agent-only
--resume SESSION --text 'Continue your task.'`.

The detached refresh worker continues across a broker restart; a final status
for a stopped orchestrator is parked for delivery. A retry of a saved final
status retries delivery only, not prepare. If execution was interrupted and its
outcome is uncertain, inspect the child and its services manually before sending
another refresh: prepare commands such as migrations might already have run.
Durability across host power loss is not guaranteed.

## Reuse a running worker without autostart

Use `jailbee inbox send restart --child C --agent-only --text "Next task"`
(or manifest `"agent_only": true`) for a persistent worker kept with
`on_done: "keep"`. The child must be RUNNING and idle: either its `done`
was accepted, or its previous monitored agent has ended (the broker reported,
or would report, `blocked`). A working child or uncertain agent liveness is
refused; there is no force option. Active startup workers also prevent relaunch.
Without the flag, a running child is still refused with "stop it first".
A kept worker's agent keeps `jailbee inbox wait --timeout none` in the
background between tasks, re-arming it after handling messages.

Only the agent tmux window is replaced. Container boot, setup stages and all
`on_start` actions are skipped; existing services stay running. The fresh
session receives the prompt with the usual child preamble as its first input.
Runner options work exactly as ordinary restart: omit to keep, name
`--agent`/`--profile`/`--model` to change, or use `--default-runner`.
`--on-done` may change the completion policy. `--autostart-profile` is refused
with `--agent-only`, since no autostart runs.

The child already occupies its running slot, so this action adds no slot or
reservation and never queues, even at the running-child limit. It returns
`started`, followed by `done` after ready-and-alive confirmation; an early
failure returns `failed`, and later exit without accepted child `done` sends
`blocked`, just like spawn/restart. For shared-state rework, send `update`
first and include merging the imported ref in the new task; the broker never
merges it itself.

## Runner: which agent a child runs

A `spawn` or `restart` manifest may carry an optional `runner`: `agent`
(`claude` or `claude-jb`), `profile` (a LiteLLM profile, `claude-jb` only) and
`model` (`fable`, `opus`, `sonnet` or `haiku`). It rewrites the Claude
command of the repo's `agents.claude.command` for that child. When omitted on
spawn, the container default applies, then the local, repo, or global
`orchestration.default_runner`, in that order; without any default the child
runs the repo's primary agent as configured. Set the container preference with
`jb orchestrate grant bugs --runner-agent claude-jb --runner-profile grok --runner-model sonnet`;
`--runner-profile` and `--runner-model` require `--runner-agent`. Use
`--clear-runner` to remove the container preference and fall back to config.
Reapplying `--profile` also clears the container preference unless a runner is
specified or the profile names one. `jb orchestrate show` and `ls` display the
effective preference; the dashboard offers a default runner entry. This preference grants no rights.
On `restart`, an absent `runner` keeps the child's previous one, while
`"runner": null` selects the current effective default, or clears the runner
if no default is configured. The runner is stored on the child as a label and
shown on its dashboard card.

A runner that cannot be honoured (Claude not enabled, no `prompt_args`, a
command that is not `claude`/`claude-jb`, LiteLLM disabled, an unserved
profile or a tier the profile lacks) is refused with the reason; it never falls
back to the default. Orchestrators run `jailbee inbox runners` first to see
the configured default and available profiles and tiers. Omitting `runner` on
spawn starts the default. They may choose a cheaper tier for routine,
well-specified work or a stronger tier for hard or open-ended work, following
the user's model guidance; when in doubt they use the default. They keep the
default's agent and profile unless the guidance says otherwise. A restart
without `runner` keeps the child's runner; `--default-runner` selects the
current default. A refusal is reported to the human, not retried with another
model. Without a configured default, the command shows `default: omit runner`
and the launch agent, or `unknown` when that agent is not known.

## The broker

The broker is a systemd user service, `jailbee-broker.service`. `jb init` /
`jb setup` install and enable it, and `jb orchestrate grant` starts it if it
is not active. It is always on: every ~5 seconds it checks each running
orchestrator and child for new manifests, and with no running orchestrator or
child it slows to one check every 60 seconds. `jb doctor` reports whether the
unit is active. Container-creation workers it starts outlive a broker restart.
When a different jailbee version is installed, the broker finishes its round
and exits with status 75, and systemd starts it again on the new code;
`make install` restarts it as well, which also covers a reinstall of the same
version. A unit written by an older `jb setup` restarts it too, but logs the
exit as a failure until `jb setup` rewrites the unit.

The broker logs to the user journal (`journalctl --user -u jailbee-broker`).
Each creation or restart worker it starts writes its own output to
`~/.local/state/jailbee/logs/<container>-<timestamp>.log` (`$XDG_STATE_HOME`
when set). A `failed` status whose worker left no error does not name that
file, since statuses reach the container; the broker log does.

For each manifest the broker validates it, acts on it at most once, deletes
it, and answers with a `status` message in the sender's inbox: `queued`,
`started`, `done`, `failed` or `refused`, with a detail. Spawn and restart
return `started` (or `queued`, then `started`), followed by `done`
(``child `<short>` is ready; agent is alive``) only after setup finishes and
the actual prompted process survives two observations at least two seconds
apart. Failure to start within 120 seconds after setup, an immediate exit,
or a failed boot/setup returns a final `failed` for the same ref. A child that
already reported done before readiness was confirmed still gets a final `done`
(``child `<short>` is ready; it already reported done``), so the ref always ends.

The broker continues monitoring that process. Any exit without an accepted
child `done` sends the orchestrator a `blocked` message, including exit 0,
nonzero exits and signals. The detail includes the exit status and signal
name when known, and up to 4096 characters from the last 30 tmux output
lines when the pane survives. If the wrapper itself or the container dies,
the broker reports that the process disappeared; an unavailable exit status
is explicitly unknown. Accepted `done` and successful broker `stop`/`destroy`
end monitoring; a restart starts a fresh run. External kills/stops are not
silenced. A refused `done` does not finish the run. Transient probe errors
are retried, not misreported as death. Existing children need a broker
restart request to acquire the new supervision. At most 32 manifests per
container are read per round.

A live prompted agent that waits for input without accepted `done` also
triggers an `idle` message. The default threshold is 15 minutes, configured
per repo with `orchestration.idle_after_seconds` (positive seconds, default
900); the same interval controls repeats. The body names the child, elapsed
idle duration, last accepted child message kind and first line (at most 240
characters, or `none`), plus the same bounded tmux tail as `blocked` when
available. Read that message and answer the child; this is not completion
and does not authorize restarting an agent that is still alive.

The broker matches its prompted PID and process start to the account
adapter's session, currently Claude. `waiting` is input required; `idle`
counts only with readable transcript evidence and no subagent history.
Claude transcripts have no reliable subagent-finished marker, so even old
subagent files conservatively suppress plain `idle`; a genuine `waiting`
state still reports input required.
`busy` and `shell` do not count. Adapter or probe failures and unsupported
agents are unknown, not idle. An unchanged pane is deliberately not used:
a silent tool call could still be working. For those agents the separate
message-silence watchdog remains available. The episode resets on activity
or accepted child messages; timestamps survive a broker restart. Accepted
`done`, successful broker stop/destroy and agent-exit `blocked` end it. An
accepted child-sent `blocked` suppresses idle until its next accepted message.
A refused `done` does not suppress monitoring. `wait` only filters these
messages; it neither detects idle nor configures the threshold.

The broker is non-interactive. Anything that would prompt the operator (the
autostart escalation gate for an untrusted source, an autofetch that needs a
hardware-key touch) is refused with that reason; the broker never answers a
prompt for you. Messages for a stopped recipient wait in the broker's on-disk
queue and are delivered when it runs again, also across broker restarts.

Inside each orchestrator and child, `/home/dev/.jailbee/orchestration/` holds
`outbox/` (container to host), `inbox/` (host to container) and, for an
orchestrator, `grant.json`, a read-only copy of its limits. The broker creates
both directories in every orchestrator and child it serves. New images include
`jailbee inbox` (`wait`, `ls`, `read`, `runners`, `children`, `send`), so agents
need not write JSON by hand. `jailbee inbox children [--json]` lists each child
from the broker's `children.json` (it must be fresh, at most 15 minutes old): one
line per child with its name, whether its container is running, its latest agent
session (`-` if none) and whether its `done` report was accepted; `--json` prints
the raw list. For an old image without the container CLI, run
`jb base build` on the host and recreate the container. After `wait`, run `ls` and handle every
unread message. `JAILBEE_INBOX_SETTLE` controls the quiet window after an arrival
(default 1 second, 0 disables), capped at 5 seconds and the wait timeout;
messages already present return immediately. Use `send --wait` for spawn,
update and destroy, and always for a child's done report: it consumes only
that manifest's status, returning 0 for done/queued/started, 1 for
refused/failed, 2 for usage errors and 3 for timeout (default 30m).
For `send --wait` and ordinary `wait`, `--timeout none` waits without a
deadline; `send --wait --timeout none` never exits 3.
For lower-noise orchestration, use the helper directly, not a wrapper that
reads and discards progress notes:

```bash
jailbee inbox wait --wake-on msg:done,question,blocked,idle,status:failed,status:refused --quiet-after 60m --timeout 30m
```

For a background orchestrator waiter, use `--timeout none` only together with
`--quiet-after` so silent children still wake it.

`--wake-on` accepts comma-separated message kinds (`note`, `question`, `done`,
`blocked`, host-generated `idle`) and status states (`queued`, `started`, `done`, `failed`, `refused`).
`done` is both a kind and a state, so a bare word matches either: a plain
`done` also wakes on the `delivered to <child>` status ack that every
`inbox send message` gets. Prefix a token to say which you mean: `msg:<kind>`
matches only a message (including the broker's `idle`/`blocked` reports),
`status:<state>` only a status reply; an unknown kind or state after a prefix is a
usage error (exit 2). The recommended filter above wakes on child reports and
on failed or refused manifests, but not on successful acks, which `send --wait`
already returns.
It returns the oldest matching unread path; **nothing is marked read**, even
when it does not match. Omit the filter for any unread message. The first
message from a child after each spawn/restart always wakes, regardless of
kind; read history prevents later notes from counting as first again.
After waking, list and handle all unread messages, including filtered notes.

`--quiet-after` returns exit 0 with `quiet-child CHILD` instead of a path when
a running child that has not reported done shows no activity for that duration.
Activity is the latest of a message from the child (read or unread), an agent
event, a commit on the child's branch and CPU use at or above 5% of one core,
taken from the `status` object below (the broker's own `idle`/`blocked` reports
from `child.short` count as messages from the child, for `LAST-MSG` too); with
no activity, time is measured from the child's start. A child whose `status` is missing (an older broker) is timed by
messages alone. Stop or restart resets monitoring, and an overdue child keeps
waking until activity or a lifecycle change. The broker publishes read-only
`children.json`: per child its name, `running` (the container state), `done` (an
accepted `done`; a done child may keep running until it is stopped or
destroyed), start time, latest agent session and `status`. After the broker
itself completes a stop, destroy, restart or spawn, it republishes the snapshot
before sending the reply. Quiet monitoring
watches running children that are not done. Requested quiet monitoring exits 2
if the snapshot is absent, malformed or older than 15 minutes; check the
broker. Without quiet monitoring, filtering still works if that snapshot is
missing, but the first-report override needs fresh child state.

`--digest DURATION` makes `wait` exit 0 printing `digest` on the first line and
the `jailbee inbox status` table after it, when at least DURATION has passed
since the last digest and a child is running and not done. At most one digest
per period is emitted for all children together; the last time is stored
atomically in `~/.jailbee/orchestration/digest-at`, and the first digest of a
fresh container is due DURATION after its first `wait --digest`. A real message
that is due at the same time wins and the digest follows at the next wait; a
due digest ignores `--wake-on`. A due real message and a `quiet-child`
wakeup both win over the digest. A missing, unreadable or future-dated stamp is
created (reset) to now, which is not due, even with no running child; with a
stale stamp the digest is due as soon as a child next runs, and with no running
child an existing stamp is left unchanged. Like `--quiet-after`, it needs a
fresh `children.json` (exit 2 otherwise).

### Child status

`jailbee inbox status [--child C] [--json]` shows how each child is doing
without asking it. It reads `children.json` (same validation and 15-minute
staleness rule as `children`) and prints one line per child:
`NAME STATE AGENT ACTIVE CPU MEM HEAD DIRTY LONGEST LAST-MSG`. `STATE` is
`running`, `stopped` or `done` (done wins); `AGENT` is `alive:<state>`, `starting`
(the agent has not been launched yet: create or restart still running), `dead`
(a recorded agent is gone) or `?`; `ACTIVE` is the age of `activity_at`; `LAST-MSG` shows the age of the
last message from the child (`↑`, from its own inbox files) and the last
delivery to it (`↓`). Unknown values print `?`. `--json` prints the items as
published plus `last_from_child_at`. An unavailable or invalid snapshot, or an
unknown `--child`, exits 1; a usage error exits 2. A stale (but valid) snapshot
only warns on stderr and still prints with exit 0, unlike `wait --quiet-after`
and `wait --digest`, which exit 2.

Each child item in `children.json` carries `status` (an object for a running
child; for a stopped one `{"head", "last_to_child_at"}`, either value possibly
`null` (a stopped child that was never observed or messaged has `status: null`;
`version` stays 1 and every field is additive and optional, so older and newer
readers and brokers interoperate):

| Field | Meaning |
|---|---|
| `observed_at` | Epoch seconds the object was last refreshed. |
| `agent` | `{"alive", "state", "since", "last_event_at", "last_tool"}`: the prompted agent's liveness and session state (currently Claude). `alive` is `null` with `state: "starting"` while no agent has been recorded yet. |
| `cpu_percent` | Container CPU over the last sample, in % of one core. |
| `memory_bytes` | Container memory in bytes, net of page cache. |
| `longest_command` | `{"command", "seconds"}`: the process name (not the command line) of the longest-running non-shell process a shell started below the agent, excluding the agent and any `jailbee inbox wait` or `inbox send --wait` waiter (recognised by its command line, e.g. `python -m jailbee.container_main inbox wait`); `null` if none or the agent is unknown. |
| `head` | `{"sha", "time", "subject"}` of the newest commit on the child's branch (committer time), or `null` without commits. |
| `dirty` | Uncommitted changes in the work tree, or `null` if unknown. |
| `last_busy_at` | Last time `cpu_percent` was at least 5. Kept across broker restarts. |
| `last_to_child_at` | Last time the broker accepted a message, update or restart for the child. |
| `activity_at` | Latest of `agent.last_event_at`, `head.time`, `last_busy_at` and the start time; never decreases for one child instance. Child-supplied times later than now + 5 s are ignored by the broker, and `wait` ignores an `activity_at` later than now + 5 s. |

Host-side fields, including the agent session fields, refresh at most every 30
seconds per child; the liveness probe (`agent_watch.probe`) and the git probe
run at most every 60 seconds; between refreshes the last values are republished
with their `observed_at`. A session that cannot be read keeps the last known
`last_event_at`. A stopped child publishes only `head` and `last_to_child_at`
(each `null` when unknown; the whole `status` is `null` only for a child never
observed running or messaged). Commit subjects and process names come from the
child and are untrusted: they are cut to 120 and 160 characters and control
characters are replaced, in the broker and again when printed.

`cpu_percent` and `last_busy_at` are container-wide, so autostarted background
services (a JVM, a database, a file watcher, dockerd) can keep
`--quiet-after` from ever firing for that child.

One ordinary waiter per container is enforced with a process lock (a second
exits 2). It may overlap `send --wait`: the sender reserves its status ref
before publishing the manifest, and ordinary wait skips those statuses while
the sender lives. Locks release on exit/crash/timeout; leftover lock files do
not reserve anything. `ls` and explicit `read` remain administrative operations:
do not read a pending sender's reply in another session. Ordinary wait never
consumes a reply. After a send timeout, a late reply is eligible again.

For multi-session work, spawn with `--on-done keep`; `update --child C`
brings the parent's latest committed branch into the existing child. Add
`--ref REF` to send a full ref or 40-hex commit from the parent's repo, or
`--base BRANCH` to send a host-resolved base without switching the parent's
branch. The two options are mutually exclusive. Plain update transports a ref
for the child to merge manually; use `--merge` when the child is idle or between
tasks to check for conflicts and apply it automatically. A clean fast-forward
advances HEAD, divergent histories create a merge commit, and an already-contained
source leaves HEAD unchanged. The broker refuses a stopped or mount-mode child,
detached HEAD, dirty worktree, merge in progress, or conflicts, including gitlink
conflicts conservatively even when Git might resolve them. There is no idle gate,
so coordinate with the child. An update message with `merged_head` means the
branch was already updated: the child must re-read files, not merge the
`imported_ref` again. Prefer a fresh fork for rework so it starts from the latest
merged state.

## Lifecycle

- **Spawn.** `kind: fork` (from the orchestrator's committed work) or `kind:
  new` (from a base), plus the prompt and rights. Only **running** children
  count against `max_running_children`; a child being created counts as
  running. During creation, a child counts from the `started` reply until
  it is running and its create has finished, or its create has failed.
  Beyond the limit the spawn is queued in order, reported as
  `queued at position N`, and started when a slot frees. A queued fork pins
  its source commit when it is queued.
- **Done.** A child reports `done` when finished. If its work tree is dirty or
  its HEAD is detached, the broker refuses with `uncommitted changes` (or
  `detached HEAD`) and does not relay it. Otherwise the broker fetches the
  branch to the host, imports it into the orchestrator, then delivers `done`
  with `imported_ref` and `imported_sha`. Merge that ref yourself. The default
  `on_done: destroy` removes the child after delivery and its status reply;
  `on_done: keep` leaves it available for another session. A grant without
  `destroy` defaults to `keep` and refuses an explicit `destroy` policy.
  Import failures arrive as `import_error`, keep the child, and can be retried
  with `import` after fixing the cause. When the import succeeded but the
  child is kept anyway (the grant lost `destroy`, or the destroy marker could
  not be set), the report carries `on_done: keep` and `on_done_reason`
  instead.
- **Import.** `import` brings a running child's branch into the orchestrator
  at `refs/jailbee/from/<child>/<branch>`, without merging. The orchestrator
  merges or cherry-picks from that ref itself; conflicts are its to solve.
  A stopped child is refused ("restart it first"). The orchestrator needs a
  branch and must not be a mount-mode container; its tree may be dirty or
  have an in-progress merge because the transport only writes refs and objects.
- **Update.** `update` (allowed by the `import` operation) brings the
  orchestrator's current branch into a running child at
  `refs/jailbee/from/<orchestrator>/<branch>`. The child receives an `update`
  message with `imported_ref`/`imported_sha`. Without `--merge`, the child
  merges that ref itself. With `--merge`, a message with `merged_head` means
  the branch was already updated: the child re-reads files and must not merge
  `imported_ref` again. Optional `ref` selects a full `refs/` name or 40-hex commit in the parent's
  repository, with spawn's validation; optional `base` selects a host branch
  using spawn `--new --base` resolution (including `new.autofetch`). They
  cannot be combined. Explicit sources land at
  `refs/jailbee/update/<orchestrator>/<manifest-id>`, never overwriting the
  parent's current-branch ref or a child's imported ref. Without `--merge`,
  no worktree, index, local branch or base anchor is moved by the broker. Detached parent HEAD
  is refused only when neither selector is supplied.
- **Stop** a child; **restart** a stopped child with a new prompt (it counts
  against the limit and is queued if the limit is full). A running child is
  refused ("stop it first").
- **Destroy** (needs `destroy` in the grant) works on a running or stopped
  child; a done `keep` child needs only `destroy`, not `stop` first. It is
  refused for uncommitted changes or commits never imported: HEAD must equal
  the last import. Unreadable status also keeps the child. A
  child's starting commit counts as imported, so one that never committed
  is destroyed without `force`. A
  stopped child's HEAD is the one recorded when the broker stopped it; a
  child stopped any other way is refused until restarted.
  `force: true` overrides the guard.
- **Messages** go both ways with replies (`note`, `question`, `blocked`,
  `done`). A child can only message its orchestrator, and an orchestrator only
  its own children.

If the orchestrator is stopped, `done` waits as an ordered finish item:
import, delivery and cleanup run when it returns. If it is destroyed, the
child is kept and told the report cannot be imported; the fetched branch
remains on the host. A child that commits or edits after `done` is also kept
instead of destroyed, with a notice to the orchestrator.

## Typical workflows

In both, one child takes one whole unit of work: a ticket or a plan. Inside
its own container the child is free to use its own subagents for the steps
of that unit; the orchestrator never splits a plan's tasks across children.

**A bug backlog.** The user asks the orchestrator to work through clear bug
tickets on GitHub. The orchestrator reads them with the read-only `gh`,
spawns one child per ticket (`fix/issue-<n>`) whose prompt carries the
ticket text and the acceptance tests, and lets each child investigate, fix
and test. Each `done` arrives after auto-import; the orchestrator merges its
`imported_ref`, while default cleanup frees the slot for the next ticket. Forking
each new child from the orchestrator's latest merged HEAD keeps later
fixes from conflicting with earlier ones.

**A series of plans.** The user hands over a higher-level plan that orders
several implementation plans (for example written with the superpowers
`writing-plans` skill), some independent and some depending on others. The
orchestrator spawns a child per plan whose dependencies are met, one per
free slot. When a child is done it merges the already-imported result, resolves
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
