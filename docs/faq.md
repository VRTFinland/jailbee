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

One unprivileged Incus **system container per git branch** — its own init,
Docker daemon, IP, services and database — so several branches run in parallel
on one Linux host without port or Compose-name collisions. The container is
also a git remote, so commits move between host and container locally.

→ [Who JailBee is for](comparison.md), [Architecture](architecture.md)

### What do I need on my machine?

A Linux host (Ubuntu 26.04+ recommended), **Incus 6.0.5-8 or newer**, and a way
to install a Python CLI (`uv` or `pipx`). Host-installed Chrome and JetBrains
Toolbox are needed only for the GUI passthrough features.

→ [Prerequisites](installation.md#prerequisites)

### Can I use it from a Mac?

Only by running JailBee *inside* a Linux VM (Colima/Lima) with the repo shared
from macOS — the Incus daemon is Linux-only, and a native macOS client cannot
make the Linux daemon bind-mount macOS paths. That path is **experimental and
not yet verified on real Apple hardware**, and `jailbee ide`, `jailbee chrome`
and GPG signing are unavailable there.

→ [Running JailBee on macOS](macos.md), [Limitations](security.md#limitations)

### What does it cost me compared to `git worktree` + `docker compose -p`?

Real host setup (an afternoon, once), a ~10–15 min golden-image build per repo
stack, disk per container, Linux-only, and a shared kernel rather than a
hypervisor boundary. If your stack is one process and one database, worktrees
plus Compose are free and already installed.

→ [What JailBee costs you](comparison.md#what-jailbee-costs-you)

## Installation and first run

### How do I install the CLI?

```bash
uv tool install jailbee            # or: pipx install jailbee
uv tool install 'jailbee[gui]'     # with the optional Qt dashboard
```

This installs `jailbee` and the short alias `jb`. JailBee is an ordinary PyPI
package — a virtualenv you manage yourself works too; only a bare
`pip install` into Ubuntu's system Python is refused (PEP 668).

→ [Install JailBee](installation.md#3-install-jailbee)

### What is the minimum host setup?

Four one-time steps: install and initialise Incus, add the two
`/etc/subuid`/`/etc/subgid` delegation lines, install the CLI, and check
`jailbee version`. Everything past that in the install page is **conditional**
— apply it only if `jailbee doctor` or the symptom says so.

→ [Quick install](installation.md#quick-install)

### Why does it need an extra `/etc/subuid` line?

JailBee mounts a few host files (`~/.gitconfig`, `~/.gnupg`, the GPG agent
socket) into every container **as your real host UID** via `raw.idmap`, while
the container otherwise runs unprivileged. `newuidmap` refuses to install that
carve-out unless it is delegated. The security impact is effectively none: it
authorises root to delegate exactly one UID — your own.

Without the line, containers are created and stay `STOPPED`, with the reason
only in `incus info --show-log <name>`. `jailbee doctor`'s `uid delegation`
check reads both files and names the missing line.

→ [Why the UID mapping is needed](installation.md#why-the-uid-mapping-is-needed)

### What do I run the first time in a repo?

```bash
jailbee config init      # write .jailbee/config.yaml
jailbee doctor           # sanity-check host + config
jailbee init             # profiles, ACL, jailbee-loose bridge, shared dirs
jailbee registry up      # Docker users only — host-level Docker registry mirror
jailbee base build       # golden image, ~10–15 min, one time
jailbee new feat/x       # first container
```

→ [Initialize and build](getting-started.md#initialize-and-build),
[Setting up JailBee in your own project](project-config.md)

### When do I have to rebuild the golden image?

After changing anything the image bakes in — `golden.stacks`,
`golden.extra_apt_packages`, `golden.enable_snippets`, or a custom
`provision_script`. Run `jailbee base build` again; existing containers keep
the old image until they are recreated. Old dated archives are cleaned up with
`jailbee base prune`.

→ [Stack runtimes and extra apt packages](project-config.md#4-optional--stack-runtimes-and-extra-apt-packages)

### What is the difference between `jailbee init` and `jailbee apply`?

`init` is first-time setup — it creates the Incus profiles, the ACL and the
shared directories. `apply` re-applies the current config to what already
exists (profiles, ACL, `/etc/hosts`, dockerd proxy) and offers to restart
containers when profiles changed. Run `apply` after editing config;
`apply --no-restart` pushes an egress change live without a restart.

→ [Commands](commands.md)

## Everyday container work

### How do I create a container for a branch?

`jailbee new <name> [<base>]`. `<name>` is the environment name and the branch
inside the container — Incus names can't contain `/`, so `feat/my-feature`
becomes the container `feat-my-feature`. `<base>` sets the base branch: it is
forked when `<name>` is new, and used purely as the comparison anchor when
`<name>` already exists. `jailbee new --current` takes the host's checked-out
branch.

→ [Choosing the starting point for `jailbee new`](git-bridge.md#choosing-the-starting-point-for-jailbee-new)

### `jailbee new` blocks for minutes — can I get my shell back?

Pass `--background` (`-b`). `jailbee ls` then shows a `JOB` column with the
live phase (`creating` → `cloning` → `autostart`). A failed job leaves the
container intact for inspection; clear the record with `jailbee job clear`
once you've fixed it, and read the worker log with `jailbee job log`. Set
`new.background: true` to make it the default.

`jailbee start` and `jailbee restart` take the same flag — there the wait is
the `on_start` autostart run, not the boot — with `boot.background: true` as
the config default for both. A failed *boot* record needs no acknowledging:
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
jailbee snapshot restore <name> before-migration
```

Worth doing before a long unattended agent run.

→ [Commands](commands.md),
[Running an agent without prompts](security.md#running-an-agent-without-prompts)

### How do I tear a container down without losing work?

`jailbee destroy <name>` (or `--all` for the repo). Before the usual
confirmation JailBee assesses what would be lost — dirty tree, changed
submodule, commits held nowhere else — and asks a second time, defaulting to
No, if anything is at risk. `--force` skips both prompts *and* the assessment.
`jailbee prune` cleans up stale containers interactively.

→ [Commands](commands.md)

## Moving code between host and container

### How do I get the container's commits onto the host?

```bash
jailbee git checkout <name>   # fetch + fast-forward/create the host branch
jailbee git pull <name>       # fetch + merge into the container's BASE branch
```

`jailbee git pull --current` merges into the host's checked-out branch instead.
The transport is git's `ext::` helper over `incus exec` — no daemon, port, key
or network involved.

→ [The git bridge](git-bridge.md#the-git-bridge)

### How do I send a host branch into a container?

`jailbee git push [<name>] [--merge|--rebase|--plain]`. `--merge`/`--rebase`
apply the pushed ref to the container's branch (conflicts are left for
`jailbee shell`); `--plain` is transport only. With no name on a TTY you get a
multi-select picker; with exactly one eligible container JailBee prints what it
is about to do and waits for confirmation.

→ [The git bridge](git-bridge.md#the-git-bridge),
[Confirming an auto-picked container](git-bridge.md#confirming-an-auto-picked-container)

### `jailbee git push` sent something I didn't expect — why?

Two defaults do the surprising part. The source defaults to the container's
**base branch** (`push.default_source`), and JailBee pushes the *`origin/`
copy* of it rather than your local ref (`push.push_from`), because the local
branch of a base you never check out is stale precisely when you just fetched.
`--from-local` sends the local branch, `--current` sends the checked-out one.

→ [Which copy of the source branch travels](git-bridge.md#which-copy-of-the-source-branch-travels)

### What is the "base branch" and how do I change it?

Every clone-mode container records the branch it forked from
(`user.jailbee.base_branch`, plus a `refs/jailbee/base/<base>` anchor).
`jailbee ls`'s AHEAD counts, `jailbee git diff` and `jailbee git pull` are all
measured against it. Re-point it afterwards with
`jailbee git retarget <name> <base> [--merge]`.

→ [The git bridge](git-bridge.md#the-git-bridge)

### Do submodules work?

Yes, in both directions and without a round trip through the submodule's
upstream. On `jailbee new` they are initialised recursively and **offline**
from the read-only host-source mount; on push/pull/checkout their objects
travel over the same transport, and a sub-repo the peer lacks is created there
first. Conflicting gitlinks are merged for you where possible, with a report of
what is left. `jailbee submodule checkout -b <branch>` puts the whole tree —
superproject and submodules — on one branch locally, which is how you jump
back to `master` and out again in one command.

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

`jailbee pr <name>` opens a draft PR, or pushes new commits and optionally
regenerates the description when one exists. With Claude enabled the title,
body and head branch name are AI-generated (`--no-ai` opts out). On a PR
JailBee did not create it stays hands-off: the description is never regenerated
unless you ask, and `--force` asks a second time.

→ [Commands](commands.md),
[A branch that already has a PR](git-bridge.md#a-branch-that-already-has-a-pr)

### How do I open a PR for a submodule?

`jailbee submodule pr [<name>] [<path>]` — a separate command from `jailbee
pr`, because a submodule is a separate GitHub repository. Without `<path>`,
the submodule with commits ahead of its own base is targeted automatically;
several ahead means naming one. The signal is the submodule's own base
anchor, not the superproject's gitlink diff, so it sees the commits even
before you've committed the gitlink bump in the superproject. Base and head
branch names come from the submodule's own `.gitmodules`/remote data, not the
superproject's. Merge the submodule PR first — the superproject PR's gitlink
bump then points at a merged commit.

→ [Submodule pull requests](git-bridge.md#submodules)

### How do stacked PRs work?

Base the second container on the first one's branch
(`jailbee new feat/b feat/a`). The host stays the hub and chain maintenance is
merge-based — never rebase a branch with work stacked on it. When PR1 merges,
`jailbee git retarget feat-b main --merge` flips B's base.

→ [Stacked PRs](git-bridge.md#stacked-prs)

### Three containers, one branch — where do I resolve the conflicts?

In a container, not on the host: the host is the one place with no test suite,
no lint gate and no agent. Send each branch into *one* of the containers with
`jailbee git push feat-c --current --merge`, resolve there, then
`jailbee git pull feat-c --current` onto the host.

→ [Merging several containers through one](git-bridge.md#merging-several-containers-through-one)

## Network and ports

### What is the difference between `strict` and `loose`?

`strict` is a default-deny kernel ACL — only `egress_allow` destinations are
reachable. `loose` permits all egress over a dedicated `jailbee-loose` bridge.
Switch per container with `jailbee net strict|loose <name>`; the initial mode
comes from `defaults.network` (`strict`).

→ [Networks](config.md#networks)

### Why does `git push` / `gh` fail inside a container?

By design: `github.com` is **not** in the default strict-mode allowlist, so
day-to-day work runs offline-of-GitHub and an unattended agent cannot produce a
surprise push. Either bring the commits to the host
(`jailbee git checkout <name>` → `git push`), or switch to loose for the write
and back again.

→ [Git remote & push](security.md#git-remote--push)

### How do I allow a host through in strict mode?

For a host the whole team needs, add it to `egress_allow` as `host[:port]`
(a CIDR or IPv4 also works) and run `jailbee apply --no-restart`. Prefer the
`host:port` form — a port-less entry allows *any* protocol and port.
Hostnames are resolved into the ACL and re-resolved every minute into a
cumulative IP pool, so CDN addresses that rotate keep working; a hostname
that fails to resolve aborts the whole apply.

For a host only you need, or one you want to try before committing it,
`jailbee net egress add <host>[:<port>] [<container>]` widens that one
container's allowlist, and on a strict container it takes effect at once (on a
`loose` one it is stored and applies when the container returns to strict).
`--repo` widens every container of the repo on this machine instead, pushed by
the next `jailbee apply`. Neither touches `config.yaml` — container entries
die with the container, repo entries are host-local state.
`jailbee net egress export` prints the config key back with those overrides
folded in, for when one has earned a place in git.

→ [`egress_allow`](config.md#egress_allow),
[Egress overrides](security.md#egress-overrides)

### Does `loose` stay on until I remember to switch back?

No — by default it auto-reverts after 5 minutes. Pick the TTL per switch with
`jailbee net loose <name> --for 2h`, or opt out with `--for never` /
`--no-revert`. With neither flag on a TTY JailBee asks. The remaining TTL shows
in `jailbee ls` and `jailbee net status`.

→ [`loose_auto_revert`](config.md#loose_auto_revert)

### How do I reach a host service (e.g. `adb`) from inside a container?

Declare it once for every container of the repo:

```yaml
host_ports:
  - { name: adb, port: 5037 }
```

Then plain `adb devices` works inside. `jailbee port to-container` /
`to-host` / `rm` add or remove a single forward on one container without
touching the config. A forward is a deliberate hole through the strict-mode
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

No. `<shared_dir>/claude` is mounted as `~/.claude`, so one login, settings,
MCP servers and agents are shared across the repo's containers and survive
`jailbee destroy` / `jailbee new`. Claude Code's global config
(`.claude.json`) lives **inside** that mount: the golden image exports
`CLAUDE_CONFIG_DIR=$HOME/.claude`, and Claude Code reads
`(CLAUDE_CONFIG_DIR || $HOME)/.claude.json`. Claude runs its onboarding once,
inside the first container.

→ [Claude Code in the container](getting-started.md#claude-code-in-the-container)

### Can I really run an agent with permission prompts off?

That is the intended mode inside a container: you size the blast radius once in
the config instead of adjudicating it prompt by prompt. Know what you sized —
the container's clone, everything in `host_mounts`, and the **shared** state
layer (including Claude's own credentials) are reachable; your host filesystem,
unattached `optional_mounts`, private keys and every off-allowlist host are
not. Stay in `strict` and snapshot before a long run.

→ [Running an agent without prompts](security.md#running-an-agent-without-prompts)

### Can I use an agent other than Claude Code?

Yes — `agents:` is a mapping keyed by agent name and wires any terminal coding
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

### How do I make `gh` work for an agent inside a container?

Put a fine-grained PAT per GitHub owner in `~/.config/jailbee/global.yaml`
under `github.api_tokens`, keyed by each repo's `container_prefix`. The
`github` block is **rejected** in a repo's `.jailbee/config.yaml` so tokens
can't leak via git. `chmod 600` the file; `jailbee doctor` warns about loose
permissions and classic (`ghp_*`) tokens.

→ [GitHub CLI (`gh`) inside containers](git-bridge.md#github-cli-gh-inside-containers)

## Desktop apps and dashboards

### Can I run the IDE and a browser from inside the container?

`jailbee ide <name>` launches a JetBrains IDE and `jailbee chrome <name> [URL]`
a Chrome onto your Wayland session. Both are opt-in
(`jetbrains.enabled`, `chrome.enabled`, personal settings that belong in
`global.yaml`). Only one IDEA-family IDE runs at a time across containers
(shared profile); Chrome runs per-container from a profile pool
(`jailbee pool ls/prune chrome-profile`, or the deprecated
`jailbee chrome-pool ls/prune` alias).

→ [Limitations](security.md#limitations),
[Configuration reference](config.md)

### Is there a graphical dashboard?

`jailbee gui` (or `jailbee dashboard --gui`) — the Qt counterpart to the TUI,
requiring the `gui` extra. It detaches to the background by default and logs to
`/tmp/jailbee-gui.log`; `--foreground` keeps it attached. Interactive actions
open in a host terminal emulator (`$JAILBEE_TERMINAL` forces one).

→ [`jailbee gui`](commands.md#jailbee-gui--jailbee-dashboard---gui)

### How do GPG signing and SSH work inside a container?

JailBee shares the host agent's **socket**, not the key: with `gpg.enabled` the
host gpg-agent socket is attached and `SSH_AUTH_SOCK` points at its SSH socket,
so `git commit -S` and `ssh` work inside while the private key never leaves the
host — a smartcard can still demand a touch per signature. `ssh.enabled` seeds
only `config`, `known_hosts` and `config.d/`; private keys and
`authorized_keys` are never seeded.

→ [Security model](security.md#security-model),
[Sharing host sockets](project-config.md#sharing-host-sockets)

## Configuration

### Where does configuration live, and which file wins?

Two layers, deep-merged: `~/.config/jailbee/global.yaml` (personal, every repo)
and `<repo>/.jailbee/config.yaml` (checked in, shared with the team). Scalars
from the repo layer win, **lists append**, and `[]` in the repo resets a list
to empty. Inspect with `jailbee config show --layer global|repo|effective`.

→ [Configuration layers](config.md#configuration-layers),
[Merge rules](config.md#merge-rules)

### What belongs in the global file and what in the repo file?

Personal things — UID/GID, credential mounts, IDE and Chrome preferences,
GitHub tokens, agent API keys — go global. Repo-shaped things — stacks,
autostart, resource limits, repo-specific egress — go in the repo file. All
fields are technically legal at either layer; the split is convention, except
that `github` is rejected at the repo layer outright.

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

`autostart.on_create` fires on `jailbee new`, `autostart.on_start` on
`jailbee start`. Each step is a shell command run as the dev user, with
optional `working_dir`, `background`, per-step `mounts` and `network`.
Auto-launching the IDE or Chrome is configured *outside* that block, via
`jetbrains.autostart` / `chrome.autostart`.

→ [Define autostart steps](project-config.md#5-define-autostart-steps),
[`autostart`](config.md#autostart)

### How do containers of the same repo share caches and credentials?

Through `<shared_dir>` — one host directory per repo. Most entries are
bind-mounted read-write into every container of the repo, so a
package-manager cache stays warm across branches and settings written in
one container appear in the next. A few caches — Gradle and Maven by
default, since their tools take an inter-process lock on the cache
directory — are instead **pooled**: each container gets its own private
slot, seeded from the warmest existing one rather than shared live, so two
containers never contend on one lock file. Either way it outlives
`jailbee destroy` / `jailbee new`, and it is a layer of its own, so nothing
a container does reaches your host dotfiles.

→ [`shared_caches`](config.md#shared_caches),
[`pooled_caches`](config.md#pooled_caches),
[`shared_dir`](config.md#shared_dir)

### How do I stop two repos from colliding on this host?

Every JailBee-owned Incus resource is prefixed with `<container_prefix>-`,
which defaults to the repo directory name and must match `[a-z0-9][a-z0-9-]*`.
Set `container_prefix:` explicitly if your directory name has capitals, dots or
underscores.

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

A host firewall is blocking DHCP/DNS or forwarding on the JailBee bridges. Add
the firewalld zone entries, or the UFW `route` rules plus the three
`before.rules` lines **per bridge**. With the container left running,
`jailbee doctor`'s `network <bridge> reachability` check tells the three
missing openings apart — no lease, no DNS, or no forwarding — and names the
rule to add. It needs a container on the bridge to read a symptom from, so on
a fresh host it stays silent until one is running.

→ [Containers get no IPv4 address](troubleshooting.md#containers-get-no-ipv4-address),
[Host networking](installation.md#host-networking-only-if-you-use-a-firewall)

### "disk quota exceeded" when starting a container or Docker

That is the host **kernel keyring** quota, not disk space — it runs out after a
handful of concurrent containers. Raise `kernel.keys.maxkeys` and friends via
`/etc/sysctl.d/`.

→ [Kernel keyring limits](installation.md#kernel-keyring-limits-running-many-containers-in-parallel)

### How much disk is all this using?

`jailbee disk-usage` for the breakdown, `jailbee base usage` for the golden
images and their dated archives, and `jailbee base prune` to remove superseded
archives (the live base is always kept).

→ [Commands](commands.md)

### How do I remove JailBee?

There is no `jailbee uninstall`; teardown is manual and ordered — per-repo
resources first (containers, profiles, ACL, image, shared dir), then host-wide
ones (registry mirror, the `jailbee-loose` bridge, the CLI itself) once the
last JailBee repo is gone.

→ [Removing JailBee](troubleshooting.md#removing-jailbee)
