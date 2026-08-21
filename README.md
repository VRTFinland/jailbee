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

**JailBee** runs isolated, per-branch development environments in
[Incus](https://linuxcontainers.org/incus/) system containers. Spin up multiple
full stacks in parallel on one host — each with its own services, Docker daemon,
IDE, and browser — without port conflicts, Docker name clashes, or
shared-database collisions.

The CLI is `jailbee`, or `jb` for short.

**JailBee** is project-agnostic: every repo supplies its own `.jailbee/config.yaml`. The
golden image ships stack-neutral by default — language toolchains (JDK, Node,
Python venv/pip, Docker) are bundled but opt-in, enabled per repo via
`golden.stacks` / `golden.enable_snippets`. It was built at GISGRO, which is
its origin, not its scope.

## Key features

- **Per-branch isolation** — one full-stack container per git branch, running
  in parallel without port or Docker-name collisions.
- **Host↔container git bridge** — the container acts as a git remote; move
  commits with `jailbee git push`/`pull`/`checkout` instead of round-tripping
  through GitHub.
- **Submodules that travel** — sub-repos are initialised offline on
  `jailbee new` and their objects move with the superproject on every
  push/pull, so a repo with submodules needs no manual setup on either side.
- **Nested Docker** — `security.nesting=true` out of the box on Ubuntu 26.04.
- **GUI passthrough** — launch a JetBrains IDE (`jailbee ide`) and Chrome
  (`jailbee chrome`) from inside a container onto your Wayland session.
- **Host sockets, shared** — Wayland, PulseAudio, D-Bus and the gpg-agent are
  attached to every container, so `git commit -S` and `ssh` work inside while
  the private key never leaves the host (a smartcard still asks for its
  touch). Mount any other host socket the same way and use it from inside.
- **Host services, forwarded in** — declare
  `host_ports: [{ name: adb, port: 5037 }]` and every container of the repo
  reaches that host service on its own localhost, so plain `adb devices` works
  inside with no `ADB_SERVER_SOCKET` juggling. `jailbee port` adds or removes a
  forward on one container without touching the config, in either direction —
  `to-host` for the rarer case where you do want a container's service on the
  host.
- **Network modes** — per-container egress allowlist with `strict` and
  `loose` policies (`jailbee net`), safe for unattended agent runs. Entries are
  hostnames and ports (`api.example.com:443`), not IP addresses: JailBee
  resolves them into the kernel ACL, keeps a cumulative pool as CDN
  addresses rotate, and pins the container's `/etc/hosts` to match. Any
  protocol, not just HTTP — `ssh`, `git+ssh` and a database client work
  under the same list.
- **First-class Claude Code** — opt in with `claude.enabled: true` and every
  container gets Claude Code installed, sharing one login and one settings
  directory across the repo's containers while your host `~/.claude` is
  never read. The Anthropic hosts are added to the strict-mode allowlist
  automatically, JailBee's own skills teach the in-container Claude to drive
  `jailbee`, and `jailbee pr` writes the PR title and body — to your repo's own
  standard, if you state one in `claude.pr_prompt`. Start it
  automatically in a tmux window and the container is ready for an
  unattended run the moment it boots — with permission prompts turned off
  (`--dangerously-skip-permissions`), because the boundary is the container
  rather than the agent's own judgement. You size that boundary once in the
  repo's config; see
  [Running an agent without prompts](https://github.com/VRTFinland/jailbee/blob/main/docs/security.md#running-an-agent-without-prompts)
  for what it does and doesn't cover.
- **Generic agent support** — `agents: {codex: {enabled: true}}` wires any
  terminal coding agent into the same mount/egress/install/autostart
  pipeline Claude Code uses, via a shipped preset or one you write yourself.
  Five presets beyond Claude (`codex`, `gemini`, `aider`, `opencode`, `grok`)
  ship as untested starting points — see
  [Generic agent support](https://github.com/VRTFinland/jailbee/blob/main/docs/agents.md).
- **One shared state layer per repo** — package-manager caches, the JetBrains
  config, the Chrome profile pool, `~/.ssh` and Claude's login live in a shared
  dir bind-mounted into every one of the repo's containers. Branches running in
  parallel draw on one warm Gradle or pnpm cache and one set of tool settings
  rather than building each from scratch, and the state outlives
  `jailbee destroy` / `jailbee new` — while nothing a container does reaches
  your host's own dotfiles.
- **Fast, cheap containers** — copy-on-write clones of one golden image; a live
  TUI dashboard (`jailbee dashboard`) or Qt GUI dashboard (`jailbee gui`) spans
  every repo, and acts on what it shows: attach a shell or tmux, open the IDE,
  create or update the PR, update a container from its base, read its diff —
  without leaving the view that told you it was needed.

## Getting started

**JailBee** needs a Linux host running Incus. Install the CLI with
[`uv`](https://docs.astral.sh/uv/) or [`pipx`](https://pipx.pypa.io/) —
JailBee is an ordinary PyPI package and needs neither at runtime, but
Ubuntu 24.04+ refuses a bare `pip install` into its system Python:

```bash
uv tool install jailbee      # or: pipx install jailbee
```

For the optional Qt GUI dashboard (`jailbee gui`), add the `gui` extra:

```bash
uv tool install 'jailbee[gui]'      # or: pipx install 'jailbee[gui]'
```

Host setup — Incus, firewall, UID mapping, kernel keyring limits — is a
one-time job with a few moving parts. Follow **[Installation](https://github.com/VRTFinland/jailbee/blob/main/docs/installation.md)**
end-to-end first. Then, from the repo you want to manage:

```bash
jailbee config init          # write .jailbee/config.yaml
jailbee doctor               # sanity-check host + config
jailbee init                 # create Incus profiles, ACL, bridge
jailbee base build           # build the golden image (one-time, ~10–15 min)
jailbee new feat/my-branch   # spin up an isolated env for a branch
```

See **[Getting started](https://github.com/VRTFinland/jailbee/blob/main/docs/getting-started.md)** for the full first-run
walkthrough.

## Shell completion

Install Typer's completion script once per shell:

```bash
jailbee --install-completion
```

Restart the shell, and TAB completes commands, options, and:

- **container names** on every command that takes one (`jailbee shell`, `jailbee destroy`,
  `jailbee git push`, `jailbee ide`, …) — short names, from the containers that exist in
  the current repo
- **branch names** on `jailbee new` and `jailbee retarget`, from the host repo's local branches
- **snapshot tags** on `jailbee snapshot restore` and `jailbee snapshot delete`, from the
  container already named on the command line
- **fixed values** for `--format`, `--layer`, `--attach` and `--user`

Completion looks for `.jailbee/config.yaml` in the current directory, the same
default the commands themselves use; elsewhere it offers nothing. Unlike the
commands, it does not honor `--config`/`-c`, so e.g. `jailbee shell -c
/other/repo/.jailbee/config.yaml <TAB>` still completes against the *current
directory's* containers, not the repo the flag points at.

## Documentation

**Setup** — get **JailBee** running:

| Doc | What's inside |
|---|---|
| [Installation](https://github.com/VRTFinland/jailbee/blob/main/docs/installation.md) | One-time host setup: Incus, UID delegation, installing the CLI (plus conditional firewall / kernel-keyring steps) |
| [Getting started](https://github.com/VRTFinland/jailbee/blob/main/docs/getting-started.md) | Concepts, configure a repo, build the image, and a "typical day" walkthrough |
| [Running on macOS](https://github.com/VRTFinland/jailbee/blob/main/docs/macos.md) | Using JailBee from an Apple Silicon Mac via a Linux VM (Colima/Lima) with the repo shared from macOS (experimental) |

**Daily use** — working with containers:

| Doc | What's inside |
|---|---|
| [FAQ](https://github.com/VRTFinland/jailbee/blob/main/docs/faq.md) | Short answers to the common questions, each linking to the page that covers it in full |
| [Commands](https://github.com/VRTFinland/jailbee/blob/main/docs/commands.md) | Full command + flag reference table |
| [Git bridge and branch workflows](https://github.com/VRTFinland/jailbee/blob/main/docs/git-bridge.md) | Host↔container git bridge, stacked PRs, mount vs clone, PR review, `gh` inside containers |
| [Setting up JailBee in your own project](https://github.com/VRTFinland/jailbee/blob/main/docs/project-config.md) | Tutorial for adapting JailBee to your own repo and stack |
| [Troubleshooting](https://github.com/VRTFinland/jailbee/blob/main/docs/troubleshooting.md) | Common failures by symptom, and how to remove JailBee |

**Reference** — the details:

| Doc | What's inside |
|---|---|
| [Configuration reference](https://github.com/VRTFinland/jailbee/blob/main/docs/config.md) | Every `.jailbee/config.yaml` and `global.yaml` key |
| [Generic agent support](https://github.com/VRTFinland/jailbee/blob/main/docs/agents.md) | Wiring a terminal coding agent (Claude Code or otherwise) into the container lifecycle; the shipped presets and their verification status |
| [Security and limitations](https://github.com/VRTFinland/jailbee/blob/main/docs/security.md) | Isolation model, git-remote handling, known limits |
| [Architecture](https://github.com/VRTFinland/jailbee/blob/main/docs/architecture.md) | How the pieces fit together |
| [Who JailBee is for](https://github.com/VRTFinland/jailbee/blob/main/docs/comparison.md) | What JailBee is good at, what it costs, and how it differs from Dev Containers, BranchBox, nono and Docker Sandboxes |

**Meta** — project internals:

| Doc | What's inside |
|---|---|
| [Manual testing](https://github.com/VRTFinland/jailbee/blob/main/docs/manual-testing.md) | End-to-end smoke-test recipes (require a real Incus daemon) |
| [Releasing](https://github.com/VRTFinland/jailbee/blob/main/docs/releasing.md) | Release process |
| [Contributing](https://github.com/VRTFinland/jailbee/blob/main/CONTRIBUTING.md) | Development setup and repo conventions |

## License

`jailbee` is free software, released under the GNU General Public License v3.0
or later (GPL-3.0-or-later). See [`LICENSE`](https://github.com/VRTFinland/jailbee/blob/main/LICENSE) for the full text.

Copyright © 2026 GISGRO Oy.
