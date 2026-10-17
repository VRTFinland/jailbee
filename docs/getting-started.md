# Getting started

This assumes the host prerequisites are in place, `jailbee` is installed,
and `jailbee setup` has been run — see [Installation](installation.md).
Setup installs shell completions, the egress-refresh timer and, if you opt
in, skills for agents on your host. Run the commands below on the host
unless a step explicitly says to work inside the container.

## Concepts

A quick mental model before the commands:

- **A separate environment for each branch.** In clone mode,
  `jailbee new <branch>` creates a system container with its own services and
  network namespace. For `feat/my-feature`, the Git branch stays
  `feat/my-feature`, the short container name is `feat-my-feature`, and the
  full Incus name is `<container_prefix>-feat-my-feature`. Use the short name
  with `jailbee shell`, `jailbee ide` and `jailbee destroy`. `--name` overrides
  the container name without changing the branch. Docker, databases and other
  services are available when included in your image and project setup.
- **A reusable golden image.** `jailbee base build` provisions the OS and
  selected tools once; new containers reuse that image. Copy-on-write pools
  such as btrfs share its data blocks, while a `dir` pool copies them. See
  [Storage](storage.md). Build time depends on the selected stacks and
  downloads; rebuild when the image's tools or provisioning need updating.
- **Clone mode vs mount mode.** By default the container gets its **own**
  `git clone` of the repo (clone mode), isolated from the host tree. With
  `--mount` it shares the host working tree directly instead.
- **Base branch + the git bridge.** A clone-mode container remembers the
  **base branch** it forked from and acts as a **git remote**: commits move
  between host and container over a local bridge (`jailbee git checkout` /
  `pull` / `push`) rather than through GitHub.
- **Network modes.** Each container runs `strict` (kernel egress allowlist)
  or `loose` (wider egress). Switch with `jailbee net strict <name>` or
  `jailbee net loose <name>`. Loose switches auto-revert by default through
  the refresh timer; that behavior can be disabled, so do not assume every
  loose container has an expiry.
- **Persistent shared state.** Configured package caches and tool settings
  live outside the containers, usually in a repo-specific shared directory.
  Enabled integrations mount the relevant state into containers, so concurrent
  branches can reuse caches and settings. Credential groups can also share
  agent logins across repos; the generated global config uses the `default`
  group for Claude credentials.
