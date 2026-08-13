# Who jailbee is for

jailbee gives each git branch a **system container**: an unprivileged Incus
container with its own init, its own Docker daemon, its own IP, and — if you
want it — its own browser and IDE on your desktop. From userspace up it
behaves like a separate Linux machine, and several of them run in parallel on
one Linux host.

It is a container, not a VM. The host kernel is shared, so the boundary is
kernel-enforced — user namespaces, cgroups, AppArmor, seccomp — rather than a
hypervisor. What that buys over a sandbox applied inside your own user session
is a different *kind* of boundary: the container gets its own uid range, its
own filesystem tree, its own PID space and its own network stack, instead of a
restricted view of yours. What it does not buy is virtualisation-grade
separation; see [Security and limitations](security.md).

That is a heavier answer than most tools in this space give, and it is the
right one for a specific situation.

## You'll recognise yourself if…

- **You're on Linux** and you control the host.
- **Your work isn't just editing files.** It builds, boots a stack of
  services, runs a database, spawns an emulator, opens a browser, shells out
  to `docker`. The interesting part happens at *runtime*.
- **You want several of those running at once** — a feature, a review, an
  agent experiment — without renaming Compose projects or hunting for a free
  port.
- **You point coding agents at it and leave the room.** You want the blast
  radius bounded by something the agent cannot argue with, and a snapshot to
  roll back to when it goes wrong.

If that's you, the rest of this page explains why the heavier boundary pays
for itself.

## The idea: isolation by boundary, not by allowlist

Most sandboxes work by enumeration. You declare which paths, which hosts,
which syscalls, which tools — and everything the workload does has to fit
through that list. It's precise, and it works beautifully when you know in
advance what the workload will do.

Agentic development is the case where you don't. The agent decides it needs
`docker compose up`. Then an Android emulator. Then a headless browser, then a
real one. Then a package manager you've never installed. **With an allowlist,
every one of those is a policy change, and each one widens the policy for
everything else.** The isolation degrades exactly as the work gets
interesting.

jailbee moves the boundary instead of enumerating through it. The container *is*
the boundary; what happens inside is your business. Adding a tool costs
nothing, because nothing was enumerated in the first place.

Concretely, that means the container behaves like a Linux box you own:

| You want to… | Inside a jailbee container |
|---|---|
| Run the repo's existing `docker-compose.yml` | Works unmodified — the container runs **its own Docker daemon** (`security.nesting`). No project renaming, no port remapping, no adapter for your stack. |
| Run an Android emulator or a KVM VM | Declare `host_devices: [{ path: /dev/kvm }]` and it's there. Same for `/dev/net/tun`, a USB device, whatever the repo needs. |
| Run systemd services | It has systemd. |
| Test your stack in a real browser | `jailbee chrome <name>` launches Chrome **inside the container**, rendered onto your Wayland session. It reaches the container's own `localhost:3000`. |
| Use a JetBrains IDE against the code | `jailbee ide <name>`, same passthrough. |
| Install something weird | `apt install` it. It's a full Linux userland, not a locked-down image. |

The browser point is worth dwelling on, because it's where the boundary
choice becomes visible. Five containers can each serve port 3000 and each
have their own Chrome pointed at it. Nothing is forwarded to the host,
nothing collides, and no configuration decided in advance which ports were
interesting. Tools that expose services *to the host* have to allocate ports;
jailbee doesn't have the problem.

### And the agent is still fenced in

Genericity inside the container doesn't mean the container is open:

- **Per-container egress allowlist** (`jailbee net strict|loose`), enforced by a
  kernel-level ACL. `github.com` is deliberately **not** in the default strict
  list, so an unattended agent can't surprise-push. Flip to `loose` for the
  minute you need it, with an auto-revert TTL.
- **Secrets are read-only or absent.** GnuPG, SSH agent and gitconfig are
  bind-mounted read-only; everything else stays out unless you declare it.
- **Snapshots.** `jailbee snapshot create` before you let it run, `restore` when
  it doesn't work out.
- **The container holds its own clone**, so a wrecked environment can't take
  your working tree with it. `jailbee git push/pull/diff` and `jailbee pr` move the
  commits you want to keep.

This is why running an agent with its own in-process sandbox *disabled* is
reasonable inside a strict-mode jailbee container — see
[Security and limitations](security.md).

## The repo decides how its containers run

