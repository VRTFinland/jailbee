<p align="center">
  <img src="https://raw.githubusercontent.com/VRTFinland/jailbee/main/docs/images/jailbee-logo-dark.jpg"
       width="220" alt="JailBee">
</p>

<p align="center">
  <a href="https://github.com/VRTFinland/jailbee/actions/workflows/ci.yml"><img
    src="https://github.com/VRTFinland/jailbee/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/VRTFinland/jailbee/blob/main/LICENSE"><img
    src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="License: GPL v3"></a>
</p>

**JailBee gives each git branch its own Linux development environment.** Run
several full stacks on one host — services, databases, Docker, browsers and
IDEs — without branches competing for ports or Docker names. The
environments are [Incus](https://linuxcontainers.org/incus/) system
containers: each has its own userland and network stack and shares the host
kernel, which makes them far lighter than VMs.

That makes parallel coding-agent work practical: give each attempt its own
working tree and runtime, see from the terminal dashboard which Claude Code
session needs you, and bring commits back through a local git bridge. The
same workflow serves development and review without an agent.

The CLI is `jailbee`, or **`jb`** for short. It needs a **Linux host with
Incus**. A repo's `.jailbee/config.yaml` describes its environment, so JailBee
is project-agnostic — built at GISGRO, usable with any stack.

## What people run in it

<p align="center">
  <img src="https://raw.githubusercontent.com/VRTFinland/jailbee/main/website/assets/img/dashboard-parallel.png"
       width="760"
       alt="The terminal dashboard: an Android app's eight parallel branch containers, each with its own agent, above another repository's backlog containers, with the AI column showing which agents are working, idle or waiting and the merge column flagging two conflicts">
</p>

<p align="center"><em>One dashboard, one afternoon: an Android app across eight branches with an
emulator each, and below it a backlog of small ideas with a container apiece.</em></p>

- **A mobile app, one emulator per branch.** An Android/iOS codebase split
  into eight pieces of work, each container with an Android emulator of its
  own on the host's `/dev/kvm`, and a dashboard that shows which of the eight
  agents is waiting for you.
  [Host devices](https://jailbee.gisgro.io/docs/config/#host_devices) ·
  [adb](https://jailbee.gisgro.io/docs/project-config/#talking-to-android-devices-over-adb)
- **A backlog of small ideas, one container each.** A container per idea, an
  agent in each, and the ones that work merged back to main from one container
  into another or straight to the host.
  [Git bridge](https://jailbee.gisgro.io/docs/git-bridge/)
- **A Compose stack per branch.** Every branch runs its own Docker daemon and
  Compose project, so two branches never fight over a port or share a
  database, and a browser inside the container reaches that branch on its own
  localhost.
  [Stacks](https://jailbee.gisgro.io/docs/config/#golden)
- **A pull request reviewed beside your own work.** `jb new --pr 482` boots
  the PR's stack in a container of its own and leaves your branch alone; push
  fixes back to its head or open a stacked PR on top.
  [Reviewing a pull request](https://jailbee.gisgro.io/docs/git-bridge/#reviewing-a-pull-request)
- **An agent left to work without prompts.** Strict network mode, PRs and
  issues staged in an outbox you publish from the host, a read-only token, and
  a snapshot to restore.
  [Running an agent without prompts](https://jailbee.gisgro.io/docs/security/#running-an-agent-without-prompts)
- **A supervised admin task with time-boxed credentials.** Lend the container
  a helpdesk's admin credentials as an optional mount for an hour, open the
  network for the same hour, and watch from the dashboard; both revert on
  their own.
  [Lending a credential](https://jailbee.gisgro.io/docs/security/#lending-a-container-a-credential) ·
  [Timed loose mode](https://jailbee.gisgro.io/docs/config/#loose_auto_revert)

## See it work

<p align="center">
  <a href="https://youtu.be/XIu-REiLms4"><img
    src="https://raw.githubusercontent.com/VRTFinland/jailbee/main/website/assets/videos/XIu-REiLms4-play.jpg"
    width="760"
    alt="Play the JailBee 1.7 tour on YouTube: the terminal dashboard listing branch containers and their coding-agent activity."></a>
</p>

<p align="center">
  <a href="https://youtu.be/XIu-REiLms4"><strong>▶ JailBee 1.7: the 3-minute tour</strong></a>
</p>

See what a container per branch gives an agent and how the terminal dashboard
helps you manage several at once. For the complete create → code → merge →
destroy workflow, [watch the end-to-end demo](https://jailbee.gisgro.io/#demos).

## What you get

- **A whole stack per branch.** Each container runs its own services and,
  when enabled, its own Docker daemon; a browser inside it reaches that
  branch's app on `localhost` with no port forwarding. Provisioning and
  autostart are configured once and baked into a reusable golden image —
  general development tools by default, language stacks and Docker opt-in.
  On a copy-on-write backend such as btrfs, new environments and snapshots
  are cheap.
  [Stacks](https://jailbee.gisgro.io/docs/config/#golden) ·
  [Storage](https://jailbee.gisgro.io/docs/storage/)

- **A local git workflow.** `jb git push` and `jb git pull` move commits
  between host and container without a round trip through GitHub;
  `jb git merge` merges one container's branch into another, and `jb fork`
  starts a new environment from a container's committed work. Submodules
  initialized on the host travel with the repo, and `jb pr` can open
  submodule PRs before the superproject's.
  [Git bridge](https://jailbee.gisgro.io/docs/git-bridge/)

- **A terminal dashboard for parallel work.** Bare `jb` opens the TUI. It
  spans every registered repo, shows git and resource status, and tracks
  Claude Code's state, recent tool calls and messages. Attach a session,
  inspect a diff, open a PR, or start and stop containers from the same
  view. Claude Code has a dedicated integration; Codex, Gemini, Aider,
  OpenCode and Grok presets are best-effort starting points. For other
  providers' models, `claude-jb` routes Claude Code through a LiteLLM proxy
  container.
  [Agents](https://jailbee.gisgro.io/docs/agents/) ·
  [Claude Code through LiteLLM](https://jailbee.gisgro.io/docs/litellm/)

- **Explicit network policy and a GitHub outbox.** Strict mode is the
  default: a destination IP allowlist, optionally per TCP port, filters
  container traffic, and an HTTP(S) egress proxy adds hostname rules with
  wildcards. Loose mode lifts the allowlist. Agents stage proposed PR and
  issue changes in an outbox for you to inspect and publish with the host's
  `gh`; pair it with a read-only container token.
  [Security and limitations](https://jailbee.gisgro.io/docs/security/) ·
  [Outbox commands](https://jailbee.gisgro.io/docs/commands/)

- **Desktop tools inside the environment, local or remote.** Launch a
  JetBrains IDE, Chrome, Firefox or a configured app inside a container with
  its window on your Wayland desktop; declare host devices and forwarded
  services (adb, say) when a project needs them. Optional SSH access exposes
  the TUI and policy-controlled commands, and remote GUI apps run over
  `waypipe ssh` or a shared RDP display.
  [GUI apps](https://jailbee.gisgro.io/docs/config/#apps) ·
  [Remote GUI](https://jailbee.gisgro.io/docs/remote-gui/)

- **Reusable state, personal settings outside git.** Shared caches and
  agent settings outlive individual containers, and credential groups share
  one Claude login across repos — in JailBee-managed directories, never your
  host `~/.claude`. Environment settings live in the repo, personal defaults
  in `~/.config/jailbee/global.yaml`, and host-local repo overrides in
  `~/.config/jailbee/repos/<prefix>.yaml`.
  [Configuration layers](https://jailbee.gisgro.io/docs/config/#configuration-layers) ·
  [Credential groups](https://jailbee.gisgro.io/docs/agents/#shared-credential-groups-credentials)

JailBee earns its place when **the runtime is the hard part**: several
services, Docker stacks, desktop tools or agents in parallel. If worktrees
alone solve your problem, you may not need it — see
[Who JailBee is for](https://jailbee.gisgro.io/docs/comparison/) for
trade-offs and alternatives.

## Getting started

### Prepare the host

Install the CLI with [`uv`](https://docs.astral.sh/uv/) or
[`pipx`](https://pipx.pypa.io/) (Python 3.12+):

```bash
uv tool install jailbee      # or: pipx install jailbee
```

Incus, a storage pool and UID/GID delegation are host setup, separate from
the Python package: follow
**[Installation](https://jailbee.gisgro.io/docs/installation/)**, which also
covers the firewall and kernel-limit adjustments some hosts need. Then run
`jb setup` for shell completion, the network-refresh user timer and,
optionally, JailBee skills for agents on the host.

### Try it without a repo config

From any git repo with a fetchable upstream and no `.jailbee/config.yaml`:

```bash
jb new work                 # create a scratch environment
jb                          # open the terminal dashboard
```

Scratch mode (on by default) synthesizes a config, creates the repo's Incus
profiles and, on an interactive terminal, offers to build a scratch image
that later scratch environments reuse. The image carries the configured
tools; your app's own stack is set up in the next step.

For a local-only repo, `jb new work <local-branch>` starts from an existing
committed branch and skips the upstream fetch. For a directory that is not a
git repo, `jb new --mount work` shares the host directory read-write into
the container instead of cloning it.
[Scratch configuration](https://jailbee.gisgro.io/docs/config/#scratch)

### Configure a repeatable project environment

From the repo you want to manage:

```bash
jb config init              # generate .jailbee/config.yaml
# Edit the config: enable the stacks and integrations your project needs.
jb doctor                   # check host prerequisites and config
jb init                     # create the repo's profiles, ACLs and shared dirs
jb registry up              # only if the project uses the registry mirror
jb base build               # build its reusable image; allow several minutes
jb new feat/my-branch       # create the branch environment
jb                          # manage it in the terminal dashboard
```

Projects that enable Docker, ECR or extra registries need the mirror running
before their first strict-mode `jb new`; others skip `jb registry up`. Add
provisioning and autostart steps for your dependencies, database and dev
server —
[Getting started](https://jailbee.gisgro.io/docs/getting-started/) walks
through a complete config and a typical day. If you tried scratch mode in
the same repo first, see
[Scratch configuration](https://jailbee.gisgro.io/docs/config/#scratch) for
keeping its containers under the new config.

Besides the TUI, `jb console` offers an interactive command prompt, and
`uv tool install 'jailbee[gui]'` adds an optional Qt dashboard (`jb gui`).

## Know the boundary

JailBee uses unprivileged system containers that share the host kernel. It
is a workspace boundary, not a hostile multi-tenant one. Before leaving an
agent running without permission prompts, review what its environment can
reach:

- **Working trees and mounts.** Clone mode gives the container its own
  working tree; `--mount` shares the host's. A clone still borrows Git
  objects from the read-only host-source mount. Shared caches, agent
  settings, writable host mounts and — by default — the repo's `.local/`
  directory are visible across environments, and container snapshots do not
  roll back bind-mounted data.
- **Credentials and host services.** Readable token files can be copied.
  GPG integration mounts the host's `~/.gnupg` read-only, secret keys
  included, and a forwarded agent socket can sign or authenticate even
  through a read-only mount. Devices, desktop sockets and port forwards
  extend the container's reach into the host; attach only what you need.
- **Network policy.** The NIC ACL filters by IP, not hostname or HTTP path,
  and different hosts can share an allowed IP. Hostname wildcards apply only
  through the HTTP(S) proxy. Host sockets and declared port forwards bypass
  the ACL.
- **GitHub publication.** The outbox is a review step, not an enforcement
  point: a write-capable token inside a container can publish directly. Use
  a read-only token.

[Security and limitations](https://jailbee.gisgro.io/docs/security/),
especially
[Running an agent without prompts](https://jailbee.gisgro.io/docs/security/#running-an-agent-without-prompts),
covers the controls and their limits.

## Updates and documentation

After updating the CLI package, re-run `jb setup`. `jb upgrade --dry-run`
lists the registered repos whose image or profiles the release's upgrade
notes say to rebuild; `jb upgrade` runs those steps without restarting
development containers or dockerd, and names anything that still needs a
restart.

The full, searchable documentation is at
**[jailbee.gisgro.io/docs](https://jailbee.gisgro.io/docs/)**.

| Guide | What's inside |
|---|---|
| [Installation](https://jailbee.gisgro.io/docs/installation/) | Host setup and CLI installation |
| [Storage](https://jailbee.gisgro.io/docs/storage/) | Copy-on-write pools, btrfs and the cost of the `dir` backend |
| [Getting started](https://jailbee.gisgro.io/docs/getting-started/) | Configure a repo and walk through a typical day |
| [Commands](https://jailbee.gisgro.io/docs/commands/) | CLI commands and flags |
| [Configuration](https://jailbee.gisgro.io/docs/config/) | Repo config, personal defaults and local overrides |
| [FAQ](https://jailbee.gisgro.io/docs/faq/) | Common questions and troubleshooting pointers |
| [News](https://jailbee.gisgro.io/news/) | Release announcements and the [RSS feed](https://jailbee.gisgro.io/news/feed.xml) |

For contributors:
[Contributing](https://github.com/VRTFinland/jailbee/blob/main/CONTRIBUTING.md),
[Manual testing](https://github.com/VRTFinland/jailbee/blob/main/docs/manual-testing.md),
and [Releasing](https://github.com/VRTFinland/jailbee/blob/main/docs/releasing.md).

## License

`jailbee` is free software, released under the GNU General Public License v3.0
or later (GPL-3.0-or-later). See [`LICENSE`](https://github.com/VRTFinland/jailbee/blob/main/LICENSE) for the full text.

Copyright © 2026 GISGRO Oy.
