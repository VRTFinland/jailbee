---
name: jailbee-container
description: Use when working inside a JailBee container, identifying the container or repo, starting, restarting, checking or reading logs of services, resolving blocked URLs or network access, failed git push or gh writes, ports, Docker, display or host operator requests. Trigger on "inside JailBee", "which container", "service status", "blocked URL", "kontin sisällä", "mikä kontti", "käynnistä palvelu", "palvelun lokit", "verkko estetty", "portti".
---

# Working inside a JailBee container

The container is host-managed. Work and commit locally; ask the host operator for lifecycle, network and other host-only actions. If `JAILBEE_ROLE=child`, your first prompt identifies you as a child, or you have been adopted as a child, use **jailbee-orchestration-child**: children report to their orchestrator, must not push, open PRs or manage containers, and must not merge into other branches or perform host integration. Follow the child skill for own-branch updates from the orchestrator.

## Identify the container and repository

Read `/etc/jailbee/container.json` before naming a target. `name` is the full container name, `short_name` its short name, `container_prefix` identifies the repo, and `repo_dir` is its in-container repository path. `mode` distinguishes a branch clone from a mounted host tree; in mount mode `branch` is null and the git bridge does not apply. `base_branch`, `pr` and `fork_of` record creation details.

The file is a creation-time snapshot, not live state: a later rename or retarget is not reflected. It may be absent on older containers or if creation could not write it. In that case, `hostname` gives the container name and `$JAILBEE_BRANCH`, when set for a clone, records the creation-time branch rather than live Git state. Run `git branch --show-current` in the repository for the current branch; it prints nothing for a detached HEAD. Confirm other details with the operator. For host commands outside the repo, the root option `jailbee --repo <container_prefix> ...` selects the repo; do not confuse it with an egress subcommand's `--repo` scope flag.

## Services

Autostart may run configured services at boot, but profiles or other startup choices may skip them. Inspect actual state rather than assuming they are running. From the repository directory:

```bash
jailbee service ls
jailbee service start api
jailbee service restart api
jailbee service logs api -n 100
jailbee service logs api --follow
jailbee service refresh api
jailbee service plan api --op refresh
jailbee service stop api
```

`start` also starts required dependencies. `refresh` runs configured preparation steps and restarts the selected service; if preparation needs elevated network or mounts, it refuses and tells you how to ask the orchestrator or host. Service output comes from `svc-<name>` windows in the autostart tmux session. For another repository use `jailbee service --repo <path> ls`; `ls` and `plan` support `--json`.

`service ls` has a DESCRIPTION column (`description` in `--json`) with the
repo's own note on what each service does and when it is needed; read it to
decide which service to start.

### Status and log queries

`service ls` may run the configured read-only `status` probe; its output or a
failure/timeout is reported without changing lifecycle state. `service logs`
reads retained tmux scrollback, not durable logs. Use repeatable `--grep` regexes
or named `--filter` entries; multiple patterns form a union. `-n` selects the
last N matching lines (default 30), before context expansion; `-B`, `-A`, and
`-C` add context, merging overlapping ranges and separating gaps with `--`.

For a one-shot line, start the wait command in the background before triggering
the action:

```bash
jailbee service logs api --wait 'ready' -A 40 --timeout 120 &
wait_pid=$!
# Trigger the action that should produce the line here.
wait "$wait_pid"
```

The wait boundary is successful attachment to the tmux control output stream,
not command invocation; short setup output before attachment may be missed, and
background launch alone does not guarantee attachment. Context before a match
may also omit a short setup gap. If the action already ran, query retained
history instead: `jailbee service logs api --grep 'ready' -n 5 -A 40`. For
errors, use `jailbee service logs api --filter errors -n 20 -A 30` when the repo
defines that filter. Wait excludes `--grep`, `--filter`, and `--follow`; timeout
defaults to 300 seconds (`0` is unlimited). Exit codes: 0 match, 1 timeout, 2
invalid input, 3 unusable pane. See service config for named filters and the
session-wide scrollback limit.