The environment isn't configured on your laptop — it's declared in the repo,
in a committed `.jailbee/config.yaml`. Clone the repo, run `jailbee new`, and you get
the same environment your colleague gets. Change the file and the change goes
through review like any other.

That file is where the genericity above is actually spent:

- **Bind mounts.** `host_mounts` declares what the container sees of the host,
  read-only by default. `optional_mounts` are declared but detached until you
  ask (`jailbee mount <kind> <name>`), so sensitive things like `~/.aws` stay out
  of an unattended agent's reach unless you attach them deliberately.
- **Devices** (`host_devices`), **egress allowlist**, **shared caches** that
  survive `jailbee destroy`, and the **golden image's** language stacks and
  `install.d/` provisioning snippets.
- **What happens on create** — `autostart` boots the repo's services and a
  tmux session, so `jailbee new` ends with a running stack, not an empty shell.
- **Behavioural defaults** — confirmation prompts, push/pull semantics, which
  columns `jailbee ls` and the dashboards remember.

A host-level `global.yaml` holds your machine's own settings and the repo's
config layers on top (per-repo entries append; `[]` resets). Editing either
one is `jailbee apply`, not a rebuild — network mode, mounts and profiles change
under running containers.

## Getting code in and out

The container holds its own clone, which is what makes it disposable. The
cost of that is transport, so jailbee makes the container a git remote:

- `jailbee git push` / `pull` / `fetch` / `checkout` / `diff` move commits between
  host and container without a round trip through GitHub. Container branches
  land in `refs/jailbee/<short>/*` on the host.
- Each container knows its **base branch**, so `jailbee ls` can show how far ahead
  it is, and `jailbee git retarget` re-points it when a stacked PR's base moves.
- `jailbee new --pr <N>` builds a container from a pull request for review;
  `jailbee pr` opens or updates a draft PR from a container, generating the branch
  name and description when `claude.enabled`.
- `jailbee destroy` checks first whether anything would be lost — dirty tree,
  changed submodule, commits held nowhere else — and makes you confirm twice
  if so.

## Driving it

`jailbee` is a large CLI, not three verbs: lifecycle, git bridge, PRs, network
modes, snapshots, mounts, GUI launches, golden-image management, background
jobs (`--background` plus `jailbee job ls/log/clear`), diagnostics (`jailbee doctor`,
`jailbee disk-usage`), and JSON output (`-o json`, `--fields`) for scripting.
Shell completion covers container names, branches and snapshot tags.

For the overview there are two dashboards, both spanning **every repo** on
the host rather than one: `jailbee dashboard`, a live TUI, and `jailbee gui`, a Qt
window with table and card layouts. Both list containers with their git
status and offer the same per-container actions — shell, tmux, IDE, Chrome,
destroy — from a menu.

## How this differs from the alternatives

Four projects come up often. They are all good, and they sort into two
families that answer two different questions: *"how do I get the right
toolchain, per branch?"* and *"how do I stop the agent from wrecking my
laptop?"* jailbee is trying to answer both at once, which is why it is
heavier than either.

| | **Dev Containers** | **BranchBox** | **nono** | **Docker Sandboxes** | **jailbee** |
|---|---|---|---|---|---|
| Isolates | a toolchain | a per-feature app stack | a process tree | an agent session | a full userland |
| Boundary | host Docker daemon | host Docker daemon | Landlock + seccomp, Seatbelt | hypervisor, own kernel | unprivileged Incus container, shared kernel |
| Your working tree | bind-mounted read-write (or cloned into a volume) | worktree on the host, read-write | the host tree itself | mounted read-write at the same path; `--clone` for a private copy | the container's own clone; commits move over a git bridge |
| Nested Docker daemon | privileged `docker-in-docker`, or the host socket | no — shares the host daemon | no | yes, one per sandbox | yes (`security.nesting`) |
| Desktop browser / IDE **inside** the boundary | no | no | n/a — they run on the host | no | yes, on your Wayland session |
| Egress control | none | none | per-tool L7 credential proxy | deny-by-default HTTP(S) proxy; other protocols dropped | kernel ACL, any protocol, `strict`/`loose` + TTL |
| Environment spec | committed `.devcontainer/` | generated from stack detection | agent profile (JSON, registry) | template image + kit YAML | committed `.jailbee/config.yaml` |
| Platforms | Linux, macOS, Windows | Linux, macOS | Linux, macOS, WSL2 | macOS (Apple silicon), Windows 11, Ubuntu 24.04+ — hardware virtualisation required | Linux |
| Licence | open spec (MIT); VS Code's extension is Microsoft's | MIT | Apache-2.0 | free CLI, Docker sign-in required, governance is paid | GPL-3.0-or-later |