- **Separate cache slots where tools lock.** Pooled caches such as Gradle,
  Maven and browser profiles get separate slots to reduce lock contention.
  Selected artifacts are hardlinked when seeding a slot, so these are not
  fully independent copies or a security boundary. Shared and pooled state
  survives container recreation, but writable shared mounts also let
  containers affect each other. Explicit writable host mounts can change
  host files too. See [`shared_caches`](config.md#shared_caches),
  [`pooled_caches`](config.md#pooled_caches) and the [Security model](security.md).

## Configure

For this walkthrough, run `jailbee` from the root of the repo you want to
manage and create its `.jailbee/config.yaml`:

```bash
cd ~/path/to/your/repo
jailbee config init        # creates a commented starter template
jailbee doctor             # sanity-check
```

The repo file can be minimal: defaults are stack-neutral, not an automatic
setup for every application. Review the generated template and add the
language toolchains your project needs with `golden.stacks`. Configure any
project-specific setup and startup commands before building the image.
See [Project setup](project-config.md) and the [Configuration reference](config.md).
`doctor` can report missing JailBee resources before the initialization and
build steps below; resolve host prerequisite failures first.

**Multi-repo:** Repo-specific containers, profiles and ACLs use the
`<container_prefix>-` prefix. Host-wide services and shared networks are
exceptions: they are shared rather than duplicated per repo. The prefix
defaults to your repo directory name (`repo_root.name`) and must match
`[a-z0-9][a-z0-9-]*`. If
your directory name contains underscores, dots, or capital letters, set
`container_prefix:` explicitly in `.jailbee/config.yaml`. Two repos on the
same host with distinct prefixes coexist without colliding. Use
`jailbee ls --all` to list containers from every jailbee-managed repo.

**Per-developer settings:** `.jailbee/config.yaml` is checked into the repo
and shared with your team, so anything personal lives in
`~/.config/jailbee/global.yaml` instead:

```bash
jailbee config init --global    # writes a fully-commented ~/.config/jailbee/global.yaml
```

That file carries your personal integrations (`gpg`, which mounts `~/.gnupg`), your IDE
preference, the Docker registry mirror port (default 3128, rpardini's
default), and the **Claude Code** opt-in — see
[Claude Code in the container](#claude-code-in-the-container) below. A third,
optional file holds what is personal *and* specific to one repo, such as its
GitHub token: `~/.config/jailbee/repos/<container_prefix>.yaml`, edited with
`jailbee config edit --local` (see
[Host-local overrides](config.md#host-local-overrides)). The files are
deep-merged at load time, global then repo then host-local: a later layer wins
on scalar collisions. Non-empty lists generally append, while `[]` clears
earlier values; some fields, such as app command lists, replace instead.
Host-controlled blocks including `github`, `credentials` and `litellm` are
rejected in the repo file and follow their own loading rules. Keep secrets
out of committed files: validation cannot prevent accidentally committing
a secret in another field. See [Host-local overrides](config.md#host-local-overrides)
for the merge rules and exceptions.

## Initialize and build

```bash
jailbee config validate         # verify your config is sane
jailbee init                    # creates Incus profiles, ACLs, jailbee-loose bridge, shared dirs
jailbee registry up             # when the registry mirror is enabled — see below
jailbee base build              # builds the reusable golden base (a stopped instance on btrfs, an image otherwise)
```

Run `jailbee registry up` when the registry mirror is enabled for your setup.
`docker_registry_mirror.enabled: auto` enables it when JailBee detects Docker
provisioning, extra registries or the ECR stack. Explicit `true` enables it
regardless; `false` disables it. A stack-neutral repo with no such settings
can skip this command.

When the mirror is enabled, strict-mode `jailbee new` requires its endpoint
to be available and its CA certificate to exist on the host; loose mode warns
and proceeds if they are missing. The default strict policy does not allow
direct Docker Hub pulls, so disabling the mirror does not by itself make
those pulls work. See [Configuration reference](config.md#host-level-docker_registry_mirror).
For an already initialized repo, use `jailbee apply` after configuration
changes instead of running `jailbee init` again.

## A typical day

A clone-mode branch from creation to teardown. The host stays the git hub:
you bring commits back over the local bridge and push from the host, so
publishing them does not require outbound git access from the container.
Package downloads, coding-agent APIs and other development services may
still need network access allowed by the container's egress policy.

```bash
# 1. Spin up a container for a new branch (off the default branch).
jailbee new feat/my-feature                  # → container "feat-my-feature" (the / is slugified to -)
jailbee ls                                   # watch it come up

# 2. Work inside it — a shell, or launch the IDE onto your desktop.
jailbee shell feat-my-feature                # lands in the in-container clone
#   … edit, run services, commit inside the container …
exit                                        # return to the host shell
jailbee ide feat-my-feature                  # optional: JetBrains IDE on the host display

# 3. Bring the container's commits back to the host (local bridge, no network).
jailbee git checkout feat-my-feature         # fast-forwards / creates the host branch

# 4. Push and open a PR from the host, as usual.
git push -u origin feat/my-feature
gh pr create                             # or your normal PR flow

# 5. Iterate: more commits in the container → checkout again → push.
jailbee git checkout feat-my-feature && git push

# 6. Snapshot before a risky change; restore only if you need to roll back.
jailbee snapshot create feat-my-feature before-migration
# jailbee snapshot restore feat-my-feature before-migration

# 7. Once merged and all needed work is saved, tear down with the safety checks.
jailbee destroy feat-my-feature
```

`jailbee git checkout` switches the host working tree to the container's
branch and creates or fast-forwards it; divergence is not silently overwritten.
Save unrelated host changes first. Snapshots cover the container's own
storage, not host-mounted shared state or a mount-mode working tree. Quiesce
services before snapshotting if you need a consistent database backup.

As an alternative to the manual push and `gh pr create`, run
`jailbee pr feat-my-feature` **on the host**. It fetches the container's
commits, pushes with the host's credentials and creates or updates the PR.
The container can stay in strict mode; optional AI generation only needs
access to the selected agent's allowed API endpoints. An in-container agent
can also prepare a PR description in the outbox for the host to publish.
See [Git bridge and branch workflows](git-bridge.md) for the outbox,
pushing host→container, stacked PRs and reviewing existing PRs.

## Claude Code in the container

JailBee can run coding agents in per-branch containers. Start with the
agent's normal permission checks. For unattended work, Claude's
`--dangerously-skip-permissions` is an explicit opt-in, not a requirement:
it removes the agent's approval prompts but does not isolate writable shared
state, credentials or forwarded sockets. Review mounts, leave sensitive
`optional_mounts` detached and use strict egress before relying on the
container boundary. Read
[Running an agent without prompts](security.md#running-an-agent-without-prompts)
for the remaining exposure.

[Claude Code](https://claude.com/claude-code) is one of six shipped agent
presets. The `agents:` mechanism also supports other terminal coding agents;
see [Generic agent support](agents.md) for the presets and custom adapters.
To enable Claude Code, set this in `~/.config/jailbee/global.yaml`:

```yaml
agents:
  claude:
    enabled: true
    plugins_enabled: true
    # autostart: true                                # launch claude on every container start
    # command: claude --dangerously-skip-permissions # what the autostart window runs
```

The template written by `jailbee config init --global` already contains
this `agents.claude` block with `enabled: true`, so if you took that path
you have it already. Run `jailbee apply` after editing, then `jailbee new`
a container.

A top-level `claude:` block is still accepted and means the same thing, but
defining both spellings at once is a `ConfigError` — so if you have an older
`claude:` block, rename it rather than adding `agents.claude` alongside it.

What turning it on gets you:

- **A shared Claude Code installation.** Containers reuse a version store
  shared across the repo rather than each installing an independent copy.
  Auto-updates can still download new versions.
- **Persistent settings and login.** `<shared_dir>/claude` is mounted as
  `~/.claude` for repo-shared settings and global config. `CLAUDE_CONFIG_DIR`
  points there, including the `.claude.json` file inside it. With the generated
  global configuration, credentials instead use the host-wide `default`
  credential group through a separate `~/.claude-creds` mount. Set
  `credentials.group: null` for a repo-local login. These stores survive
  container recreation, but sharing a group also shares login changes across
  its repos. Host `~/.claude` and `~/.claude.json` are not imported. Without an
  existing shared login, complete onboarding in the first container; with one,
  JailBee seeds a fresh config home's onboarding and repo trust by default
  (`seed_onboarding`).
- **Strict mode still works.** The Anthropic API and CLI-update hosts are
  added to the egress allowlist automatically; `plugins_enabled` adds the
  GitHub hosts the plugin marketplace and skills reach. You don't
  list them by hand. The npm registry is not among them: a plugin that
  installs from npm needs `registry.npmjs.org:443` in `egress_allow`.
- **The agents know JailBee.** JailBee's own `jailbee-usage`,
  `jailbee-repo-setup`, `jailbee-pr-review` and `jailbee-issue-management`
  skills are copied into the shared skills directory of every enabled agent
  that has one (claude, codex,
  gemini, opencode), so the in-container agents can drive `jailbee` commands
  for you.
- **AI-written PRs.** `jailbee pr <name>` asks the container's own agent —
  the one the repo autostarts, or whichever `pr.agent` names — for the title,
  body, and branch name (`--no-ai` opts out per call). Claude runs on Sonnet by
  default (`pr.model`), and the agent follows your project's own PR-writing
  rules if you state them in `pr.prompt`.

With `autostart: true`, JailBee launches Claude in a tmux window during
container startup. `jailbee tmux <name>` attaches to the session; later
attachments return to the last selected window, which may not be Claude's.
See [Claude configuration](config.md#claude) for the agent options and
[`pr`](config.md#pr) for how `jailbee pr` selects the agent that writes PRs.

## Next steps

- [Commands](commands.md) — full command + flag reference
- [Git bridge and branch workflows](git-bridge.md) — moving commits between host and containers, stacked PRs, mount vs clone
- [Setting up JailBee in your own project](project-config.md) — adapting `jailbee` to your own repo and stack
- [Configuration reference](config.md) — every `.jailbee/config.yaml` key
