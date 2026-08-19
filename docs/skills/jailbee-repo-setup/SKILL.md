---
name: jailbee-repo-setup
description: Use when configuring a new repository to work with `jailbee` — adding `.jailbee/config.yaml`, optional `install.d/` snippets, and `container_prefix`/host-mounts/egress/autostart adjustments. Trigger on phrases like "set up jailbee", "configure jailbee", "make this repo jailbee-compatible", "jailbee config", "set up gie", "configure gie", "make this repo gie-compatible", "gie config", "lisää gie-konfiguraatio" (`gie` is jailbee's deprecated pre-1.0 command alias), or whenever the user wants to run `jailbee new`/`jailbee shell` against a repo that doesn't yet have `.jailbee/config.yaml`. The skill inspects the repo's stack (package.json, pyproject.toml, pom.xml, Cargo.toml, Makefile, docker-compose, …) and generates a tailored config rather than the defaults-only template `jailbee config init` ships.
---

# JailBee repo setup

Goal: take any existing git repo and make `jailbee new <name>` work for it. The repo needs at minimum `<repo>/.jailbee/config.yaml`. Beyond that, the configuration is **repo-specific** — stack versions, system packages, autostart commands, and egress endpoints all depend on what the repo actually does.

This skill walks Claude through inspecting the repo, generating a tailored config, and (optionally) adding `install.d/` snippets for stack tools the bundled golden image doesn't cover.

`gie` is the pre-1.0 name of this tool. It still works as a **deprecated**
console-script alias (removed in 1.1.0), but don't teach it — use `jailbee`
in every command you write or suggest.

## Workflow

1. **Confirm JailBee is installed.** `jailbee --version` should work. If not, the user installs it with `uv tool install jailbee` or `pipx install jailbee`.
2. **Confirm cwd is a git repo.** `jailbee` derives `repo_root` from the directory containing `.jailbee/`. If `.git/` is missing or the repo dir name has uppercase/underscores/dots, note it — `container_prefix` will need an explicit override.
3. **Inspect the stack.** Read the manifest files (see [Stack detection](#stack-detection) below). Note the language, build tool, runtime versions, and any services declared (databases, Redis, etc.) in `docker-compose*.yml`.
4. **Generate the starter file.** Run `jailbee config init` (or `uv tool run jailbee config init` if `jailbee` isn't on PATH yet). This writes a fully-defaulted `.jailbee/config.yaml`. **Don't** hand-write the file from scratch — the template's comments are the user-facing schema documentation.
5. **Tailor the generated file.** Edit only the fields that need to change for this repo. See [Common edits per stack](#common-edits-per-stack). Leave the rest at default.
6. **Add `install.d/` snippets only if needed.** The golden image is stack-neutral by default — the always-on `install.d/` snippets only cover locale, prompt, GUI libs, and GitHub CLI. Java, Node, Python, Docker, and the ECR/registry-mirror helpers are bundled but off; turn them on with `golden.stacks` (see [Common edits per stack](#common-edits-per-stack)) rather than writing a new snippet for them. Only add a repo-level snippet for tooling that isn't already bundled and doesn't fit `golden.extra_apt_packages`. See [When to add install.d snippets](#when-to-add-installd-snippets).
7. **Validate.** Run `jailbee config validate`. Fix any errors. Then run `jailbee doctor` for a host-level sanity check (incus running, bridges, subuid mapping, etc.) — these aren't config errors but block `jailbee init`.
8. **Tell the user the next step.** Almost always: `jailbee init && jailbee base build && jailbee new <name>`. Don't run these yourself unless asked — `jailbee base build` takes 10–15 minutes and `jailbee init` mutates host-level Incus state.

## Stack detection

Walk the repo root and look at top-level manifest files. Don't recurse — sub-projects' manifests are usually irrelevant for the golden-image decision.

| File present | Implies | Maps to config |
|---|---|---|
| `pyproject.toml` / `requirements*.txt` / `setup.py` | Python | `golden.python` is deprecated/ignored (the image's `python3` tracks `golden.ubuntu_version`); add `python3.X` via `golden.extra_apt_packages` if a specific interpreter is needed, and `golden.stacks.python: true` for the venv/pip bootstrap. |
| `package.json` | Node | `golden.stacks.node: <major>` (24, 22, 20 — check `engines.node` for the pin), or `true` for the default major. |
| `pom.xml` / `build.gradle*` | Java | `golden.stacks.java: "corretto-<N>"` (e.g. `corretto-21`, `corretto-17`) or `"openjdk-<N>"` for a stock-archive JDK instead of Corretto. |
| `Cargo.toml` | Rust | No first-class support — add `cargo` via `golden.extra_apt_packages: [cargo]` or write an `install.d/` snippet that installs rustup. |
| `go.mod` | Go | No first-class support — `golden.extra_apt_packages: [golang-go]` or an `install.d/` snippet for a specific Go version. |
| `Gemfile` | Ruby | `golden.extra_apt_packages: [ruby-full]` |
| `docker-compose*.yml` | Containerized services | Don't add to JailBee config — the dev user runs compose inside the JailBee container directly. But: if compose references custom registries (e.g. `*.dkr.ecr.<region>.amazonaws.com`), add them to `docker_registry_mirror.extra_registries`; if it drives Docker itself, add `golden.stacks.docker: true`. |
| `Makefile` | Conventional build entrypoints | Useful for autostart: targets like `make dev-env`, `make run` are good `autostart.on_create`/`on_start` candidates. |
| `.nvmrc`, `.node-version`, `.tool-versions` | Version pins | Set `golden.stacks.node` to the pinned major. |

If multiple stacks are present (e.g. Java backend + Node frontend, common in this org), keep `golden.stacks.java` and `golden.stacks.node` both set — they don't conflict.

If the repo has none of these and is just docs/scripts, the defaulted config is fine; the user probably just wants an isolated shell.

## Common edits per stack

### `container_prefix` — required for non-conforming dir names

`jailbee` defaults this to the repo directory name. It must match `[a-z0-9][a-z0-9-]*`. If the dir is `MyRepo`, `my_repo`, or `repo.thing`, set it explicitly:

```yaml
container_prefix: myrepo
```

Don't add this key if the directory name already matches — the default is clearer.

### `host_mounts` — credentials the repo's tooling needs

Personal credentials (`~/.gnupg`, `~/.gitconfig`, JetBrains Toolbox, the host Chrome install) usually live in `~/.config/jailbee/global.yaml`, not the per-repo file. `~/.gnupg`, the Toolbox dir, and `/opt/google/chrome` are also added automatically by the `gpg` / `jetbrains.toolbox_host_path` / `chrome.host_path` auto-mounts when those blocks are enabled — only list them manually if you want to override readonly/source. The per-repo file is for repo-specific mounts:

```yaml
host_mounts:
  - { host: ~/.aws,    container: /home/dev/.aws,    readonly: true }   # if repo uses AWS APIs at build time
  - { host: ~/.docker, container: /home/dev/.docker, readonly: true }   # if you need host docker auth
```

Android repos, adb over a **unix socket**: if the host's adb server listens on
a unix socket rather than its default TCP port, mount that socket
**read-write** and point the client at it (a read-only socket cannot be
talked on) — this is the one adb setup `host_ports` below cannot cover,
since the config schema only forwards TCP/UDP. The host must run its adb
server on that socket (`adb -L localfilesystem:$HOME/.android/adb.sock
start-server`):

```yaml
host_mounts:
  - { host: ~/.android/adb.sock, container: /home/dev/.adb.sock, readonly: false }

container:
  env:
    ADB_SERVER_SOCKET: "localfilesystem:/home/dev/.adb.sock"
```

For an emulator inside the container instead, add `host_devices: [{ path: /dev/kvm }]`.

For mounts only some containers need (e.g. AWS for ECR pulls during a specific autostart step), use `optional_mounts` and reference them from the step:

```yaml
optional_mounts:
  aws:
    host: ~/.aws
    container: /home/dev/.aws
    readonly: true
    description: "AWS creds for ECR pulls"

autostart:
  on_create:
    - name: docker-login
      run: "aws ecr get-login-password | docker login --password-stdin ..."
      mounts: [aws]
```

### `host_ports` — forwarding a host TCP/UDP service into every container

For a host service the container needs to reach over TCP/UDP — the adb
server, a database, a media daemon, a device bridge — `host_ports` is
usually simpler than a socket mount:

```yaml
host_ports:
  - { name: adb, port: 5037 }
```

Full schema, one entry per forward:

| Key | Required | Default | Notes |
|---|---|---|---|
| `name` | yes | — | `^[a-z0-9][a-z0-9-]*$`, max 40 chars, unique. Becomes the Incus device name and the `jailbee port rm <name>` key. |
| `port` | yes | — | 1..65535. The **container**-side port — the container listens here. |
| `host_port` | no | same as `port` | The host-side port Incus connects to. |
| `proto` | no | `tcp` | `tcp` or `udp`. |
| `host_address` | no | `127.0.0.1` | IP literal only — a hostname is rejected (it would have to be resolved once, at device-add time, and silently pinned). |
| `container_address` | no | `127.0.0.1` | Same restriction. |

Only the **to-container** direction (a host service reachable inside the
container) is configurable here — a host-side listener is a machine-wide
resource, so a repo declaring one in `host_ports` would make every branch
container of that repo fight over the same host port, breaking the "many
containers coexist" property the whole tool is built on. Config rejects
`direction`/`to_host`/`bind` keys with that explanation. The mirror
(a container service reachable on the host) is `jailbee port to-host`,
run per container — see the jailbee-usage skill.

Offer `host_ports` when the repo's stack suggests one: an Android project
(the adb example above — the host's default adb server already listens on
`127.0.0.1:5037`, so no `ADB_SERVER_SOCKET` override is needed once this
forward exists), or any stack where the container needs a host-run service —
a database, a media daemon, a device bridge — over TCP/UDP. It doesn't
replace the `host_mounts` adb-socket recipe above for a host adb server that
only ever speaks over a unix socket; `host_ports` can't forward those.

Entries are attached when `jailbee new` creates a container and kept in sync
by `jailbee apply` (added/replaced/removed to match the file) — no image
rebuild, no container restart, on either command.

### `golden.extra_apt_packages` — repo-level system deps

Anything the bundled `install.sh` doesn't install but the repo's tooling expects at the OS level. Package names are validated against Debian grammar (`[a-z0-9][a-z0-9+\-.]*`).

```yaml
golden:
  extra_apt_packages:
    - postgresql-client
    - imagemagick
```

Don't list dev libraries that pip/npm/cargo will fetch themselves — only system binaries and `-dev` packages that build steps need.

### `egress_allow` — strict-mode egress

The default network mode is `strict`, which blocks everything except what's listed here (plus DNS to the Incus bridge). What to include depends on what the repo's dev workflow hits:

- `api.anthropic.com:443` — Claude API (usually lives in global config)
- `archive.ubuntu.com:443`, `security.ubuntu.com:443` — apt updates inside the container
- `registry.npmjs.org:443`, `pypi.org:443`, `files.pythonhosted.org:443` — package installers
- The repo's own dependency hosts (Sonatype Nexus, GitHub Packages, internal registries)

**Do NOT auto-add `github.com`.** That's a deliberate design choice — strict mode keeps `git push` blocked so unattended agents can't surprise-push. The user switches to loose mode (`jailbee net loose <name>`) when they actually want to push or fetch.

If the user installs deps with `pnpm/uv/cargo` at *autostart* time (not at runtime), the autostart step can switch network per-step:

```yaml
autostart:
  on_create:
    - name: install-deps
      run: "uv sync"
      network: loose          # only for this step
    - name: build
      run: "make build"       # back to strict (defaults)
```

### `golden.stacks` — pin the version and stage the runtime in one field

Read the manifest files (`engines.node` in package.json, `.tool-versions`, `pom.xml` `maven.compiler.source`, etc.) and set `golden.stacks` accordingly — one key both pins the version and stages the matching snippet (no separate `enable_snippets` entry needed):

```yaml
golden:
  stacks:
    java: corretto-21   # if pom.xml uses Java 21 ("openjdk-21" for a stock-archive JDK instead)
    node: 22             # if .nvmrc says 22
```

Don't pin a version the repo doesn't actually require — bumping the golden image rebuild is a 10–15 min cost.

`golden.python` is deprecated and ignored — the container's Python is always the base image's system `python3` (a function of `golden.ubuntu_version`). Need a different interpreter? Add the apt package via `golden.extra_apt_packages` (e.g. `python3.12`), and set `golden.stacks.python: true` if you also want the venv/pip bootstrap from `40-python.sh`.

`golden.enable_snippets` + a bare `golden.java`/`golden.node` version pin remain available directly as the low-level path when `stacks` doesn't cover what you need — see [Stacks](references/config-schema.md#stacks-goldenstacks) in the config schema reference.

### `autostart.on_create` / `on_start` — what runs when a container appears

`on_create` runs once when `jailbee new <name>` provisions the container. `on_start` runs every time the container goes stopped→running (including on the *initial* `jailbee new`, after `on_create`).

Rule of thumb:
- **`on_create`**: one-time setup. `uv sync`, `pnpm install`, `make dev-env`, DB initialisation. Often needs `network: loose` because strict-mode `egress_allow` rarely includes every package mirror.
- **`on_start`**: recurring launches. Backend dev server, frontend hot-reload, watchers. Usually `background: true` so the step finishes immediately and the process keeps running in a detached tmux window.

A typical pattern:

```yaml
jetbrains:
  autostart: true       # opt in to IDE auto-launch (default false)
chrome:
  autostart: true       # opt in to Chrome auto-launch (default false)

autostart:
  step_timeout: 600
  on_create:
    - name: install-deps
      run: "pnpm install"
      network: loose
  on_start:
    - name: dev
      run: "pnpm dev"
      working_dir: frontend
      background: true
```

> `jetbrains.autostart` / `chrome.autostart` only fire when the corresponding **`enabled`** master switch is true. Both `jetbrains.enabled` and `chrome.enabled` default to **`false`** — the user normally turns them on in `~/.config/jailbee/global.yaml` (the `jailbee config init --global` template does this). If the repo-level config above doesn't seem to launch the IDE or browser, check that the master switch is on.

Inspect the repo's `package.json` scripts / `Makefile` targets / `README` quickstart section to figure out the right commands. Don't invent commands — if the repo's `README` says `make run`, use exactly that.

Once this block is committed, it is not just the local default: `jailbee new <branch>` (clone mode) reads `autostart` from **whatever branch it clones**, at that branch's own commit — not from whoever runs the command. Every other key in this file stays under the operator's control regardless of branch. A step a branch adds with `network: loose` prompts the operator for confirmation before the container is created; see [Configuration](../../config.md#where-does-the-autostart-config-come-from) for the exact diff format.

### `defaults.memory` / `defaults.cpu` — match repo size

Default is 16GiB / 8 CPU. For a small Python-only repo, 4GiB / 4 CPU is plenty. For a JVM monorepo with Gradle and several services, 16–32GiB is appropriate. Don't oversize — Incus enforces the limit and OOM-kills will surface.

### `jetbrains.ide` — default IDE for `jailbee ide` / autostart

Map by stack:
- `idea` — generic JVM/Kotlin (default)
- `pycharm` — Python-heavy
- `webstorm` — frontend-only
- `goland` / `clion` / `phpstorm` / `rider` / `rubymine` / `datagrip` / `rustrover` / `aqua` / `dataspell` — others

`jetbrains.ide` has no `null`. If the user lives in a non-JetBrains editor and never runs `jailbee ide`, the cleanest off-switch is `jetbrains.enabled: false` (the default — leaves it alone if global.yaml hasn't flipped it on). `jetbrains.autostart: false` is the right knob when the master switch is on but auto-launch is unwanted.

### `docker_registry_mirror.extra_registries` — non-default registries

Only relevant if the repo's compose/Dockerfile pulls from registries outside the rpardini default set (Docker Hub, registry.k8s.io, gcr.io, quay.io, ghcr.io). AWS ECR is the common one:

```yaml
docker_registry_mirror:
  extra_registries:
    - 803520778560.dkr.ecr.eu-north-1.amazonaws.com
```

Entries must be bare `host[:port]` — no scheme, no path.

## When to add install.d snippets

The bundled `install.d/` snippets cover the common cases. **Reach for a custom snippet only when** `golden.extra_apt_packages` isn't enough — i.e. the install needs more than an apt package:

- Curl-bash installers (rustup, uv, mise, asdf, language version managers)
- Multi-step compiles (e.g. building a specific OpenJDK variant)
- Pre-seeding caches or files into the image

Snippets live at one of three tiers, resolved by filename (repo > user > bundled):

| Path | Tier | Use when |
|---|---|---|
| `<repo>/.jailbee/install.d/*.sh` | repo | Stack tooling specific to this repo |
| `~/.config/jailbee/install.d/*.sh` | user | Personal tooling you want in every container (e.g. shell config) |
| Bundled (in the JailBee wheel) | base | Don't touch directly |

### Writing a snippet

Snippets run as root inside the golden-build container during `jailbee base build`. They're plain bash with a header convention:

```bash
#!/bin/bash
# 55-rustup — install rustup as the dev user
# Env: CONTAINER_USER, JAILBEE_USER_HOME
# Installs: ~/.cargo/bin/cargo, rustc
set -euo pipefail

su - "${CONTAINER_USER}" -c '
  curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
'
```

Naming: prefix with a two-digit number that places it among the built-in snippets. Occupied slots: the always-on set `05/10/15/60/75` plus the opt-in `install.d.available/` set `20/30/40/50/80/90` (any of which may be enabled via `golden.enable_snippets`). Pick a free slot:
- `55-` between docker (50) and gui-libs (60)
- `35-` between node (30) and python (40)
- `95-` after everything else

Make sure the file is executable: `chmod +x .jailbee/install.d/55-rustup.sh`.

### Disabling a bundled snippet

If the repo doesn't need, say, the JetBrains/Chrome GUI libs installed in the container, drop one of:

1. Config (recommended — visible in `jailbee config show`):
   ```yaml
   golden:
     disable_snippets:
       - "60-gui-libs"
   ```
2. Empty shadow: `: > .jailbee/install.d/60-gui-libs.sh` (same filename, zero bytes — `install.sh` skips empty files).

### Snippet environment

Snippets get these env vars automatically:

| Variable | Value |
|---|---|
| `CONTAINER_USER` | always `dev` |
| `CONTAINER_UID` / `CONTAINER_GID` | from `container_user.{uid,gid}` |
| `JAVA_PACKAGE` | apt name derived from `golden.java` |
| `NODE_MAJOR` | from `golden.node` |
| `EXTRA_APT_PACKAGES` | whitespace-joined `golden.extra_apt_packages` |
| `JAILBEE_USER_HOME` | `/home/dev` |
| `JAILBEE_PROVISION_DIR` | `/provision` |

Custom vars go in `golden.provision_env`. Reserved names (the table above minus `JAILBEE_*`) are rejected.

## Validation and finishing touches

After editing, run:

```bash
jailbee config validate         # config-only checks (schema + cross-field)
jailbee config show             # print merged effective config (sanity check)
jailbee doctor                  # host-level (incus running, bridges, subuid, …)
```

`jailbee config validate` will reject:
- Unknown YAML keys (the schema is fail-closed)
- Bad `container_prefix` (regex mismatch)
- Invalid `egress_allow` entries
- Reserved `provision_env` keys
- Duplicate autostart step names within a trigger
- Non-existent `optional_mounts` referenced from a step
- Bad `host_ports` entries — name regex/length, an out-of-range port, a
  non-IP `host_address`/`container_address`, or a `direction`/`to_host`/
  `bind` key (only the to-container direction is configurable)

`jailbee doctor` is host-level — failures there mean the user needs to fix host setup (see the JailBee README) before `jailbee init` works. They're not the skill's responsibility, but flag them so the user knows.

## What this skill does NOT do

- **Don't run `jailbee init` / `jailbee base build` / `jailbee new` automatically.** These mutate host-level Incus state and the golden-image build is slow. Show the user the commands and let them run.
- **Don't invent autostart commands.** Read the repo's README / Makefile / package.json scripts and use exactly what's documented.
- **Don't add `github.com` to `egress_allow`.** That's a security boundary by design.
- **Don't put personal credentials in the per-repo file.** `~/.gnupg`, `~/.gitconfig`, JetBrains Toolbox, and the host Chrome install belong in `~/.config/jailbee/global.yaml`. The per-repo file is committed to git and shared with the team.
- **Don't enable host-tooling blocks in the per-repo file unless the repo really requires it.** `gpg`, `ssh`, `jetbrains`, `chrome` all default to `enabled: false`; users opt in via `~/.config/jailbee/global.yaml`. If a repo absolutely needs (e.g.) JetBrains tooling for everyone, then a per-repo `jetbrains.enabled: true` is fine — otherwise leave the master switch to the user's global config.

## Inside a JailBee container

You may be reading this from **inside** a JailBee container (repo clone at
`~/<container_prefix>`, no `jailbee` binary on `PATH`). You can still help with
configuration: edit `.jailbee/config.yaml` and `install.d/` snippets directly in the
clone. You **cannot** run `jailbee config validate` / `jailbee apply` / `jailbee new` here —
those run on the host. After editing the config, tell the host operator to run
`jailbee config validate` then `jailbee apply` (or recreate the container) to apply the
change.

## Further reference

If you hit a field this document doesn't cover, see [`references/config-schema.md`](references/config-schema.md) for the full field reference (every key, every default, every validation rule). It's a verbatim distillation of `docs/config.md` from the JailBee repo.

Once the repo is configured and the user wants to *use* `jailbee` day-to-day (creating/entering containers, the host↔container git bridge, network modes, the dashboard, reviewing PRs, …), that's the **jailbee-usage** skill's domain — point there rather than reproducing command guidance here.