### Toolchain per branch: Dev Containers and BranchBox

**[Dev Containers](https://containers.dev/)** is the default answer in this
space and the one most teams should try first: a committed
`.devcontainer/devcontainer.json`, a Features marketplace, prebuilt images,
three host OSes, and implementations beyond VS Code (GitHub Codespaces,
JetBrains, DevPod). If what you need is "everyone gets the same Node and the
same `psql`", it wins on adoption cost and jailbee has nothing to add.

It is a *toolchain* boundary, not a containment one, and its defaults all
point away from the latter. Your folder is bind-mounted read-write, so an
agent inside is editing your real tree — "Clone Repository in Container
Volume" is the isolated alternative, and it's a deliberate choice, not the
default. Git credential helpers and your SSH agent are forwarded
automatically, by design. Services are reached by forwarding ports onto host
localhost, so parallel containers share one port space and the docs' own
example is a container's 3000 arriving as `localhost:4123`. There is no
egress policy of any kind. And the two supported ways to get Docker inside
are the `docker-in-docker` feature, which sets `"privileged": true`, or
bind-mounting the host's Docker socket — the second is host root by another
name. Desktop apps aren't
in the model at all; the nearest thing is a community `desktop-lite` feature
serving fluxbox over noVNC.

So the overlap with jailbee is smaller than the surface similarity suggests:
`devcontainer.json` declares the *tools* a repo needs, `.jailbee/config.yaml`
declares the *machine* it needs — devices, egress, what may be mounted, what
boots on create.

**[BranchBox](https://github.com/branchbox/branchbox)** (MIT, Rust) is the
closest thing to jailbee's shape: a git worktree plus a Docker Compose
project plus a generated devcontainer per feature, with a database and
optional Cloudflare tunnel. It is much lighter to adopt — Docker is already
installed, stacks are auto-detected, images come prebuilt — and it runs on
macOS and integrates with VS Code and Cursor, which jailbee does not. It
inherits the devcontainer posture above and then goes further in the same
direction: tool credentials (`~/.gh`, `~/.claude`, `~/.codex`) are mounted
read-write into every feature by design. Its per-feature setup is generated
from stack detection rather than declared in a spec the team reviews — less
to write up front, less to pin down. It solves collisions between your own
parallel workstreams. It is not built to contain something you don't trust.

### Fenced agents: nono and Docker Sandboxes

**[nono](https://github.com/nolabs-ai/nono)** (Apache-2.0, Rust) is a fence
around the agent process — no container, no VM, no disk. Its per-tool
policies are genuinely clever: the agent may call `gh`, but `gh` gets its own
filesystem grants and receives its GitHub token through a proxy that can
restrict it to `GET /repos/org/repo/issues/**`. That's finer-grained than
anything jailbee does, and its profiles are composable JSON shared through a
registry — though they describe an agent's policy, not a repo's environment.
It is also the allowlist model, with the cost curve described above, and it
can't give you a second Postgres or a second port 3000, because it isn't an
environment. nono's own security-model page says its boundary is agent
containment, "not guest/host isolation", and recommends running it inside a
container or microVM when you need that. **Running nono inside a jailbee
container is a sensible combination**, not a contradiction.

**[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)** (`docker sbx`)
is the one tool here with a *stronger* boundary than jailbee's, and it should
be said plainly: each sandbox is a microVM with its own kernel, no shared
memory or processes with the host, and its own Docker daemon inside. Egress
is deny-by-default through a host-side HTTP(S) proxy, and that proxy injects
API keys into request headers so that, in Docker's words, "credential values
never enter the VM" — strictly better than jailbee, which bind-mounts your
real GnuPG and SSH-agent sockets read-only and trusts the container boundary
to hold. If your requirement is a hypervisor between the agent and your
laptop, jailbee does not meet it and `sbx` does.

What it is not is an environment for a branch. The differences that matter:

- **It sandboxes a session, not a machine.** The unit is "run this agent in a
  box" (`sbx run claude`), and by default your workspace is mounted
  read-write at the same absolute path inside — so the agent is editing your
  host tree, and a wrecked checkout is a wrecked checkout. `--clone` flips
  that to a read-only mount plus a private in-VM clone, but there is no
  branch model on top: no base-branch tracking, no `push`/`pull`/`diff`
  bridge, no PR flow, no snapshot/restore.
- **HTTP or nothing.** Non-HTTP traffic — raw TCP, UDP, ICMP — is blocked
  outright, so an outbound `ssh`, `git+ssh`, or a database client pointed at
  a staging host has no allowlist entry to add. jailbee's ACL is
  protocol-agnostic; it allows or denies hosts, not schemes.
- **No desktop.** No Chrome and no IDE inside the boundary, which is the
  point of jailbee's GUI passthrough.
- **A Docker account, and a closed CLI.** `sbx login` is mandatory,
  the binaries are Docker's, and centrally managed policy is a paid
  add-on. jailbee is GPL-3.0 and runs entirely on your machine with no
  account and no service to sign into.
- **Cost of the stronger boundary.** Sandboxes need hardware
  virtualisation (Ubuntu 24.04+, Windows 11, or Apple-silicon macOS), and
  Docker notes that they "don't share images or layers" — every sandbox
  pays for its own VM image and image cache. jailbee's containers are
  copy-on-write clones of one golden image.
- **The spec is per-agent, not per-repo.** Kits (still marked experimental)
  layer install commands, files, network and credential rules onto a
  template image — closer to nono's agent profiles than to a committed
  environment your colleague inherits by cloning the repo.

### Where that leaves jailbee

Nothing above does these three things together, and they are the whole
argument for the heavier boundary:

1. A **long-lived environment per branch that the repo declares** — devices,
   egress, mounts, caches, and services already running when `jailbee new`
   returns.
2. A **desktop inside** it: Chrome and a JetBrains IDE against the
   container's own `localhost`, five of them at once, no port allocation.
3. The container as a **git remote** with base-branch tracking, PR flow,
   snapshots, and dashboards that span every repo on the host.

Pick Dev Containers if you want the standard, BranchBox if you want parallel
workstreams cheaply, nono if you want fine-grained policy around one agent,
and `sbx` if you want a hypervisor. jailbee is for when the runtime is the
hard part *and* you want the whole runtime fenced.

## What jailbee costs you

- **Linux only.** The macOS path runs jailbee inside a Linux VM and is
  experimental.
- **Host setup is real work.** Incus, UID delegation, firewall, kernel keyring
  limits — [Installation](installation.md) is an afternoon, once.
- **A golden image build**, ~10–15 minutes, once per repo stack. After that
  containers are copy-on-write clones and creation is fast.
- **Disk.** Cheap per container, not free.
- **JetBrains and Chrome, not VS Code.** jailbee's GUI passthrough targets the
  JetBrains IDEs; there is no devcontainer integration. Only one IDEA-family
  IDE at a time across containers (shared profile).
- **A shared kernel.** A system container is the right boundary for code you
  are supervising loosely; it is not a multi-tenant boundary against a
  determined attacker, and a kernel bug is an escape path. Incus can run real
  VMs — jailbee does not use them. If a hypervisor boundary is a hard
  requirement, [Docker Sandboxes](#fenced-agents-nono-and-docker-sandboxes)
  gives you one today and jailbee does not.
- **Secrets are mounted, not brokered.** GnuPG and the SSH agent reach the
  container as read-only binds, so anything that runs inside can use your
  keys while it runs. A credential proxy that keeps the token out of the
  environment entirely (nono, `sbx`) is the stronger design; jailbee relies on
  the egress ACL to limit where those keys can be used.
- **Every `host_devices` entry widens that surface further** — `/dev/kvm` in
  particular hands the container a host-kernel interface. List only what the
  repo needs.
- **It's young.** jailbee is developed at GISGRO for its own use. No public
  community, no package-manager release, a small maintainer team. Price that
  in.

If your stack is one process and one database, `git worktree` plus
`docker compose -p` is free and already installed. jailbee earns its cost when the
runtime is the hard part.

---

*Written by the jailbee maintainers, so read the comparison with that in mind.
Claims about the other four tools were checked by reading their source and
documentation rather than by running them: BranchBox and nono on 2026-08-06
(BranchBox 0.10.1, last commit 2026-03-20; nono pre-1.0, actively developed),
and Dev Containers and Docker Sandboxes on 2026-08-13 (`docs.docker.com/ai/sandboxes`
— architecture, security, customize and FAQ pages; kits documented as
experimental). All four move quickly. Corrections are welcome — please open an
issue.*
