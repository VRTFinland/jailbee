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

## How this differs from BranchBox and nono

Two adjacent projects come up often. Both are good; both answer a different
question.

| | **nono** | **BranchBox** | **jailbee** |
|---|---|---|---|
| Isolates | a process tree | an application container | a full userland |
| Mechanism | Landlock + seccomp (Linux), Seatbelt (macOS) | Docker Compose project on the host daemon | unprivileged Incus system container |
| Can run a nested Docker daemon, emulator, desktop browser | no | not in its model | yes |
| Environment spec | agent profile (JSON, shareable via registry) | generated `.devcontainer/` + `.env`, stack auto-detected | committed `.jailbee/config.yaml` per repo |
| Platforms | Linux, macOS, WSL2 | Linux, macOS | Linux |

**[nono](https://github.com/nolabs-ai/nono)** (Apache-2.0, Rust) is a fence
around the agent process — no container, no VM, no disk. Its per-tool
policies are genuinely clever: the agent may call `gh`, but `gh` gets its own
filesystem grants and receives its GitHub token through a proxy that can
restrict it to `GET /repos/org/repo/issues/**`. That's finer-grained than
anything jailbee does, and its profiles are composable JSON shared through a
registry — though they describe an agent's policy, not a repo's environment.
It is also the allowlist model, with the cost curve described above, and it
can't give you a second Postgres or a second port 3000, because it isn't an
environment. nono's own security-model page says
its boundary is agent containment, "not guest/host isolation", and recommends
running it inside a container or microVM when you need that. **Running nono
inside a jailbee container is a sensible combination**, not a contradiction.

**[BranchBox](https://github.com/branchbox/branchbox)** (MIT, Rust) is the
closest in spirit: a git worktree plus a Docker Compose project plus a
devcontainer per feature, with a database and optional Cloudflare tunnel. It
is lighter to adopt than jailbee — Docker is already installed, stacks are
auto-detected, images come prebuilt — and it works on macOS and integrates
with VS Code and Cursor, which jailbee does not. What it doesn't offer is the
boundary: the worktree sits on the host filesystem, features share the host's
Docker daemon, and tool credentials (`~/.gh`, `~/.claude`, `~/.codex`) are
mounted read-write into every feature by design. Its per-feature setup is
generated from stack auto-detection rather than declared in a committed
environment spec — less to write up front, less for a team to pin down. It
solves collisions between your own parallel workstreams. It is not built to
contain something you don't trust.

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
  VMs — jailbee does not use them.
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
Claims about BranchBox and nono were checked against their repositories and
documentation on 2026-08-06 (BranchBox 0.10.1, last commit 2026-03-20; nono
pre-1.0, actively developed) by reading source and docs rather than by
running them. Corrections are welcome — please open an issue.*
