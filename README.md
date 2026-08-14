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
  touch). Mount any other host socket the same way — an adb server, a
  database — and use it from inside.
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
  `jailbee`, and `jailbee pr` writes the PR title and body. Start it
  automatically in a tmux window and the container is ready for an
  unattended run the moment it boots.
- **Fast, cheap containers** — copy-on-write clones of one golden image; a live
  TUI dashboard (`jailbee dashboard`) or Qt GUI dashboard (`jailbee gui`) spans every repo.

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
one-time job with a few moving parts. Follow **[Installation](docs/installation.md)**
end-to-end first. Then, from the repo you want to manage:

```bash
jailbee config init          # write .jailbee/config.yaml
jailbee doctor               # sanity-check host + config
jailbee init                 # create Incus profiles, ACL, bridge
jailbee base build           # build the golden image (one-time, ~10–15 min)
jailbee new feat/my-branch   # spin up an isolated env for a branch
```

See **[Getting started](docs/getting-started.md)** for the full first-run
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
| [Installation](docs/installation.md) | One-time host setup: Incus, UID delegation, installing the CLI (plus conditional firewall / kernel-keyring steps) |
| [Getting started](docs/getting-started.md) | Concepts, configure a repo, build the image, and a "typical day" walkthrough |
| [Running on macOS](docs/macos.md) | Using JailBee from an Apple Silicon Mac via a Linux VM (Colima/Lima) with the repo shared from macOS (experimental) |

**Daily use** — working with containers:

| Doc | What's inside |
|---|---|
| [Commands](docs/commands.md) | Full command + flag reference table |
| [Git bridge and branch workflows](docs/git-bridge.md) | Host↔container git bridge, stacked PRs, mount vs clone, PR review, `gh` inside containers |
| [Setting up JailBee in your own project](docs/project-config.md) | Tutorial for adapting JailBee to your own repo and stack |
| [Troubleshooting](docs/troubleshooting.md) | Common failures by symptom, and how to remove JailBee |

**Reference** — the details:

| Doc | What's inside |
|---|---|
| [Configuration reference](docs/config.md) | Every `.jailbee/config.yaml` and `global.yaml` key |
| [Security and limitations](docs/security.md) | Isolation model, git-remote handling, known limits |
| [Architecture](docs/architecture.md) | How the pieces fit together |
| [Who JailBee is for](docs/comparison.md) | What JailBee is good at, what it costs, and how it differs from Dev Containers, BranchBox, nono and Docker Sandboxes |

**Meta** — project internals:

| Doc | What's inside |
|---|---|
| [Manual testing](docs/manual-testing.md) | End-to-end smoke-test recipes (require a real Incus daemon) |
| [Releasing](docs/releasing.md) | Release process |
| [Contributing](CONTRIBUTING.md) | Development setup and repo conventions |

## License

`jailbee` is free software, released under the GNU General Public License v3.0
or later (GPL-3.0-or-later). See [`LICENSE`](LICENSE) for the full text.

Copyright © 2026 GISGRO Oy.
