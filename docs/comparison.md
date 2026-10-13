# Who JailBee is for

JailBee gives each git branch a **long-lived system container**: an
unprivileged Incus container with its own init and IP, and — if the repo
needs them — its own Docker daemon, browser and IDE on your desktop.
Several branches can each boot
the same stack, listen on `localhost:3000` and run a browser against it,
without sharing the host's Docker daemon or allocating host ports.

It is a container, not a VM. User namespaces, cgroups, AppArmor and seccomp
provide a kernel-enforced boundary, but the host kernel is shared. Devices,
mounts and host services you attach deliberately widen that boundary. If you
need a hypervisor between the workload and your laptop, JailBee is not the
answer; see [Security and limitations](security.md).

## You'll recognise yourself if…

- **You're on Linux** and you control the host.
- **The runtime is the hard part.** Your work boots services, runs a database,
  spawns an emulator, opens a browser or shells out to `docker`.
- **You want several of those running at once** — a feature, a review, an
  agent experiment — without renaming Compose projects or hunting for ports.
- **You leave coding agents running unattended**, but want their own checkout,
  bounded host access and a snapshot to restore when something goes wrong.

## Choose by the problem you have

- **Dev Containers:** a standard, portable toolchain the team can adopt with
  little ceremony. This is the first option most teams should try.
- **BranchBox:** parallel worktrees and Compose stacks, with stack detection
  and editor integration doing much of the setup.
- **nono:** fine-grained filesystem and network policy around an agent and
  its tools, without provisioning another machine.
- **Docker Sandboxes:** a microVM boundary with a separate kernel and
  brokered API credentials around a supported agent.
- **JailBee:** a persistent runtime per branch, with its own Docker,
  localhost, desktop apps and git transport back to the host. You accept
  Linux-only host setup and a shared kernel in return.

## How this differs from the alternatives

