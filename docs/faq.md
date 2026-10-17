# FAQ

Short answers to the questions the rest of the documentation answers at
length. Every entry links to the page that covers it properly — this page is
an index into the docs, not a replacement for them.

- [Is JailBee for me?](#is-jailbee-for-me)
- [Installation and first run](#installation-and-first-run)
- [Everyday container work](#everyday-container-work)
- [Moving code between host and container](#moving-code-between-host-and-container)
- [Network and ports](#network-and-ports)
- [Coding agents](#coding-agents)
- [Desktop apps and dashboards](#desktop-apps-and-dashboards)
- [Configuration](#configuration)
- [When something breaks](#when-something-breaks)

## Is JailBee for me?

### What does JailBee actually give me?

An unprivileged Incus **system container for each branch environment** — its
own init, IP and, when installed, Docker daemon, services and database — so
several branches can use the same internal ports and Compose project names
on one Linux host. Host-published listeners must not conflict. In clone mode,
the container is also a git remote, so commits move between host and
container locally.

→ [Who JailBee is for](comparison.md), [Architecture](architecture.md)

### What do I need on my machine?

A Linux host (Ubuntu 26.04+ recommended), **Incus 6.0.5-8 or newer**, and
**Python 3.12+** (`uv` can provision it; `pipx` needs a compatible interpreter).
Host-sourced browsers need a host browser installation; image-sourced ones
do not. JetBrains integration uses a host Toolbox installation. These GUI
integrations are optional.

→ [Prerequisites](installation.md#prerequisites)

### Can I use it from a Mac?

Yes, as a client. JailBee itself needs Linux — the Incus daemon is Linux-only,
and a native macOS client cannot make the Linux daemon bind-mount macOS paths —
so it runs either on a Linux host you SSH into (recommended) or in a Linux VM
on the Mac (Colima; **experimental, not yet verified on real Apple hardware**).
In both, the Mac can use the optional SSH service for the dashboard, commands
and files. GUI apps can use Windows App for the shared RDP display, or
waypipe with Cocoa-Way for native Mac windows. GPG signing with a key held
on the Mac is not bridged.

→ [Using JailBee from a Mac](macos.md), [Limitations](security.md#limitations)

### What does it cost me compared to `git worktree` + `docker compose -p`?

Host setup, a golden-image build per configured image (often ~10–15 min,
depending on the stack and network), disk per container, a Linux daemon, and
a shared kernel rather than a hypervisor boundary. If your stack is one
process and one database, worktrees plus Compose may be enough.

→ [What JailBee costs you](comparison.md#what-jailbee-costs-you)

## Installation and first run

### How do I install the CLI?

```bash
uv tool install jailbee            # or: pipx install jailbee
# For the optional Qt dashboard, use this instead:
uv tool install 'jailbee[gui]'      # or: pipx install 'jailbee[gui]'
```

This installs `jailbee` and the short alias `jb`. To add extras to an existing
pipx install, use `pipx install --force 'jailbee[gui]'`; include all extras
you want to retain. JailBee is an ordinary PyPI package — a virtualenv you
manage yourself works too. Ubuntu's externally managed system Python
refuses a bare `pip install` (PEP 668).

→ [Install JailBee](installation.md#3-install-jailbee)

### How do I know when a new release is out?

When a newer release is cached from PyPI, `jailbee ls` / `new` / `shell`
print a hint on stderr naming the version and the upgrade command for your
install (`uv tool upgrade jailbee`, `pipx upgrade jailbee`, …), and the
dashboard shows the same advice on a row of its own. Both stay until you
upgrade or dismiss: `jailbee dismiss update`, or the **[dismiss]** label on
the dashboard's row, silences that release; `update_check: false` in
`~/.config/jailbee/global.yaml` — or `JAILBEE_NO_UPDATE_CHECK=1` for one
command — turns the whole thing off.

Commands do not wait for the network request: a detached process fetches
`https://pypi.org/pypi/jailbee/json` when the daily cache expires. It sends no
credentials or repo data, but PyPI can see the connection's IP address and
the installed JailBee version in the User-Agent. Concurrent commands can
launch duplicate probes. Editable checkout installs receive no PyPI
upgrade hint.

After upgrading, `jailbee upgrade` runs the `base build` and `apply` the
release owes in every registered repo, restarting nothing.

→ [`update_check`](config.md#update_check) · [`jailbee upgrade`](commands.md)

### What is the minimum host setup?

Install and initialise Incus, ensure `/etc/subuid` and `/etc/subgid`
delegate both the shifted range and your host identity, install the CLI,
run `jailbee setup` for shell completion and the refresh timer, and check
`jailbee version`. Inspect existing Incus resources and delegations before
changing them. Firewall changes and higher keyring limits are conditional;
GUI integrations and remote access are optional.

→ [Quick install](installation.md#quick-install)

### Why does it need an extra `/etc/subuid` line?

JailBee maps the configured development UID/GID to the same host IDs via
`raw.idmap`, so enabled host-file mounts and agent sockets work without
changing ownership. `newuidmap` needs a delegation covering that mapping;
the container otherwise runs unprivileged. This is a real trust boundary:
processes using the mapped identity can access mounted files and sockets
with that identity's permissions. Share only what you intend to expose.

Missing delegation can prevent a container from starting.
`incus info --show-log <name>` shows the underlying error, and
`jailbee doctor`'s `uid delegation` check reads both files and names missing
entries.

→ [Why the UID mapping is needed](installation.md#why-the-uid-mapping-is-needed)

### What do I run the first time in a repo?

```bash
jailbee config init      # write .jailbee/config.yaml; review the generated settings
jailbee doctor           # sanity-check host + config
jailbee init             # profiles, ACLs, jailbee-loose bridge, shared dirs
# If this repo uses the Docker registry mirror:
jailbee registry up      # host-wide service, shared by repos
jailbee base build       # golden image, often ~10–15 min
jailbee new feat/x       # first container
```

Enable the stacks and integrations you need before `init` and `base build`.
Use `jailbee apply` instead of `init` if the profiles already exist.

→ [Initialize and build](getting-started.md#initialize-and-build),
[Setting up JailBee in your own project](project-config.md)

### When do I have to rebuild the golden image?

After changing anything the image bakes in — for example `golden.stacks`,
`golden.extra_apt_packages`, provisioning snippets, `golden.provision_script`,
or an image-sourced browser. Run `jailbee base build` again; existing
containers keep their current root filesystem and do not gain the rebuilt
image on restart. Recreate them to use it. Each build keeps one golden
version only; dated archives left by jailbee before 1.7.5 go away on the next
build (or with `jailbee base prune`).

→ [Stack runtimes and extra apt packages](project-config.md#4-optional--stack-runtimes-and-extra-apt-packages)

### What is the difference between `jailbee init` and `jailbee apply`?

`init` is first-time setup — it creates the Incus profiles, the ACL and the
shared directories, and refuses existing repo profiles. `apply` reconciles
the current config (profiles, ACL, `/etc/hosts`, dockerd proxy and other
integrations), creating missing resources where needed. It offers to
restart development containers for relevant profile changes, and separately
to restart dockerd for stale mirror settings; either can interrupt workloads.
`--yes` accepts these restart offers without asking.

LiteLLM reconciliation can also restart its proxy automatically when a live
reload is not possible. Use `apply --no-restart` to update config and egress
without those restarts; changes that need one remain pending.

→ [Commands](commands.md)

## Everyday container work

### How do I create a container for a branch?

`jailbee new <name> [<base>]`. In clone mode, `<name>` names the branch inside
the container. Incus names cannot contain `/`, so `feat/my-feature` becomes
the short name `feat-my-feature` and the full Incus name
`<container_prefix>-feat-my-feature`. `<base>` sets the base branch: it is
forked when the requested branch is new, and used as the comparison anchor
when that branch already exists. `jailbee new --current` uses the host's
checked-out branch.

→ [Choosing the starting point for `jailbee new`](git-bridge.md#choosing-the-starting-point-for-jailbee-new)

### `jailbee new` blocks for minutes — can I get my shell back?

Pass `--background` (`-b`). `jailbee ls` then shows a `JOB` column with the
live phase (`creating` → `cloning` → `autostart`). A failed job leaves the
container intact for inspection; clear the record with `jailbee job clear`
once you've fixed it, and read the worker log with `jailbee job log`. Set
`new.background: true` to make it the default.

`jailbee start` and `jailbee restart` take the same flag to background boot
and subsequent startup work. Long waits often come from `on_start` autostart;
set `boot.background: true` to make background execution the default for both. A failed *boot* record needs no acknowledging:
the next `jailbee start`/`jailbee restart` that completes clears it, since
that boot supersedes the one that failed. A failed `jailbee new` record does
not clear itself — the container's setup never finished, and a reboot doesn't
finish it.

→ [Background creation](git-bridge.md#background-creation)

### How do I get into a container?

`jailbee shell <name>` for an interactive shell (it lands in the in-container
clone), `jailbee tmux <name>` to attach to the autostart tmux session, and
`jailbee exec <name> -- <cmd>` to run one command as the dev user.

→ [Commands](commands.md)

### How do I see what containers exist?

`jailbee ls` for this repo, `jailbee ls --all` for every jailbee-managed repo,
`jailbee dashboard` for a live TUI across all repos, and `jailbee gui` for the
Qt version. The dashboards also *act*: shell, tmux, IDE, PR, diff, update from
base.

→ [Commands](commands.md)

### Clone mode or mount mode?

Default is **clone mode**: the container gets its own `git clone --shared` of
the repo, isolated from the host tree, and the git bridge moves commits.
`--mount` bind-mounts the host working tree instead — right when the host
directory isn't a git repo at all, or when you edit on the host and run in the
container. The bridge commands refuse on mount-mode containers.

→ [Mount mode vs clone mode](git-bridge.md#mount-mode-vs-clone-mode)

### How do I take a snapshot before something risky?

```bash
jailbee snapshot create <name> before-migration
# Later, only if you want to discard changes since the snapshot:
# jailbee snapshot restore <name> before-migration
```

Worth doing before a long unattended agent run. A snapshot covers the
container's root filesystem, not host-mounted directories, shared caches or
credentials. Quiesce databases if you need an application-consistent backup,
and stop the container before restoring.

→ [Commands](commands.md),
[Running an agent without prompts](security.md#running-an-agent-without-prompts)

### How do I tear a container down without losing work?

First bring valuable commits to the host and preserve any uncommitted files.
Then use `jailbee destroy <name>` (or `--all` for the repo). Before its
confirmation (default No), JailBee checks available git status and warns
about detected or uninspectable work.
A stopped clone cannot be fully inspected. `--force` bypasses the safety
prompts and assessment.

`jailbee prune` is different: it offers old stopped containers across repos
for deletion and does **not** run the same work-loss assessment. Review or
back up their contents first.

→ [Commands](commands.md)

## Moving code between host and container

### How do I get the container's commits onto the host?

```bash
jailbee git checkout <name>   # fetch, update/create and check out the host branch
jailbee git pull <name>       # fetch + merge into the host's copy of the base branch
```

`checkout` changes the host working tree and updates its submodules.
`jailbee git pull --current` merges into the host's checked-out branch instead.
After a successful pull, the default cleanup policy offers to destroy the
container and remove the merged host branch; use `--no-cleanup` to keep them.

The host–container transport uses git's `ext::` helper over `incus exec`,
without a network connection or separate Git daemon. Some bridge workflows
also fetch from upstream on the host.

→ [The git bridge](git-bridge.md#the-git-bridge)

### How do I send a host branch into a container?

`jailbee git push [<name>] [--merge|--rebase|--plain]`. `--merge`/`--rebase`
apply the pushed ref to the container's branch (conflicts are left for
`jailbee shell`); `--plain` transports refs without integrating into the
checked-out branch. With no name on an interactive terminal you get a
multi-select picker; a single eligible container still needs confirmation.
The default `push.default_action: ask` also asks which action to take.
For non-interactive use, name the target and pass an action flag or configure
a non-`ask` default.

→ [The git bridge](git-bridge.md#the-git-bridge),
[Confirming an auto-picked container](git-bridge.md#confirming-an-auto-picked-container)

### `jailbee git push` sent something I didn't expect — why?

The source defaults to the container's **base branch**
(`push.default_source`). By default JailBee fetches that branch on the host
and prefers its remote-tracking ref (`push.push_from: origin`), falling back
to the local branch when no remote-tracking ref exists. That can differ from
the local branch you expected. `--from-local` sends the local ref;
`--current` selects the host's checked-out branch instead of the base.

→ [Which copy of the source branch travels](git-bridge.md#which-copy-of-the-source-branch-travels)

### What is the "base branch" and how do I change it?

Every clone-mode container records its configured comparison branch in
`user.jailbee.base_branch`; bridge operations also maintain a
`refs/jailbee/base/<base>` anchor. The base need not be the branch it
originally forked from. AHEAD counts and committed diffs normally compare
against the host's current local base branch, falling back to its
remote-tracking ref; `jailbee git pull` normally merges into the host's base
branch. Change the configured base with
`jailbee git retarget <name> <base> [--merge]`.

→ [The git bridge](git-bridge.md#the-git-bridge)

### Do submodules work?

Yes, in both directions and without a round trip through the submodule's
upstream. On `jailbee new` they are initialised recursively and **offline**
from the read-only host-source mount; on push/pull/checkout their objects
travel over the same transport, and a sub-repo the peer lacks is created there
first. Conflicting gitlinks are merged for you where possible, with a report of
what is left. `jailbee branch <branch>` puts the whole tree —
superproject and submodules — on one branch locally, which is how you jump
back to `master` and out again in one command. `jailbee submodule checkout`
is a hidden alias kept for compatibility; it prints a pointer to
`jailbee branch`.

One limitation: the transport enumerates the *sender's checked-out state*, so
`-b <branch>` on `jailbee git merge` / `fetch` / `pull` — which reads a branch
the container does **not** have checked out — does not carry that branch's
submodules. Check the branch out in the container first and run the command
without `-b`.

→ [Submodules](git-bridge.md#submodules)

### How do I review someone's pull request?

```bash
jailbee new --pr 1234
```

The PR head is fetched into `refs/jailbee/pr/1234/head` and checked out in the
container — your own branches are untouched. Requires the `gh` CLI and
`gh auth login`; fork PRs work. Pull in commits the author pushed later with
`jailbee git push <name> --pr --rebase` (the fetch runs on the host, so no
`net loose` is needed).

→ [Reviewing a pull request](git-bridge.md#reviewing-a-pull-request),
[Round-tripping a PR container](git-bridge.md#round-tripping-a-pr-container)

### How do I open or update a PR from a container?

Run `jailbee pr <name>` **on the host**. It opens a draft PR, or pushes new
commits and optionally regenerates the description when one exists. The
title, body and head branch name are written by the repo's agent — the one
it autostarts, or the one `pr.agent` names, as long as it has a `headless`
command (`--no-ai` opts out). For a PR JailBee did not create, description
regeneration is opt-in. `--force` requires an additional confirmation unless
`--yes` is supplied; non-interactive forcing requires `--yes`.

→ [Commands](commands.md),
[A branch that already has a PR](git-bridge.md#a-branch-that-already-has-a-pr)

### How do I open a PR for a submodule?

`jailbee submodule pr [<name>] [<path>]` — a separate command from `jailbee
pr`, because a submodule is a separate GitHub repository. Without `<path>`,
an interactive terminal offers a picker, with ahead submodules first, even
for a single candidate. Non-interactively, exactly one ahead submodule is
auto-selected; otherwise name the path. The signal is the submodule's own
base anchor, not the superproject's gitlink diff, so it sees commits even
before you've committed the gitlink bump in the superproject. Base and head
branch names come from the submodule's own `.gitmodules`/remote data, not the
superproject's. Merge the submodule PR first — the superproject PR's gitlink
bump then points at a merged commit.

→ [Submodule pull requests](git-bridge.md#submodules)

### How do stacked PRs work?

Bring the first container's branch to the host, then base the second on it
(`jailbee new feat/b feat/a`). The host stays the hub and chain maintenance is
merge-based — avoid rebasing a branch with work stacked on it. When PR1
merges, `jailbee git retarget feat-b main --merge` changes B's base (replace
`main` with your integration branch).

→ [Stacked PRs](git-bridge.md#stacked-prs)

### Three containers, one branch — where do I resolve the conflicts?

Prefer a container with the project's test and lint tools available.
`jailbee git merge feat-a feat-b --into feat-c` merges each source into the
target container, one at a time, stopping at the first conflict. Resolve and
test inside `jailbee shell feat-c`, resume any remaining sources, then run
`jailbee git pull feat-c --current --no-cleanup` on the host. Omit
`--no-cleanup` if you want the usual post-pull cleanup offers.

→ [Merging several containers through one](git-bridge.md#merging-several-containers-through-one)

### How do I merge one container's branch into another?

`jailbee git merge <source…> --into <target>` — objects travel source → host
→ target without a host checkout, and no host branch, index or superproject
working tree is touched (a host sub-repo can still be created, for a
submodule born in the source container). Run it bare (`jailbee git merge`)
and it asks for the sources, then the targets, each prompt a checkbox
hiding whatever the other end holds — a container cannot merge into itself.
Repeat `--into` for several targets; each takes every source. Sources run one
at a time in the order given and stop at the first conflict, but only for the
target they were running into: every target prints what landed, what didn't
and the resume command, and a multi-target run closes with a roll-up.
`--plain` transports the refs without merging. `jailbee merge` is a
top-level alias for it.

→ [Merging one container into another](git-bridge.md#merging-one-container-into-another--jailbee-git-merge)

## Network and ports

### What is the difference between `strict` and `loose`?

`strict` uses default-deny kernel ACLs with an effective allowlist:
`egress_allow`, enabled integrations and local overrides, plus infrastructure
exceptions such as DNS and DHCP. Explicit proxy forwards are another
exception. `loose` removes JailBee's egress restrictions; host firewalls and
service authentication still apply.

Switch per container with `jailbee net strict|loose <name>`; the initial mode
comes from `defaults.network` (`strict`). Legacy networking switches between
bridges, including `jailbee-loose`. Containers on the optional `jailbee-work`
network (`jailbee net migrate`) stay on one bridge with a fixed address, and
the switch changes their ACL policy instead.

→ [Networks](config.md#networks),
[Work network](installation.md#optional-work-network-activation-and-rollback)

### Why does `git push` / `gh` fail inside a container?

Check both network access and credentials. Strict mode permits configured
GitHub HTTPS endpoints, but an IP/port ACL cannot distinguish reads from
writes. **You must supply a read-only `github.token`**: JailBee injects the
token as given, rather than reducing its permissions. A write-capable token
can permit API writes even in strict mode.

The intended workflow is to bring commits to the host (`jailbee git checkout
<name>` then `git push`, or `jailbee pr <name>`) and let agents stage issue
and PR writes in the outbox for you to publish. Loose mode can fix blocked
connections, but cannot grant missing token permissions.

→ [Git remote & push](security.md#git-remote--push)

### How do I allow a host through in strict mode?

For a host the whole team needs, add it to `egress_allow` as `host[:port]`
(a CIDR or IPv4 also works) and run `jailbee apply --no-restart`. Prefer the
`host:port` form — a port-less entry allows *any* protocol and port.
Hostnames are resolved into the ACL. The host's running refresh timer
re-resolves them roughly every minute into an IP pool bounded by age and
per-host limits, helping with rotating CDN addresses. Partial DNS failures
produce warnings; if all hostname lookups fail, apply aborts.

For a host only you need, or one you want to try before committing it,
`jailbee net egress add <host>[:<port>] [<container>]` widens that one
container's allowlist, and on a strict container it takes effect at once (on a
`loose` one it is stored and applies when the container returns to strict).
`--repo` stores a host-local override for the whole repo; run `jailbee apply`
to apply it immediately, or let the running refresh timer pick it up. Neither
touches the committed `.jailbee/config.yaml` — container entries die with
the container, repo entries live in the host-local config.
`jailbee net egress export` prints the config key back with those overrides
folded in, for when one has earned a place in git.

→ [`egress_allow`](config.md#egress_allow),
[Egress overrides](security.md#egress-overrides)

### Does `loose` stay on until I remember to switch back?

The default auto-revert policy is 5 minutes, enforced by the host's
`jailbee-net-refresh` timer installed by `jailbee setup`. Without a running
timer, expiry is not enforced automatically. Pick the TTL per switch with
`jailbee net loose <name> --for 2h`, or opt out with `--for never` /
`--no-revert`. With neither flag on an interactive terminal JailBee asks.
The policy is configurable; the remaining TTL shows in `jailbee ls` and
`jailbee net status`.

→ [`loose_auto_revert`](config.md#loose_auto_revert)

### How do I reach a host service (e.g. `adb`) from inside a container?

Declare it once for every container of the repo:

```yaml
host_ports:
  - { name: adb, port: 5037 }
```

Run `jailbee apply` for existing containers (new containers pick it up on
creation), start the host's adb server, and use `adb devices` inside with a
compatible adb client installed. `jailbee port to-container` / `to-host` /
`rm` add or remove a single forward on one container without touching the
config. A forward is a deliberate hole through the strict-mode
boundary — it bypasses the ACL because Incus's proxy device never traverses the
bridge the ACL is attached to. `jailbee net status` lists them.

→ [Talking to Android devices over `adb`](project-config.md#talking-to-android-devices-over-adb),
[Port forwards](security.md#port-forwards)

## Coding agents

### How do I get Claude Code into my containers?

In `~/.config/jailbee/global.yaml`:

```yaml
agents:
  claude:
    enabled: true
    autostart: true      # launch it in a tmux window on every start
```

Then `jailbee apply` and create a container. The Anthropic hosts are added to
the strict allowlist automatically, and JailBee's own skills are copied in so
the in-container Claude can drive `jailbee`.

→ [Claude Code in the container](getting-started.md#claude-code-in-the-container),
[Claude](agents.md#9-claude)

### Does it read my host `~/.claude`?

Not by default. `<shared_dir>/claude` is mounted as `~/.claude`, so settings,
MCP configuration and agents are shared across the repo's containers and
survive `jailbee destroy` / `jailbee new`. Runtime directories such as
`sessions/`, `daemon/` and `jobs/` are kept container-private. Claude Code's
global config (`.claude.json`) lives **inside** the shared mount: the golden
image exports `CLAUDE_CONFIG_DIR=$HOME/.claude`.

A fresh login completes onboarding inside the container. When a credential
group already has a login, JailBee can seed a new repo's onboarding and trust
state from the existing setup, depending on `seed_onboarding`.

A repo in a credential group (a generated `global.yaml` selects `default`
unless overridden) reads its login from
the group's directory, mounted at `~/.claude-creds`, so one `/login` serves
every repo in the group. `jailbee account ls|use|park|rm` manages the stored
logins. Instructions you want every container's Claude to follow go in
`~/.config/jailbee/AGENTS.md` on the host, mounted read-only as Claude Code's
managed-policy memory.

→ [Claude Code in the container](getting-started.md#claude-code-in-the-container),
[`credentials`](config.md#credentials),
[Agent-wide instructions](config.md#agent-wide-instructions-configjailbeeagentsmd)

### Can I really run an agent with permission prompts off?

It is an intended use, not a guarantee that untrusted code is harmless.
Review the boundary first: the container's clone, configured and automatically
added host mounts, shared caches and credentials, `.local/` when shared,
and forwarded sockets or ports are reachable. A writable mount can affect
the host or other containers; a read-only mount can still disclose secrets.
GPG integration also shares the host's `~/.gnupg` directory.

Strict mode restricts ordinary egress, with infrastructure and forwarding
exceptions; it does not make allowed services read-only. Keep unrelated host
paths unmounted, share as little as possible, and snapshot before a long run.
Snapshots do not roll back host-mounted or shared data.

→ [Running an agent without prompts](security.md#running-an-agent-without-prompts)

### How do I give a container temporary access to an API key?

Keep the key on the host and declare its directory as an `optional_mounts`
entry, read-only and with a short `auto_unmount_after`. `jailbee mount <kind>
<container>` attaches it for the task that needs it. Automatic detachment
requires `mount_auto_revert.enabled` and the host's refresh timer. Mount the
directory rather than the file so atomic file replacements remain visible.

Detachment removes the mount, not copies the container may have made or
credentials already loaded by a process. Use a narrowly scoped, short-lived
key and revoke it when needed.

→ [Lending a container a credential](security.md#lending-a-container-a-credential)

### Can I use an agent other than Claude Code?

The recommended setup is Claude Code. If you want models or a subscription
other than Anthropic's, run Claude Code through the LiteLLM proxy
(`claude-jb`) — tested with ChatGPT (Codex) and SuperGrok subscriptions —
rather than switching agents.

If you do want another agent, `agents:` is a mapping keyed by agent name and wires any terminal coding
agent into the same mount/egress/install/autostart pipeline. Six presets ship
(`claude`, `codex`, `gemini`, `aider`, `opencode`, `grok`), but **only `claude`
is exercised in production** — the other five are untested starting points you
are expected to correct. You can also define an agent from scratch.

→ [Generic agent support](agents.md),
[Writing your own agent](agents.md#4-writing-your-own-agent)

### I enabled an agent, but its tmux window exits with 127

Install happens **only at `jailbee new`**. Enabling an agent for an existing
container and running `jailbee apply` attaches the mount and widens egress, but
never installs the binary. Recreate the container, or install it by hand
inside.

→ [What this does](agents.md#1-what-this-does)

### Can Claude Code run on my ChatGPT (Codex) subscription?

Yes, through `claude-jb`: a LiteLLM proxy in its own container maps Claude
Code's four tiers to GPT models on your subscription, while plain `claude`
stays on your Claude login. Set `litellm.enabled: true` in `global.yaml`, then
`jailbee litellm up`, `jailbee litellm login`, `jailbee litellm up`, and
`jailbee base build` + `jailbee apply` per repo.

→ [Claude Code through LiteLLM](litellm.md#setup)

### Can `claude-jb` use OpenRouter, Grok or another API key instead?

Yes. A route names any LiteLLM model string and the *name* of a variable in
`~/.config/jailbee/litellm/secrets.env`; a profile maps tiers to routes. No
ChatGPT subscription is needed once the built-in `codex` profile points at your
routes too. An xAI API key serves routes, and a SuperGrok or X Premium+
subscription can too (`oauth: true`).

→ [Quick start by provider](litellm.md#quick-start-by-provider), [xAI subscription](litellm.md#xai-subscription)

### A new model came out — do I wait for a JailBee release?

Usually not, if the bundled LiteLLM version supports it. Override a route's
`model` and `context_window` under `litellm.routes`, or add a route and remap
a profile tier, then run `jailbee apply`. Route changes normally reload live
and subsequent requests use the new mapping. If reload cannot be confirmed,
apply may restart the affected proxy instance; `--no-restart` leaves changes
that need a restart pending.

→ [Adding a new model](litellm.md#adding-a-new-model)

### Why does `/effort` do nothing in `claude-jb`?

The tier's route fixes its effort (`effort:`), which is how Opus and Sonnet
differ when both run the same model. Use `min_effort:` for a floor that
`/effort` can go above.

→ [Routes and profiles](litellm.md#routes-and-profiles)

### How do I make `gh` work for an agent inside a container?

Set `github.enabled: true` in `~/.config/jailbee/global.yaml` and put a
read-only fine-grained PAT (Contents, Issues, Pull requests, Metadata: Read)
in the repo's host-local file, `~/.config/jailbee/repos/<container_prefix>.yaml`,
as `github.token`; `chmod 600` that file. With those token permissions, the
container's `gh` can read but not publish changes: agents stage issue and PR
writes in an outbox that you publish from the host.
The `github` block is **rejected** in a repo's `.jailbee/config.yaml` so
tokens can't leak via git, and `jailbee doctor` warns about classic (`ghp_*`)
tokens. A pre-1.6 `github.api_tokens` map in `global.yaml` still works until
2.0.0; `jailbee config migrate --apply` moves it.

→ [GitHub CLI (`gh`) inside containers](git-bridge.md#github-cli-gh-inside-containers)

## Desktop apps and dashboards

### Can I run the IDE and a browser from inside the container?

`jailbee ide <name>` launches a JetBrains IDE; `jailbee chrome <name> [URL]`
and `jailbee firefox <name> [URL]` launch a browser onto your Wayland
session (`jailbee browser` launches whichever one is `browsers.default`, or
the single enabled one). All are opt-in (`jetbrains.enabled`,
`browsers.chrome.enabled`, `browsers.firefox.enabled` — personal settings
that belong in `global.yaml`). Only one IDEA-family IDE runs at a time
across containers (shared profile); Chrome and Firefox each run
per-container from their own profile pool (`jailbee pool ls/prune
chrome-profile` / `firefox-profile`, or the deprecated `jailbee chrome-pool
ls/prune` alias for the Chrome one). Beyond the two built-in browsers, an
`apps:` entry (an AppImage, a vendor binary, a wrapper script) launches with
`jailbee apps run <name>` — see [`apps`](config.md#apps). Firefox defaults
to `browsers.firefox.source: image`, so turning it on for the first time
also needs a `jailbee base build` before it's actually in the image — not
only when later changing `source`.

→ [Limitations](security.md#limitations),
[Configuration reference](config.md)

### Is there a graphical dashboard?

`jailbee gui` (or `jailbee dashboard --gui`) — the Qt counterpart to the TUI,
requiring the `gui` extra. It detaches to the background by default and logs to
`/tmp/jailbee-gui.log`; `--foreground` keeps it attached. Interactive actions
open in a host terminal emulator (`$JAILBEE_TERMINAL` forces one).

→ [`jailbee gui`](commands.md#jailbee-gui--jailbee-dashboard---gui)

### How do GPG signing and SSH work inside a container?

With `gpg.enabled`, JailBee forwards the host gpg-agent sockets and sets
`SSH_AUTH_SOCK` for its SSH socket. It also mounts the host's **whole
`~/.gnupg` directory read-only** by default: private key files stored there
may be readable from the container. Read-only does not prevent copying,
and a forwarded agent can perform signing or authentication operations.
A smartcard can keep its key material off disk and require a touch.

With `ssh.enabled` and `ssh.seed_from_host: true`, SSH seeding copies only
`config`, `known_hosts` and `config.d/`, not private keys or `authorized_keys`.
SSH authentication still needs an available agent identity and network
access.

→ [Security model](security.md#security-model),
[Sharing host sockets](project-config.md#sharing-host-sockets)

## Configuration

### Where does configuration live, and which file wins?

Three layers, deep-merged in this order: `~/.config/jailbee/global.yaml`
(personal, every repo), `<repo>/.jailbee/config.yaml` (checked in, shared with
the team) and the optional `~/.config/jailbee/repos/<container_prefix>.yaml`
(personal, this repo only — never committed). Scalars from a later layer win,
**lists normally append**, and `[]` resets a list to empty. One exception is
`apps.<name>.command`: a later command replaces the earlier one, including
list-form commands. Inspect with
`jailbee config show --layer global|repo|local|effective`; edit the host-local
file with `jailbee config edit --local`. `jailbee config migrate` previews
moving older per-repo settings out of `global.yaml` into it (`--apply`
writes).

→ [Configuration layers](config.md#configuration-layers),
[Host-local overrides](config.md#host-local-overrides),
[Merge rules](config.md#merge-rules)

### What belongs in the global file and what in the repo file?

Personal things — UID/GID, credential mounts, IDE and Chrome preferences,
agent API keys — go global. Personal things that concern one repo — its GitHub
token, its credential group, your own extra egress for it — go in its host-local
file. Repo-shaped things — stacks, autostart, resource limits, repo-specific
egress — go in the repo file. Most fields are legal at any layer and the split
is convention, but `github`, `credentials` and `litellm` are rejected in the
committed repo file outright.

→ [Recommended placement](config.md#recommended-placement)

### How do I add language toolchains or extra apt packages?

The golden image is stack-neutral by default. Turn runtimes on with
`golden.stacks` (which also wires up the matching shared caches), and add
system packages with `golden.extra_apt_packages`:

```yaml
golden:
  stacks: { node: 22, docker: true }
  extra_apt_packages: [postgresql-client]
```

Then `jailbee base build`.

→ [Stacks](config.md#stacks-goldenstacks),
[Optional — stack runtimes](project-config.md#4-optional--stack-runtimes-and-extra-apt-packages)

### How do I run setup steps or start services automatically?

`autostart.on_create` runs during `jailbee new`; `autostart.on_start` runs
after it on that first boot, and on subsequent `jailbee start` / `restart`,
unless autostart is skipped. Each step is a shell command run as the dev
user, with optional `working_dir`, `background`, per-step `mounts` and
`network`.
Auto-launching the IDE or a GUI app is configured *outside* that block, via
`jetbrains.autostart`, `browsers.<name>.autostart` and `apps.<name>.autostart`.

→ [Define autostart steps](project-config.md#5-define-autostart-steps),
[`autostart`](config.md#autostart)

### How do containers of the same repo share caches and credentials?

Through `<shared_dir>` — one host directory per repo. Most entries are
bind-mounted read-write into every container of the repo, so a
package-manager cache stays warm across branches and settings written in
one container appear in the next. A few caches — Gradle and Maven by
default, since their tools take an inter-process lock on the cache
directory — are instead **pooled**: each container gets its own private
slot, seeded from an existing one rather than sharing lock-bearing state
live. Seeded artifacts can be hardlinked: a slot avoids lock contention, but
is not a security boundary between containers. Shared state and retained
pool slots outlive container destruction, subject to pool cleanup.

This layer is separate from host dotfiles by default; explicitly mounted
host paths and mount-mode working trees are separate exposures, and writable
mounts let container changes reach the host.

→ [`shared_caches`](config.md#shared_caches),
[`pooled_caches`](config.md#pooled_caches),
[`shared_dir`](config.md#shared_dir)

### How do I stop two repos from colliding on this host?

Choose a unique `container_prefix` per repo. Repo containers and profiles
use it; the default is the repo directory name and the value must match
`[a-z0-9][a-z0-9-]*`. Set it explicitly for names with capitals, dots or
underscores, or for two repos with the same directory name.

Host-wide resources such as the `jailbee-loose` bridge, registry mirror and
LiteLLM service are intentionally shared, not isolated by the repo prefix.

→ [Configure](getting-started.md#configure),
[`container_prefix`](config.md#container_prefix)

### Is there a quick way to pass a file between host and container?

Yes — if a `.local/` directory exists at the repo root it is bind-mounted
read-write into each new container at `~/<container_prefix>/.local`, and it is
added to the clone's `.git/info/exclude` so it never shows up as untracked.
Presence-triggered and never auto-created; disable with `share_local: false`.

→ [Sharing files with `.local`](project-config.md#sharing-files-with-local)

## When something breaks

### Where do I start?

`jailbee doctor`, from inside the repo. It checks host and config and names
most problems — uid delegation, bridge reachability, keyring quota, Incus
reachability, GitHub token shape — with a remediation hint.

→ [Start with `jailbee doctor`](troubleshooting.md#start-with-jailbee-doctor)

### A container gets no IPv4 address

A host firewall blocking DHCP is a common cause, but not the only one.
Leave the container running, allow time for a lease, and run `jailbee doctor`.
Its bridge checks report missing IPv4 and test TCP reachability to the
bridge's DNS port and an external endpoint. These are diagnostic clues,
not proof of a particular firewall rule or successful DNS resolution; without
a running container the path cannot be fully verified.

If a firewall is responsible, follow the matching firewalld or UFW recipe
**for each bridge in use**. DNS and forwarding failures can also occur after
a container has obtained IPv4.

→ [Containers get no IPv4 address](troubleshooting.md#containers-get-no-ipv4-address),
[Host networking](installation.md#host-networking-only-if-you-use-a-firewall)

### "disk quota exceeded" when starting a container or Docker

When `runc` reports this while starting nested Docker, it can mean the host's
**kernel keyring quota** is exhausted. Check `jailbee doctor`'s keyring result
before raising `kernel.keys.maxkeys` and related limits via `/etc/sysctl.d/`.
The same error in another context can mean an actual storage quota problem;
the message alone does not distinguish them.

→ [Kernel keyring limits](installation.md#kernel-keyring-limits-running-many-containers-in-parallel)

### How much disk is all this using?

`jailbee disk-usage` gives an estimated breakdown; directory-based apparent
sizes and image metadata are not exclusive physical usage on a copy-on-write
pool. `jailbee base usage` lists the repo's golden (image or golden instance)
and any dated archives left by jailbee before 1.7.5; `jailbee base prune`
removes those archives and keeps the current golden. Check the storage
backend's own accounting for physical free space.

→ [Storage](storage.md), [Commands](commands.md)

### How do I remove JailBee?

There is no `jailbee uninstall`; teardown is manual and ordered — per-repo
resources first (containers, profiles, ACLs, image, shared dir, host-local
file), then host-wide ones (SSH service, LiteLLM proxy, shared display,
registry mirror, refresh timer, shared ACLs, the `jailbee-loose` and
`jailbee-work` bridges, the CLI itself) once the last JailBee repo is gone.

→ [Removing JailBee](troubleshooting.md#removing-jailbee)