The in-container CLI offers `service` (including `ls`, `start`, `stop`, `restart`, `refresh`, `plan`, `logs`), `config validate [--config <path>]`, `host status [--json]`, `agent models [--json]`, `inbox`, `version` and `--version`. Inbox notification commands include `jailbee inbox follow` (a non-acknowledging stream) and Claude hook handlers `jailbee inbox hook session-start` / `jailbee inbox hook session-end` / `jailbee inbox hook stop`. Start/end hooks maintain the live Claude session registry so autostart can resume sessions after unclean stops as well as normal restarts. In interactive Claude Code sessions the managed JailBee plugin starts the follower automatically; headless sessions and other agents use `jailbee inbox wait` as their fallback. The plugin marketplace ships in `jailbee/claude_marketplace`; its read-only managed-settings drop-in is mounted at `/etc/claude-code/managed-settings.d`, independently of the host-wide agent-instructions mount. Other JailBee management commands run on the host, not here.

`jailbee --version` and `jailbee version` include the staged host code's source
commit when known, e.g. `1.8.9 (abc1234)`; `-dirty` marks uncommitted source
changes when staged or built. This is not the container repository's HEAD. With
no build info they print just the package version; no rebuild is needed solely
for this metadata.

### Choosing models

Before choosing a model for a subagent or child runner, run
`jailbee agent models` (`--json` for structured output). Follow `model_policy`
and consult each model's `label`, `use_for`, `avoid_for` and `notes`. This is the
user's effective repo guidance, read live from host config; changes need no
`apply` or restart. An empty map gives a hint to configure `agents.models` in
`~/.config/jailbee/global.yaml`; JailBee supplies no model recommendations.
Runner availability and permissions still come from `jailbee inbox runners`.

### Health and waiting

Before long test runs, builds or spawning parallel work, run
`jailbee host status` (`--json` for machine-readable output) to check current
host load/CPU count, available memory, free space in this container's Incus
storage pool and the host-wide running JailBee container count across all
Incus projects and repositories. Every container may query it; no orchestration
grant is needed. Readings are computed on demand, not from the last broker
round; inventory and pool readings share a two-second cache and dedicated
bounded workers, so repeated checks do not starve inbox or outbox operations.
The usual broker connection retry applies; exit 4 means the broker is
unreachable or did not answer in time (the message distinguishes them).
Treat unavailable readings as unknown, not as free capacity.

Before long tests or work that depends on services, check `jailbee service ls`
or run `jailbee service wait api --until unhealthy` in the background to watch
for failure. `unhealthy` also matches stopped, failed, or exited services.
For connection refused, a blank UI, or HTTP 500, check `service ls` and
`service logs` first; fix the environment and restart the service rather than
changing application code to work around a dead service. JailBee does not
restart unhealthy services automatically.

### Runtime arguments

For one explicitly named service, start/restart accept `--env KEY=VALUE`
(repeatable) and `-- ARGS`. Arguments require `services.<name>.args: true`
and `run` must forward `"$@"`. Overrides are one-off unless `--persist` is
given; a plain restart uses persisted defaults. `--reset-args` clears stored
arguments and environment first. Status hides environment values.

## Blocked network access

The default network mode is strict, a default-deny egress allowlist; configuration may differ. Do not bypass a blocked destination through a proxy or tunnel. A child must ask its orchestrator for access, not request it directly from the host. Otherwise, give the operator the destination and container identity and ask them to run on the host:

```bash
jailbee net egress add api.vendor.example <container>
# Or, if broader temporary access is appropriate:
jailbee net loose <container> --for 30m
```

The destination precedes the container in `egress add`. If needed, prefix either command with `jailbee --repo <container_prefix>` instead of `jailbee`.

## Commits and GitHub

In clone mode, make local commits and ask the host operator to extract them with `jailbee git pull <container>` or publish with `jailbee pr push <container>`. In mount mode the host and container share the tree; no bridge extraction is needed. Treat `gh` as read-only policy, not a guarantee about token permissions. Stage PR review writes with **jailbee-pr-review** and issue writes with **jailbee-issue-management** for host publication. Child agents follow the stricter **jailbee-orchestration-child** rules instead of requesting publication directly.

## Other resources

- Ports: ask the operator for host-side `jailbee port` forwarding when a service must be reached across the host/container boundary.
- Docker: use it only if provisioned and available; ask the operator if the daemon or access is missing.
- Memory: containers have a configured memory limit; ask the operator to inspect or adjust it if a build runs out of memory.
- GUI/display: host-side `jailbee display` and GUI commands manage display access; ask the operator rather than assuming a display is attached.

For host usage see **jailbee-usage**; for repository configuration see **jailbee-repo-setup**; for JailBee defects see **jailbee-bug-report**; for managing granted child containers see **jailbee-orchestration**.