The table compares **built-in or documented support**, not everything you
could build on top of each tool. **✅** supported, **🟡** supported with the
qualification in the cell, **❌** no built-in or documented support found,
**n/a** outside that tool's role. Defaults and opt-in configurations are
named explicitly. A ❌ is not a claim that a custom integration is impossible.
The [source note](#sources-and-scope) records the versions and scope checked.

| | **Dev Containers** | **BranchBox** | **nono** | **Docker Sandboxes** | **JailBee** |
|---|---|---|---|---|---|
| **What it is** | a toolchain in a container | a worktree + Compose per feature | a fence around one process | a microVM for an agent | a Linux environment per branch |
| **Boundary** | Docker container; host daemon by default | Docker container by default; opt-in microVM runtimes | Landlock + seccomp, Seatbelt | hypervisor, own kernel | Incus container, shared kernel |
| Run the repo's `docker-compose.yml` | ✅ | ✅ | n/a — does not provision a runtime | ✅ | ✅ |
| …without using the host's Docker daemon for the repo's stack | 🟡 privileged `docker-in-docker` | 🟡 opt-in VM runtimes | n/a | ✅ | ✅ |
| Separate runtimes both listening on `:3000` | 🟡 different host ports when forwarded | 🟡 a host port range per feature | n/a — no separate network stack | ✅ | ✅ no forwarding for an in-container browser |
| Run an emulator or a VM (`/dev/kvm`) | 🟡 explicit device passthrough | ❌ | n/a — host device permissions still apply | ❌ documented device support is an experimental NVIDIA GPU | ✅ `host_devices` |
| Browser and IDE **inside** the boundary, as native desktop windows | ❌ community noVNC desktop instead | ❌ | n/a — they run on the host | ❌ | ✅ Wayland passthrough |
| Built-in egress policy | ❌ | 🟡 `local-vm` blocks host and private networks | ✅ per tool, including HTTP method and path | ✅ hostname rules for TCP, HTTP method and path | ✅ NIC ACL plus HTTP(S) hostname proxy; explicit host connections are exceptions |
| Stage agent's GitHub writes for human publication | ❌ | ❌ | 🟡 token proxy can block writes; no outbox | 🟡 method/path rules can block writes; no outbox | 🟡 outbox; requires a correctly scoped read-only container token |
| Workspace separate from the host's main checkout | 🟡 opt-in clone into a volume | 🟡 separate worktree; shared git repository | 🟡 path grants, not a separate checkout | 🟡 opt-in `--clone` | 🟡 clone by default; `--mount` shares the host checkout |
| Built-in host ↔ environment git bridge | ❌ no bridge for a volume clone | n/a — worktrees share the repository | n/a | ❌ no bridge for a `--clone` copy | ✅ `jailbee git push/pull/diff` |
| Snapshot and restore the current environment | ❌ rebuild instead | ❌ | 🟡 filesystem rollback in macOS supervised mode | 🟡 `sbx save` creates a template, not in-place rollback | ✅ |
| Separate guest kernel | ❌ | 🟡 opt-in VM runtimes | ❌ | ✅ | ❌ |
| macOS or Windows host support | ✅ | 🟡 macOS | 🟡 Windows via WSL2 | ✅ hardware virtualisation required | ❌ Linux host required |
| Repo-carried environment specification | ✅ `devcontainer.json` | 🟡 generated from stack detection | n/a — agent policy profiles | 🟡 agent/team kits | ✅ `.jailbee/config.yaml` |
| Licence / distribution | open spec (MIT); VS Code's extension is Microsoft's | MIT | Apache-2.0 | free CLI, Docker sign-in required, governance is paid | GPL-3.0-or-later |

### Toolchain per branch: Dev Containers and BranchBox

**[Dev Containers](https://containers.dev/)** wins on ecosystem and adoption:
a committed `.devcontainer/devcontainer.json`, a Features marketplace,
prebuilt images, three host OSes and implementations beyond VS Code
(GitHub Codespaces, JetBrains, DevPod). If you need everyone to have the same
Node and `psql`, start here.

Its usual local workflow is not designed to contain an unattended agent.
The workspace is bind-mounted read-write and credentials are forwarded;
"Clone Repository in Container Volume" is the separate-checkout alternative.
Services are normally reached through host port forwarding. There is no
built-in egress policy. The standard Docker Features offer either privileged
Docker-in-Docker or the host Docker socket, which gives access to the host
daemon. A community `desktop-lite` feature provides a desktop over noVNC,
not native windows on your compositor.

You can extend this setup with networking, device mappings and custom
security controls. JailBee's difference is that it integrates those concerns
with a branch lifecycle, rather than asking the team to assemble them.

**[BranchBox](https://github.com/branchbox/branchbox)** (MIT, Rust) combines a
git worktree, a Compose project and a generated devcontainer per feature.
Stack detection, prebuilt images, macOS support, VS Code/Cursor integration
and optional Cloudflare tunnels make it lighter to adopt. A worktree keeps
feature files separate, but shares the underlying git repository rather than
being an independent clone.

The default Docker runtime mounts tool credentials (`~/.gh`, `~/.claude`,
`~/.codex`, `~/.cloudflared`) read-write into each feature. Since 0.12,
opt-in Docker Sandboxes and an experimental Firecracker `local-vm` runtime
also provide VM boundaries. The latter blocks host/private networks and
mounts neither the Docker socket nor credential directories. These VM
boundaries are stronger than JailBee's; the default remains host Docker.
JailBee adds a native in-container desktop, declared devices, an independent
clone with a git bridge, and in-place snapshots.

### Fenced agents: nono and Docker Sandboxes

**[nono](https://github.com/nolabs-ai/nono)** (Apache-2.0, Rust) applies policy
to an agent process and its tools, without a container or VM. A tool such as
`gh` can receive its own filesystem grants and use a token proxy that filters
API requests by method and path. On macOS, supervised mode also offers
filesystem rollback snapshots. These are finer-grained controls than
JailBee provides; composable JSON profiles can be shared through a registry.

It does not provision a second runtime or network stack. nono's own security
model calls itself "intentionally a different model from a guest/host
isolation boundary" and recommends a container or microVM perimeter with
nono inside it. **nono inside JailBee is a sensible combination** when you
want both a separate runtime and tool-specific policy.

**[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)** (`docker sbx`)
gives each sandbox its own kernel and Docker daemon. Outbound TCP goes
through a deny-by-default host-side proxy, with hostname rules for TCP ports,
HTTP method/path rules and experimental UDP. Supported API credentials are
injected into request headers instead of entering the VM. For those tokens,
this is stronger than making a readable token file available inside an
environment. If a hypervisor boundary is required, `sbx` meets that
requirement and JailBee does not.

The distinction is the workflow around the boundary:

- **Persistent branch lifecycle.** JailBee boots the repo's services and
  keeps the environment for subsequent human or agent sessions. `sbx` is
  organised around supported agents (`sbx run claude`); saved templates can
  start new sandboxes, but are not in-place rollback snapshots.
- **Independent checkout and transport.** `sbx` mounts the host workspace
  read-write by default; `--clone` creates a private copy. JailBee clones by
  default, tracks the base branch and provides a host/container git bridge,
  PR flow and a review outbox. Its opt-in `--mount` mode instead shares the
  host checkout read-write. Its destroy checks warn about work at risk.
- **Native desktop and host integration.** JailBee can attach devices and
  host-agent sockets, and run Chrome or a JetBrains IDE on your Wayland
  session. These connections are also security trade-offs. VMs can broker
  host services too, but `sbx` documents no equivalent compositor,
  smartcard or adb integration; its documented device passthrough is an
  experimental NVIDIA GPU.
- **Configuration and distribution.** JailBee carries the environment spec
  in the repo and runs locally without a JailBee account. `sbx` kits package
  workload/mixin configuration; Docker sign-in is mandatory and centrally
  managed policy is paid.
- **Storage model.** JailBee clones one golden image copy-on-write. Docker
  documents that sandboxes do not share images or layers. That favours
  JailBee for many similar environments, but actual disk and memory costs
  depend on the stack and storage backend.

Both can run a Docker stack. Choose `sbx` when the stronger kernel boundary
is decisive; choose JailBee when branch lifecycle, native desktop apps and
explicit host integration are the work you would otherwise build yourself.

### Two more you may be thinking of

Neither has been checked in the same depth as the four above:

- **[container-use](https://github.com/dagger/container-use)** (Dagger,
  Apache-2.0) gives each agent a container plus a git worktree over MCP,
  driven from the agent's side.
- **[microsandbox](https://github.com/superradcompany/microsandbox)**
  (Apache-2.0) runs OCI images in local libkrun microVMs without a daemon or
  root, behind SDKs.

One that is not an alternative at all, although it is often named in the
same breath: **[herdr](https://github.com/herdrdev/herdr)** (Apache-2.0,
Rust) is a terminal multiplexer for coding agents — a background server
keeps each agent's terminal alive, a sidebar shows whether it is working,
blocked or idle, and a socket API lets agents open panes and wait on each
other. Its panes are plain host processes: it has no isolation of its own
(its docs point at nono for that), no per-branch environment and no git
model. It sits in the layer JailBee's dashboard and in-container tmux
occupy, and it will run inside a JailBee container like any other terminal
program.

### Where that leaves JailBee

The argument for JailBee is the combination, not a claim to the strongest
sandbox or the broadest ecosystem:

1. A **long-lived runtime per branch**, declared by the repo, with its own
   Docker daemon and services already running after configured autostart.
2. A **desktop inside it**, against its own `localhost`, without host port
   allocation for each branch's browser.
3. An **independent clone and git bridge**, with snapshots, PR workflows and
   dashboards across repos.
4. **Explicit host integration** for devices, mounts and services, accepting
   the wider attack surface each connection brings.

## The idea: isolation by boundary, not by allowlist

A process sandbox grants access to selected paths, hosts and tools. This is
valuable when you need fine-grained policy, and per-tool grants can avoid
widening permissions for everything else. But a changing development stack
may require repeated policy work: a new service, browser or emulator can
need a different set of filesystem, process and network permissions.

JailBee instead provides a separate Linux userland and network stack.
Installing another ordinary tool inside it does not require a new
host-filesystem grant. Egress destinations, host devices and mounts still
need explicit configuration — the boundary does not make those free.

| You want to… | Inside a JailBee container |
|---|---|
| Run the repo's Compose stack | Its own Docker daemon runs the stack without changing Compose project names or remapping ports just to coexist with another branch. |
| Run an emulator or a KVM VM | Declare `host_devices: [{ path: /dev/kvm }]`; install and configure the emulator inside. |
| Use a phone plugged into the host | Declare `host_ports: [{ name: adb, port: 5037 }]` to reach the host adb server. [Recipe](project-config.md#talking-to-android-devices-over-adb). |
| Test in a real browser | `jailbee chrome <name>` / `jailbee firefox <name>` runs a browser inside, rendered on your Wayland session, against the container's own `localhost:3000`. |
| Edit with a JetBrains IDE | `jailbee ide <name>` uses the same display passthrough. |
| Run services or install tools | Use systemd and the container's package manager. |

### And the agent is still fenced in

- **Egress policy.** In strict mode, a kernel ACL filters traffic through the
  container's network interface, including SSH and database connections.
  JailBee resolves allowed hostnames and pins `/etc/hosts` to the allowed
  addresses. On the work network, a Squid proxy additionally checks HTTP(S)
  hostnames for tools that honour the proxy variables, including wildcard
  rules. TLS is not intercepted; direct traffic is checked by the IP ACL,
  which cannot distinguish hosts sharing an allowed IP. Declared port
  forwards and attached host sockets are deliberate exceptions, not traffic
  covered by this ACL. See [Egress proxy](security.md#egress-proxy) and
  [Port forwards](security.md#port-forwards) for the limits.
- **GitHub review gate.** With a correctly scoped read-only container token,
  agents stage GitHub changes in an outbox; you review and publish with the
  host's `gh`. JailBee cannot verify that token's effective permissions. A
  write-capable token bypasses the gate. `github.com` is absent from the
  default strict allowlist, but that is not a substitute for token scope.
- **Host access is explicit.** Built-in GnuPG/SSH credential mounts and
  gitconfig are read-only. **A read-only agent socket can still sign and
  authenticate:** it keeps key material on the host, not authority out of
  the container. Hardware-key touch requirements depend on your key's
  configuration. Wayland attaches when a window is first opened; D-Bus and
  PulseAudio are opt-in. Shared caches and agent settings are writable
  shared state, so snapshots are not a rollback of everything reachable.
- **Own clone and snapshots.** In the default clone mode, the agent edits
  the container's working tree, not your main checkout. Snapshot before a
  run and restore the container afterwards; writable host mounts remain
  outside that protection. `jailbee new --mount` shares the host checkout
  read-write instead, so it does not provide this separation.

Running an agent without its in-process sandbox is a trade to make after
reviewing the effective mounts, devices, credentials and network policy,
not a guarantee supplied by strict mode alone. See
[Running an agent without prompts](security.md#running-an-agent-without-prompts).

### Whichever agent you run

Any terminal agent can run inside. `agents:` presets wire installation,
mounts, egress and autostart for `codex`, `gemini`, `aider`, `opencode` and
`grok`; Claude Code is the production-tested setup, and the others are
starting points. [Generic agent support](agents.md) covers custom setups.

Claude logins can be shared across containers and credential groups;
`jailbee account` manages stored logins. For other providers' models,
`claude-jb` routes Claude Code through JailBee's separate LiteLLM container.
Provider credentials stay there; dev containers receive a proxy key that
can still spend the account. See [Claude Code through LiteLLM](litellm.md).

## The repo decides how its containers run

The committed `.jailbee/config.yaml` declares stacks and provisioning,
autostart services, devices, egress, shared caches and GUI apps. Host-level
`global.yaml` supplies personal defaults; host-local `repos/<prefix>.yaml`
can override a repo's settings without committing tokens or machine-specific
choices. The effective environment depends on all those layers.

`host_mounts` are **read-write by default**; set `readonly: true` explicitly
for anything sensitive. `optional_mounts` remain detached until requested
and can detach after a TTL (`jailbee mount --for 2h`, 15 minutes by default).
`host_ports` connects declared host services to container localhost.

`jailbee config edit` edits the layers and `jailbee apply` updates profiles,
mounts and network settings. Changes to the golden image's installed stacks
or provisioning require a rebuild. See [Configuration reference](config.md).

## Getting code in and out

In clone mode, each container is a git remote.
`jailbee git push/pull/fetch/diff` transfers commits locally rather than
through GitHub, including submodule objects.
Base-branch tracking supports review and stacked work; `jailbee git merge`
and `jailbee fork` let containers exchange or branch from committed work.

`jailbee new --pr <N>` creates a review environment, and `jailbee pr` opens
or updates a draft PR. Before destroying a container, JailBee checks dirty
files, submodule changes and commits held nowhere else, and requires extra
confirmation if work is at risk. See [Git bridge](git-bridge.md).

## Driving it

Bare `jailbee` opens the terminal dashboard; `jailbee gui` opens the Qt
version. Both span repos and show git status, CPU use and Claude Code's
busy/waiting/idle state, with actions on selected containers. CLI commands
prompt for omitted required values on an interactive terminal, and
`jailbee console` provides completion and history.

Optional SSH access offers a restricted dashboard, console, commands and
SFTP. Remote GUI uses `waypipe ssh` from Linux or a shared RDP display from
other clients. See [Remote GUI over SSH](remote-gui.md).

## What JailBee costs you

- **Linux host required.** macOS and Windows can be remote clients; running
  JailBee in a Linux VM on a Mac is experimental. See
  [Using JailBee from a Mac](macos.md).
- **Host setup and image builds.** Incus, UID delegation, firewall and kernel
  limits take real setup work. Budget an afternoon for
  [Installation](installation.md), then roughly 10–15 minutes for an initial
  golden image per stack. Some upgrades require a rebuild and `apply`.
- **Disk and shared state.** Copy-on-write makes similar containers cheap,
  not free. Shared caches, agent settings and credential groups also mean
  not all damage is contained to one branch or reversed by its snapshot.
- **GUI integration has limits.** JetBrains, Chrome and Firefox have built-in
  launchers; other apps need an `apps:` entry and an installed binary. There
  is no devcontainer editor integration. Only one IDEA-family IDE can run
  at a time across containers with the shared profile; browsers have
  per-container profiles.
- **A shared kernel.** A kernel vulnerability is an escape path. This is not
  a hostile multi-tenant boundary. Incus supports VMs, but JailBee does not
  use them; [Docker Sandboxes](#fenced-agents-nono-and-docker-sandboxes) and
  BranchBox's opt-in VM runtimes provide a separate guest kernel.
- **Readable tokens and usable sockets.** Mounted API tokens can be read by
  code inside. Host-agent sockets keep private keys outside, but permit
  signing/authentication requests. A credential proxy is stronger for
  supported API tokens. LiteLLM keeps provider credentials outside the dev
  container, but its proxy key still allows account usage.
- **Host integration widens the boundary.** `/dev/kvm`, other devices,
  writable mounts, display sockets and forwarded host services each expose
  something outside the private runtime. Attach only what the repo needs.
- **Most agent presets are untested.** Claude Code is used daily; other agent
  presets may need correction. Claude Code through LiteLLM is the tested
  path for other vendors' models.
- **A young project and a small team.** JailBee is developed at GISGRO for
  its own use, released publicly at 1.0.0 in August 2026 and available on
  PyPI. There is no established community yet. Price that in.

If a worktree plus `docker compose -p` is enough, use it. JailBee earns its
cost when the runtime is the hard part and you want the whole runtime
fenced, with the host connections chosen deliberately.

## Sources and scope

*Written by the JailBee maintainers, so read the comparison with that in mind.
Claims about the other four tools were checked by reading their source and
documentation rather than by running them, last on 2026-10-02: BranchBox
0.13.4 (last commit 2026-09-29), nono 0.79.0 (still pre-1.0), Docker Sandboxes
`sbx` 0.46.0 (`docs.docker.com/ai/sandboxes` — architecture, security, policy,
install, usage, customize, FAQ and release notes), and the Dev Containers
specification and VS Code documentation. All four move quickly — between the
August check and this one, BranchBox gained VM runtimes and `sbx` gained
non-HTTP egress and saved templates. Corrections are welcome — please open an
issue.*
