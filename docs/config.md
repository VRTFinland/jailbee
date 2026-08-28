# Configuration reference

`jailbee` reads two configuration files:

1. **Per-repo:** `<repo>/.jailbee/config.yaml` — required.
2. **Global:** `~/.config/jailbee/global.yaml` — optional, host-level only.

Run `jailbee config init` in a repo to generate a per-repo template.

## Configuration layers

`jailbee` loads configuration from two YAML files and deep-merges them:

| Layer | Path | Purpose |
|---|---|---|
| Global (user-level) | `~/.config/jailbee/global.yaml` (or `$XDG_CONFIG_HOME/jailbee/global.yaml`) | Personal defaults applied to every repo (mounts, IDE preference, common egress endpoints). |
| Repo | `<repo>/.jailbee/config.yaml` | Per-repo configuration (stack versions, autostart, repo-specific egress, resources). |

Repo-level values overlay user-level values. The effective `Config` Python object passed to every `jailbee` command is the merged result.

### Merge rules

| Source type | Rule | How to reset |
|---|---|---|
| Scalar (`str`, `int`, `bool`, `Path`, enum) | Repo value replaces user value | `null` in repo clears |
| List | Repo list appended to user list | `[]` in repo replaces with empty list |
| Map / dict | Recursive deep-merge per key | No bulk reset — set an individual key to `null` to clear it (an empty `{}` is a no-op) |

Example: a user-level `host_mounts` entry plus a repo-level one yields two mounts after merge. A repo that needs to *exclude* a user mount must `host_mounts: []` and re-list everything it wants.

Three keys are exempt from this pipeline — see [Keys that bypass the deep-merge pipeline](#keys-that-bypass-the-deep-merge-pipeline).

### Recommended placement

| Field | Layer | Why |
|---|---|---|
| `container_user.uid` / `container_user.gid` | global | Constant per developer across repos |
| `host_mounts` (gitconfig) | global | Personal credentials |
| `gpg.enabled` | global | Personal — depends on whether host has gpg-agent. Default `false`. |
| `ssh.*` | global | Personal — depends on host SSH config. `ssh.enabled` default `false`. |
| `jetbrains.enabled` | global | Personal — turn on if you use a JetBrains IDE. Default `false`. |
| `jetbrains.userprefs_from_host` | global | Personal license state (off by default — opt in only to share host JBA login) |
| `jetbrains.ai_enabled` | global | Personal — turn on if you use JetBrains AI Assistant |
| `jetbrains.toolbox_host_path` | global | Personal Toolbox install path |
| `chrome.enabled` | global | Personal — turn on if you want `jailbee chrome` / auto-launch. Default `false`. |
| `chrome.dark_mode` | global | Personal preference |
| `chrome.host_path` | global | Personal Chrome install path (default `/opt/google/chrome`) |
| `ls` (column preference) | global | Which columns `jailbee ls` shows is personal; see [`ls:`](#ls--dashboard--remembered-columns). `dashboard:` is deprecated — the dashboards keep their own view state instead, not a config block at either layer. |
| `egress_allow` (Claude API, JetBrains license hosts) | global | Cross-cutting, repo appends |
| `optional_mounts` (personal `~/.m2`, `~/.aws`) | global | Personal opt-in caches |
| `defaults.{memory,cpu,...}` | repo | Repo size determines limits |
| `golden.{java,node,ubuntu_version}` | repo | Stack-specific (`golden.python` is deprecated — see below) |
| `golden.stacks` | repo | Repo's runtime/tool set — see [Stacks](#stacks-goldenstacks) |
| `golden.extra_apt_packages` | repo | Repo's system-level deps |
| `jetbrains.ide` | repo | Repo's stack determines the IDE |
| `jetbrains.autostart` | repo | Repo decides whether autostart launches IDE |
| `jetbrains.share_idea` | repo | Repo decides whether to shadow VCS-tracked `.idea/*` with a per-repo shared mount |
| `chrome.url` | repo | Repo's app URL |
| `chrome.autostart` | repo | Repo's autostart workflow |
| `autostart.on_create`, `autostart.on_start` | repo | Repo-specific runtime workflow |
| `container.env` | repo | Repo-specific runtime env (`NODE_OPTIONS`, app feature flags, …) |
| `shared_dir` | repo only (auto-derived) | Setting globally forces all repos to share the same dir |

All fields are technically legal at either layer. The table above is convention.

### Host-level `docker_registry_mirror`

The key `docker_registry_mirror` is ambiguous: `GlobalConfig` (host-level) uses it with the shape `{port, enabled, image, data_dir}`, while per-repo `Config.docker_registry_mirror` uses it with `{extra_registries}`. To disambiguate:

- In `~/.config/jailbee/global.yaml`, `docker_registry_mirror` is **always** interpreted as host-level (`GlobalConfig`). It is not merged into the Config layer.
- In `<repo>/.jailbee/config.yaml`, `docker_registry_mirror` is interpreted as the per-repo `Config.docker_registry_mirror` override (with `extra_registries`).

If you need per-user defaults for `extra_registries`, set them per-repo. There is no global default for that field.

### Keys that bypass the deep-merge pipeline

Four top-level keys are read from `~/.config/jailbee/global.yaml` into
`GlobalConfig` and are **not** merged into the Config layer:
`docker_registry_mirror` (see above), `ls`, `dashboard` and
`claude_credentials`. `ls`'s column block is merged field-by-field instead
(repo block over global block) — the generic pipeline would *append* its
`fields`/`hide` lists and concatenate the two layers' column lists rather
than let one replace the other. `dashboard` is deprecated and is never
merged this way — see
[`ls:`/`dashboard:`](#ls--dashboard--remembered-columns).
`claude_credentials` is resolved to the single computed field
`Config.claude_credentials_dir` instead of being merged at all — see
[`claude_credentials`](#claude_credentials) below.

One consequence: `jailbee config show` prints the *Config* layer, so the `ls:` /
`dashboard:` values it shows come from the repo file only. Use `jailbee config
show --layer global` to see what the global file contributes.

### Inspecting the layers

- `jailbee config show --layer global` — print the raw user-level YAML.
- `jailbee config show --layer repo` — print the raw repo YAML.
- `jailbee config show` (or `--layer effective`) — print the merged result.

### Initialising both layers

- `jailbee config init` — write `<cwd>/.jailbee/config.yaml` (the repo config).
- `jailbee config init --global` — write `~/.config/jailbee/global.yaml` (the user config).
- `--force` overwrites an existing file in either case.

## Provisioning snippets (`install.d/`)

The golden image is provisioned by `src/jailbee/provision/install.sh`, which performs LXC/Incus plumbing (user creation, sudoers, SSH_AUTH_SOCK passthrough, bind-mount parents, linger) and then runs every executable in `/provision/install.d/*.sh` in lexical order.

It also **masks Ubuntu's automatic apt machinery** in the image —
`apt-daily{,-upgrade}.timer`, their services, and `unattended-upgrades`.
A background upgrade in a branch container takes the dpkg lock out from
under your own `apt-get`, and one still running at shutdown can block
systemd long enough for a stop to time out. Containers get their updates
from a rebuilt golden image (`jailbee base build`) instead; if you need the
timers back in a particular repo, `systemctl unmask` them from an
`install.d/` snippet.

### Resolution order

The bundled snippet set is split into two libraries, both shipped in the
wheel:

- **`install.d/`** — always on. Stack-neutral plumbing only: locale,
  prompt, GUI libs, GitHub CLI, extra apt packages. No language runtime
  or cloud helper lives here.
- **`install.d.available/`** — opt-in. Language runtimes and cloud
  helpers; staged via `golden.stacks` (recommended — see
  [Stacks](#stacks-goldenstacks) below) or, at the low level, by naming
  the snippet directly in `golden.enable_snippets` (see
  [Opt-in snippets](#opt-in-snippets-installdavailable) below).

| Source | Path | Precedence |
|---|---|---|
| Bundled (always-on) | `src/jailbee/provision/install.d/*.sh` (shipped in the wheel) | lowest |
| Bundled (opt-in, if enabled) | `src/jailbee/provision/install.d.available/*.sh` (shipped in the wheel; staged via `golden.stacks` or `golden.enable_snippets`) | above always-on, below user/repo |
| User | `~/.config/jailbee/install.d/*.sh` | overrides bundled by filename |
| Repo | `<repo>/.jailbee/install.d/*.sh` | overrides user (and bundled) by filename |

The effective set is computed at `jailbee base build`. New filenames from user or repo are simply added; same-filename files at a higher tier **replace** the lower-tier file. After resolution, names listed in `golden.disable_snippets` are dropped (suffix `.sh` is optional in the list — both `"75-github-cli"` and `"75-github-cli.sh"` work); `disable_snippets` wins over `enable_snippets` if the same name appears in both.

At runtime inside the container, `install.sh` skips any empty (zero-byte) snippet file. Combined with same-name shadowing, this gives a low-effort disable: drop an empty `<repo>/.jailbee/install.d/<name>.sh` and that snippet won't run in this repo's golden image.

### Bundled snippets (`install.d/` — always on)

| Name | Installs | Reads env |
|---|---|---|
| `05-extra-apt.sh` | Packages from `golden.extra_apt_packages` | `EXTRA_APT_PACKAGES` |
| `10-locale.sh` | `en_US.UTF-8` locale | — |
| `15-prompt.sh` | Bash prompt branch indicator (`$JAILBEE_BRANCH`) | `CONTAINER_USER` |
| `60-gui-libs.sh` | JetBrains/Chrome runtime libs + fonts | — |
| `75-github-cli.sh` | GitHub CLI (`gh`) from `cli.github.com` | — |

### Stacks (`golden.stacks`)

The recommended way to turn on a language runtime or cloud helper.
Each key expands to the matching `install.d.available/` snippet(s),
the shared caches it needs, and the `JAVA_PACKAGE`/`NODE_MAJOR`
build-env values — one field instead of an `enable_snippets` entry
plus a manual `shared_caches` list.

| Key | Type | Values | Effect |
|---|---|---|---|
| `java` | bool \| string | `false` (default) \| `true` \| `"openjdk-N"` \| `"corretto-N"` | `true` or `"openjdk-N"` stage `20-openjdk` (apt `default-jdk` or `openjdk-N-jdk`); `"corretto-N"` stages `20-corretto` (apt `java-N-amazon-corretto-jdk`). Either form adds the `gradle`/`m2` shared caches. |
| `node` | bool \| int | `false` (default) \| `true` \| `N` | Stages `30-nodejs`; `NODE_MAJOR` is `N`, or `24` when `true`. Adds the `npm`/`pnpm-store` shared caches. |
| `python` | bool | `false` (default) \| `true` | Stages `40-python`. |
| `docker` | bool | `false` (default) \| `true` | Stages `50-docker`. |
| `ecr` | bool | `false` (default) \| `true` | Stages `80-ecr-helper`. |

`java` and `docker` together also auto-stage `90-registry-mirror-ca`
(it imports the Docker registry mirror's CA into the JDK truststore).
Opt out with `golden.disable_snippets: ["90-registry-mirror-ca"]` if this
repo doesn't use the registry mirror.

Stack-derived snippet names are unioned with `golden.enable_snippets`
(duplicates deduped); stack-derived shared caches are unioned with
`shared_caches` the same way the `claude`/`jetbrains` auto-adds are — a
manual entry with a matching `name` suppresses the auto-add.

Full stack, in one line:

```yaml
golden:
  stacks: { java: corretto-17, node: 24, python: true, docker: true, ecr: true }
```

`golden.enable_snippets`/`disable_snippets`/`shared_caches` (and the
version-pin fields `golden.java`/`golden.node`) remain available
directly — they're the low-level escape hatch for anything `stacks`
doesn't cover. See [Opt-in snippets](#opt-in-snippets-installdavailable)
below.

### Opt-in snippets (`install.d.available/`)

**Low-level escape hatch.** `golden.stacks` (above) is the recommended
way to enable these; reach for `enable_snippets` directly only when
`stacks` doesn't cover what you need. Bundled but off by default;
stage a snippet by adding its logical name
(or full filename) to `golden.enable_snippets`:

```yaml
golden:
  enable_snippets: [nodejs, docker]
```

| Name | Logical name (for `enable_snippets`) | Installs | Reads env |
|---|---|---|---|
| `20-openjdk.sh` | `openjdk` | OpenJDK from the Ubuntu archive | `JAVA_PACKAGE` |
| `20-corretto.sh` | `corretto` | Amazon Corretto JDK | `JAVA_PACKAGE` |
| `30-nodejs.sh` | `nodejs` | Node.js + per-user `~/.npmrc` | `NODE_MAJOR`, `JAILBEE_USER_HOME`, `CONTAINER_USER` |
| `40-python.sh` | `python` | `python${PYTHON_VERSION}` + venv + pip | `PYTHON_VERSION` (no auto-source — set via `provision_env`; `golden.python` is deprecated and does *not* feed it) |
| `50-docker.sh` | `docker` | Docker Engine + AppArmor systemd override; adds dev user to docker group | `CONTAINER_USER` |
| `80-ecr-helper.sh` | `ecr-helper` | `amazon-ecr-credential-helper` | — |
| `90-registry-mirror-ca.sh` | `registry-mirror-ca` | Imports `/opt/jailbee-mirror-ca.crt` into the Java truststore (no-op if absent) | — |

`registry-mirror-ca` needs a JDK enabled too (it uses `keytool` — either
`openjdk` or `corretto`); the Docker registry mirror needs `docker`
enabled. `golden.stacks` auto-stages `registry-mirror-ca` whenever both
`java` and `docker` are on (see [Stacks](#stacks-goldenstacks) above).
Unknown names in `enable_snippets` are ignored with a warning at
`jailbee base build` time.

### Snippet contract

Every snippet runs as root inside the golden-build container. The header convention is:

```bash
#!/bin/bash
# <name> — <short description>
# Env: <env vars consumed>
# Installs: <what gets added to the image>
set -euo pipefail
# ...
```

Guaranteed environment variables (set by `jailbee base build` regardless of which snippet you author):

| Variable | Source | Notes |
|---|---|---|
| `CONTAINER_USER` | hardcoded `dev` | The unix username inside the container. |
| `CONTAINER_UID`, `CONTAINER_GID` | `container_user.{uid,gid}` | Match host uid/gid for bind-mount readability. |
| `JAVA_PACKAGE` | `golden.stacks.java` (preferred) or `golden.java` (mapped) | apt package name (e.g. `java-17-amazon-corretto-jdk`). |
| `NODE_MAJOR` | `golden.stacks.node` (preferred) or `golden.node` | Major version for NodeSource (e.g. `24`). |
| `EXTRA_APT_PACKAGES` | `golden.extra_apt_packages` | Whitespace-separated; may be empty. |
| `JAILBEE_USER_HOME` | `/home/dev` (constant) | Lets snippets avoid hardcoding the path. |
| `JAILBEE_PROVISION_DIR` | `/provision` (constant) | Where snippets are staged inside the container. |

Custom env vars can be passed via `golden.provision_env`. All of the above
except `CONTAINER_USER` are reserved — passing one via `provision_env`
raises `ConfigError`. `CONTAINER_USER` is always `dev`.

### Disabling bundled snippets

Two ways:

1. **Config:** add to `golden.disable_snippets` (recommended for documentation reasons).

   ```yaml
   golden:
     disable_snippets:
       - "60-gui-libs"
       - "80-ecr-helper"
   ```

2. **Empty shadow:** drop an empty file at the same path under `<repo>/.jailbee/install.d/` (or `~/.config/jailbee/install.d/`).

   ```bash
   mkdir -p .jailbee/install.d
   : > .jailbee/install.d/60-gui-libs.sh
   ```

The config approach makes the choice visible in `jailbee config show`; the shadow approach is useful when you want to inspect the disable state via the filesystem.

Note: `90-registry-mirror-ca.sh` depends on `keytool` from the JDK installed by `20-corretto.sh`/`20-openjdk.sh`. If you disable whichever JDK snippet your `golden.stacks.java`/`golden.enable_snippets` staged, also disable `90-registry-mirror-ca.sh` (or `keytool` won't be on PATH and the snippet will fail).

### Full provisioning override

`golden.provision_script` (path, relative to repo root) replaces the bundled `install.sh` entirely. When set, `install.d/` snippets are **not** staged — the custom script owns the whole provisioning surface. This escape hatch exists for repos that need to do something fundamentally different (e.g. a non-Ubuntu base image).

## Per-repo config (`.jailbee/config.yaml`)

All keys are optional. An empty file (`{}`) is valid and yields full
defaults. The schema is fail-closed — unknown keys are rejected.

### `container_user`

UID/GID of the user account inside the container. The unix username is
**hardcoded** to `dev`. It used to be configurable, but nothing enforced
consistency between that value and the user baked into the golden image,
and a mismatch surfaced as a confusing Permission-denied error.

| Key | Type | Default | Description |
|---|---|---|---|
| `uid` | int | current host uid | UID inside container |
| `gid` | int | current host gid | GID inside container |

### `container`

Container-wide settings applied via the Incus base profile.

| Key | Type | Default | Description |
|---|---|---|---|
| `env` | map | `{}` | Env vars injected into every process Incus starts in the container — `jailbee shell`, `jailbee tmux`, autostart steps, and any nested tmux/shell. Values are passed through verbatim (no shell expansion). Keys must match `[A-Za-z_][A-Za-z0-9_]*`. |

`container.env` is ambient: it applies to interactive shells (`jailbee shell`),
the autostart tmux session (`jailbee tmux`), and every autostart step. Per-step
overrides live under `autostart.env` (every step) or
`autostart.on_{create,start}[*].env` (one step), and win on conflict because
they are passed as `tmux new-window -e`.

User entries also win over JailBee's own GUI/SSH defaults
(`DISPLAY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, `SSH_AUTH_SOCK`) — set them
only if you know why you want to.

Profile changes take effect after `jailbee apply` (which prompts to restart
running containers).

Example:
```yaml
container:
  env:
    NODE_OPTIONS: "--max-old-space-size=4096"
    MYAPP_FEATURE_FLAG: "1"
```

### `shared_dir`

Host directory that holds JailBee's shared state: shared caches (pnpm,
gradle, npm, m2), JetBrains config/data, Chrome pool slots, and Claude
state. It is the host-side *root* for these — each entry is bind-mounted
individually at its own in-container path (e.g. `~/.claude`, and the
cache paths), not under a single `/mnt/shared` mount point.

| Default | `~/.local/share/jailbee/shared/<container_prefix>` |

A `.owner` stamp file is written here on first `jailbee init`. Two repos
with conflicting `shared_dir` paths will fail loudly on the second
`jailbee init`.

### `share_local`

| Key | Type | Default | Description |
|---|---|---|---|
| `share_local` | bool | `true` | When `true` and a directory `<repo_root>/.local` exists, RW-bind-mount it into each new container at `~/<container_prefix>/.local` as a host<->container file-transfer channel. Presence-triggered: an absent `.local` dir is a silent skip and is never auto-created. Skipped in `--mount` mode (the full-repo RW bind already exposes it). Set `false` to disable entirely. |

### `host_mounts`

List of bind mounts added to every container.

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | path | — | Host path (`~` expanded). |
| `container` | string | — | In-container mount target. |
| `readonly` | bool | `false` | **Read-write unless set to `true`** — set `true` for anything sensitive. |

### `optional_mounts`

Named mounts, attached per-container with `jailbee new --mount NAME`.

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | path | — | Host path (`~` expanded). |
| `container` | string | — | In-container mount target. |
| `readonly` | bool | `true` | Read-only by default. |
| `description` | string | `""` | Shown in the `jailbee new --mount` picker. |

### `host_devices`

Pass arbitrary host character/block devices into every container as Incus
`unix-char` / `unix-block` devices. Opt-in, default empty.

```yaml
host_devices:
  - { path: /dev/kvm }                        # Android emulator / KVM VMs
  # - { path: /dev/net/tun }
  # - { source: /dev/bus/usb/001/004, path: /dev/bus/usb/001/004 }
```

| Field | Meaning | Default |
|-------|---------|---------|
| `path` | device path **inside** the container; absolute | required |
| `source` | host device path; absolute | defaults to `path` |
| `type` | `unix-char` or `unix-block` | `unix-char` |
| `mode` | node mode on the Incus profile device (octal string) | `"0666"` |
| `gid` / `uid` | node owner on the Incus profile device | unset |
| `group` | container group the `dev` user is added to for access | auto |

Layered like `host_mounts`: per-repo entries append to global ones; `[]` resets.
A device whose host `source` is absent is **skipped** (JailBee does not fail), and
`jailbee config validate` reports it as an advisory — so a team-shared config still
works on hosts that lack the device.

**How the `dev` user gets access (`group`, not `mode`).** The reliable access
mechanism is **group membership**, not the profile `mode`. Many host devices
(`/dev/kvm`, `/dev/net/tun`, `/dev/fuse`, …) carry a udev `static_node` rule, so
the container's own `systemd-udevd` resets the node to its distro default
(e.g. `/dev/kvm` → `root:kvm 0660`) on every boot — overriding whatever `mode`
the Incus profile set. (Verified: neither the profile `mode` nor an in-container
udev override survives this; only group membership does.) So JailBee adds the `dev`
user to the device's owning group:

- When `group` is **unset** (default), JailBee auto-derives it from the host source
  node's owning group — `/dev/kvm` → `kvm`. Zero-config: `{ path: /dev/kvm }`
  just works.
- Set `group` explicitly to override (e.g. when the host and container group
  names differ): `{ path: /dev/kvm, group: kvm }`.
- `mode`/`gid`/`uid` still apply to the Incus profile device and remain useful
  for devices **without** a `static_node` rule (e.g. the GPU render nodes,
  which keep `0666`).

> **Group membership takes effect on the next session.** JailBee runs the group-add
> on `jailbee new` and on `jailbee apply` (for running containers). A new `jailbee shell` /
> `jailbee tmux` / autostart session picks the group up via `incus exec --user`'s
> supplementary-group setup; an **already-open** shell must be reopened. Check
> with `id` inside the container — the device's group (e.g. `kvm`) should appear.

**Security.** JailBee containers are unprivileged (userns + `raw.idmap`), so
`/dev/kvm` does not grant container escape, host root, or new filesystem access on
its own. It does **widen the host-kernel attack surface**: KVM ioctls run in
host-kernel context, so a process that can open `/dev/kvm` could in principle
exploit a KVM kernel bug to escalate past the userns isolation. For a single-user
dev box where the host user already runs VMs this is the same trust boundary they
already extend to their own account. Treat every `host_devices` entry as
attack-surface-widening and list only what the repo's workflow needs.

### `host_ports`

Make a host service reachable **inside** every container of the repo — the
classic case is an adb server: with the forward in place, plain `adb devices`
works inside the container, and no `ADB_SERVER_SOCKET` juggling is needed,
because the host's adb server already listens on `127.0.0.1:5037` by default.

```yaml
host_ports:
  - { name: adb, port: 5037 }
```

Each entry becomes one Incus `proxy` device (named `port-cfg-<name>`): the
container listens on `container_address:port`, and Incus's forkproxy connects
to `host_address:host_port` on the host whenever something inside the
container connects to that listener. So `port`/`container_address` name the
container-side listener, and `host_port`/`host_address` name the host
service it reaches.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Handle for this forward. Must match `[a-z0-9][a-z0-9-]*`, max 40 chars, unique within `host_ports`. Becomes the Incus device name `port-cfg-<name>` and the `jailbee port rm` key. |
| `port` | int | required | Container-side port (1–65535) — what listens inside the container. |
| `host_port` | int | `port` | Host-side port the container connects to. Set this when the container-side port and the host-side port differ. |
| `proto` | `tcp` \| `udp` | `tcp` | Protocol. |
| `host_address` | string | `127.0.0.1` | Host address the container connects to. Must be an IP literal — a hostname is rejected, because resolving one at device-add time would silently pin a single IP into the device. |
| `container_address` | string | `127.0.0.1` | Container address the proxy listens on. Must also be an IP literal. |

A worked example with the two ports differing — forwarding a host service on
port 9000 to port 3000 inside the container:

```yaml
host_ports:
  - { name: api, port: 3000, host_port: 9000 }
```

Here the container listens on `127.0.0.1:3000`; anything the container
connects to at `3000` actually lands on the host's `127.0.0.1:9000`.

**Only this direction is configurable.** A host-side listener is a
machine-wide resource: if a repo's config declared one, every container of
that repo would fight over the same host port, breaking the property that
many branch containers of the same repo coexist. The reverse direction — a
container service reachable on the host — is not something `host_ports`
exposes at all; use `jailbee port to-host` per container instead (see
[Commands](commands.md)). A `direction:`/`to_host:`/`bind:` key in a
`host_ports` entry is rejected with this same explanation, not a generic
"unknown field" error.

**This is a hole through the `net strict` ACL's egress half by construction.** The
forwarded traffic never traverses the bridge the ACL is attached to — Incus's
forkproxy connects directly out of the container's network namespace to the
host — so a `strict` container's default-deny ACL never sees it, on the
egress side. (`jailbee port to-host`'s forwards are the ingress-side mirror of
this same hole; `host_ports` only ever opens the egress one.) See
[Security and limitations](security.md) for the full picture.

Entries are attached when `jailbee new` creates a container, and reconciled
by `jailbee apply`: an entry that's new is added, one whose properties
changed is replaced, and one that's been deleted from the config is removed.
There's no rebuild and no restart — proxy devices hotplug on a running
container. Reconciliation only ever touches `port-cfg-*` devices; a forward
you added by hand with `jailbee port` is never modified or removed by it.

Layered like `host_mounts`/`host_devices`: per-repo entries append to global
ones; `[]` resets.

### `shared_caches`

The state layer every container of this repo has in common. Each entry is
bind-mounted read-write into *all* of them, which is what makes a tool
worth configuring once: a package-manager cache stays warm across branches
instead of being refilled per container, and settings written in one
container are visible in the next. The same mounts outlive
`jailbee destroy` / `jailbee new`, and they live in `<shared_dir>` rather
than in your host's dotfiles, so a container can write to them freely
without touching your own setup.

That "one cache, all containers" description is the plain shared-mount
case. An entry whose `pool` resolves to non-`None` (see
[`pooled_caches`](#pooled_caches) below) is not a shared mount at all: it
is a per-container **pool slot**, seeded from the warmest existing slot
rather than shared live. Gradle and Maven default to pooled, precisely
because their tools take an inter-process lock on the cache directory —
sharing one mount across containers meant one build's lock made every
other container's build wait or fail.

List of bind-mounted shared caches, each `{name, host_subpath, container_path}`.
The host source is `<shared_dir>/<host_subpath>`; `container_path` may
start with `~` (expands to `/home/dev`).

Default is **stack-neutral** — `ssh` only:

| name | host_subpath | container_path |
|---|---|---|
| `ssh` | `ssh` | `~/.ssh` |

The language caches (`pnpm-store`, `gradle`, `npm`, `m2`) that used to
ship as defaults are now opt-in. Enabling the matching
[`golden.stacks`](#stacks-goldenstacks) key (recommended) adds them
automatically via `Config.effective_shared_caches()`; the low-level
alternative is listing them by hand under `shared_caches:` alongside
the matching `golden.enable_snippets` entry.

> The JetBrains entries (`jetbrains-config` → `~/.config/JetBrains`,
> `jetbrains-data` → `~/.local/share/JetBrains`) are *not* defaults;
> they are appended by `Config.effective_shared_caches()` when
> `jetbrains.enabled: true`. See `### jetbrains` below.
>
> The claude entries follow the same pattern. Two claude rows are appended
> by `Config.effective_shared_caches()` when `claude.enabled: true`:
> `claude` → `~/.claude` and `claude-install` → `~/.local/share/claude`.
> Claude Code's global config (`.claude.json`) lives **inside** the shared
> `~/.claude` mount: the golden image exports
> `CLAUDE_CONFIG_DIR=$HOME/.claude`, and Claude Code reads
> `(CLAUDE_CONFIG_DIR || $HOME)/.claude.json`. See `### claude` below.

`ssh` is seeded on first `jailbee init` from host `~/.ssh/` (`config`,
`known_hosts`, `config.d/`) when `ssh.seed_from_host` is on (default).
The container then has RW access to its own `~/.ssh/` — ControlMaster
sockets work, `known_hosts` updates persist, and users can drop their
own keys into `<shared_dir>/ssh/` if they don't want to use the host
gpg-agent. Private keys, `authorized_keys`, and sockets are **never**
seeded — they live outside the strict allowlist. The dir is forced to
mode 0700 on every `jailbee init` (SSH refuses `~/.ssh` with looser bits).
Disable seeding with `ssh.seed_from_host: false`, or skip the whole
SSH integration with `ssh.enabled: false`.

Set `shared_caches: []` to disable, or override with your own list for
non-JVM/Node stacks. `name` must match `[a-z0-9][a-z0-9-]*` and be
unique. `container_path` must be absolute or start with `~`.

An entry can carry its own `pool:` block instead of relying on
`pooled_caches` below — see `SharedCache.pool` in the next section.

### `pooled_caches`

| Key | Type | Default | Description |
|---|---|---|---|
| `pooled_caches` | dict of `name` → bool | `{}` | Per-cache override of pooling. `true` pools a `shared_caches` entry using its builtin preset (`POOL_PRESETS[name]`); `false` keeps it a plain shared mount. A key naming a cache with no builtin preset is rejected at load time unless that cache's `shared_caches` entry carries its own `pool:` block. `chrome-profile: false` is also rejected: its host directory *is* the pool root, so an un-pooled mount would point every container at the pool's own `slots/` and `by-container/`. Use `chrome.enabled: false` to turn Chrome off. |

A pooled cache is **not** mounted by the binds profile like the rest of
`shared_caches`. Instead each container gets its own slot directory under
`<shared_dir>/<host_subpath>/slots/`, attached as a per-container disk
device named `<cache name>-slot` — allocated on `jailbee new` and on every
boot (or on first use, for Chrome), released on `jailbee destroy`. This is
what stops two containers from contending on one tool's lock files: Gradle
and Maven both take an inter-process lock on their cache directory, so a
build in one container used to block or fail while another container's
build held it.

A key absent from `pooled_caches` follows the preset's own `default_on`:

| Preset | `default_on` | What's hardlinked (`link_paths`) |
|---|---|---|
| `gradle` | `true` | `caches/modules-2/files-2.1`, `wrapper/dists` |
| `m2` | `true` | `repository` |
| `chrome-profile` | `true` | none — SQLite + `Preferences` are rewritten in place |
| `npm` | `false` | `_cacache` |
| `pnpm-store` | `false` | `v3/files` |

`pooled_caches` is a dict rather than a list specifically so
`~/.config/jailbee/global.yaml` and a repo's `.jailbee/config.yaml` merge
per key (the generic dict rule from [Merge rules](#merge-rules)) instead of
one layer's list appending to the other's.

A fresh slot is seeded by copying the warmest existing slot (the one whose
`warmth_file` — or, absent that, whose directory — has the newest mtime).
`link_paths` names subtrees hardlinked from the seed source instead of
copied, so a multi-gigabyte artifact store (Gradle's module cache, Maven's
`repository/`) costs close to nothing per extra container. **`link_paths`
may only name subtrees whose files are written once and later deleted
whole, never modified in place** — hardlinking a lock file, or a `.bin`
that a tool rewrites in place, would restore exactly the cross-container
sharing pooling exists to remove. `wipe_paths` and `stale_globs` are the
other side of that same rule: content excluded from seeding and removed
when a slot is released — regenerable bulk (Gradle's `daemon/` dir) and
stale lock files an unclean exit left behind, respectively.

To pool a cache with no builtin preset — including one of your own
`shared_caches` entries — give that entry an explicit `pool:` block
instead of a `pooled_caches` key:

```yaml
shared_caches:
  - name: my-tool-cache
    host_subpath: my-tool
    container_path: ~/.cache/my-tool
    pool:
      link_paths: [blobs]
      stale_globs: ["*.lock"]
```

**An explicit `pool:` block on a `shared_caches` entry always overrides
`pooled_caches`** — even a `pooled_caches: {my-tool-cache: false}` key does
not un-pool it. This is also true of the presets themselves: setting
`pool:` on the `gradle`/`m2`/`chrome-profile`/`npm`/`pnpm-store` entries
replaces their builtin `PoolSpec` outright rather than merging into it.

`jailbee pool ls [NAME]` / `jailbee pool prune [NAME]` inspect and clean
pool slots — see [`commands.md`](commands.md). A pre-existing
non-pooled cache is migrated automatically: `jailbee init` and
`jailbee apply` move a cache sitting directly under the pool root into
`slots/slot-0`, so the warm cache becomes the first seed source rather
than being discarded. A pooled cache attaches to a container when that
container next boots, so restart any container that was running during
`jailbee apply` (`jailbee restart <name>`) before trusting it to be using
its own slot; `jailbee doctor` flags a pool root that still needs
migrating.

### Networks

`jailbee` ships two hardcoded network modes — `strict`, `loose`
— selectable per container via `defaults.network` or per autostart step
via `network`. The modes are **not** user-configurable: their names,
semantics, and the descriptions stamped onto the generated Incus
profiles all live in code.

| Mode | Egress behaviour |
|---|---|
| `strict` | Default-deny ACL; only `egress_allow` destinations reachable. |
| `loose` | All egress permitted (dedicated `jailbee-loose` bridge). |

#### `egress_allow`

List of allowed egress destinations for **strict** mode. `loose` ignores
this list. Each entry takes one of six forms:

| Form | Meaning | Example |
|---|---|---|
| `<hostname>` | Resolve via DNS, allow **any protocol and port** to each IPv4 | `github.com` |
| `<hostname>:<port>` | Resolve, allow only TCP/`<port>` | `github.com:22` |
| `<ipv4>` | Allow any protocol and port | `192.168.1.5` |
| `<ipv4>:<port>` | Allow only TCP/`<port>` | `192.168.1.5:5432` |
| `<cidr>` | Allow any protocol and port | `10.0.0.0/8` |
| `<cidr>:<port>` | Allow only TCP/`<port>` | `10.0.0.0/8:5432` |

The port-less forms emit an ACL rule with **no `protocol` field at all**,
which Incus reads as "any protocol" — UDP and ICMP to that destination
included, not only TCP. Adding `:<port>` narrows the rule to TCP. Prefer
the `host:port` form when you know the port: `github.com:443` is a
materially tighter rule than `github.com`.

Hostname entries are resolved to IPv4 addresses at ACL-apply time
(during `jailbee init` and `jailbee apply`), and all A records returned by the
resolver are inserted — useful for CDN-fronted services that round-robin a
small pool. Resolution does not stop there: the `jailbee net refresh` timer
re-resolves every registered repo's hostnames each minute into a
**cumulative IP pool** (SQLite-backed, 24 h TTL per IP, capped per host), so
a service that rotates through a set of addresses accumulates all of them
and stale ones expire on their own. The same pass rewrites the ACL and
mirrors the allowed IPs into each strict container's `/etc/hosts`, so the
container's own resolver answers with exactly the addresses the ACL permits
instead of drifting to an IP the ACL will drop. `jailbee apply --no-restart`
forces the same refresh immediately, live, without restarting a container.

**Error handling:** if any hostname fails to resolve, the entire ACL
apply is aborted with a non-zero exit code — the previous ACL remains
in place. The list is meant to be minimal; a broken entry is treated
as a real config error, not a soft warning.

**Limitations:** IPv6 is not supported (a single `:` is the host/port
separator, which would clash with IPv6 syntax). The `host:port` form is
TCP-only — there is no way to allow a *specific* UDP port, so UDP to a
destination is all-or-nothing via the port-less form (DNS and DHCP are
allowed unconditionally, independent of this list).

**`github.com` and strict-mode push:** `github.com` is
intentionally **not** in the default `egress_allow`, so strict-mode work
runs offline-of-GitHub. The operational workflow — why this is the gate
against unattended agents producing surprise pushes, and how to switch to
loose-mode to push/fetch/run `gh` — is documented in
[Security and limitations](security.md).

**Widening the list without editing it.** This key is the committed,
shared-by-everyone allowlist. `jailbee net egress add <entry> [<name>]`
widens one container's copy of it, and `--repo` this host's copy of the
repo's, without touching `config.yaml` — useful for a host that only one
developer needs, or for trying an entry before proposing it to the team.
Container-scoped entries live in the container's own
`user.jailbee.egress_extra` label, die with the container and are materialised
as the command runs; repo-scoped ones are host-local state, not in git, and go
live on the next `jailbee apply`. Overrides are additive only: they can
never narrow what this key grants, so reading `egress_allow` still tells you
the minimum every container of the repo can reach — but not the maximum on a
given machine, which is what `jailbee net egress ls` and `jailbee net status`
report. `jailbee net egress export` prints the whole key back with the
promotable overrides folded in, for when a temporary entry has earned its
place here. See [Egress overrides](security.md#egress-overrides) for the
security posture and [Commands](commands.md) for the flags.

#### `loose_auto_revert`

Auto-reverts `jailbee net loose <c>` back to the previous network mode
after a TTL. Lives in both `~/.config/jailbee/global.yaml` and per-repo
`.jailbee/config.yaml`; per-repo overrides global field by field, so a repo
can change just `after` and inherit `enabled` from the global file.

```yaml
loose_auto_revert:
  enabled: true   # default true
  after: 5m       # default 5m — accepts `30s`, `5m`, `2h`, or raw int (minutes)
```

When `jailbee net loose <c>` schedules a TTL, two container labels are
written: `user.jailbee.loose_until` (ISO8601 expiry) and
`user.jailbee.loose_revert_to` (the mode in effect before the switch).
The existing `jailbee-net-refresh.timer` (already runs every 60 s) reverts
the container when the deadline passes — unless `user.jailbee.autostart_in_progress`
is set, in which case the check is deferred to the next tick so an
autostart step can finish its own network swap without racing the
timer.

`jailbee net strict <c>` always clears the labels.

##### Choosing the TTL per switch

The `after` value above is the *default*. Each `jailbee net loose` decides its
own TTL:

```bash
jailbee net loose mybug --for 2h      # this switch reverts after 2h
jailbee net loose mybug --for 45m     # any `<int>s|m|h`, up to 24h
jailbee net loose mybug --for never   # no auto-revert (same as --no-revert)
jailbee net loose mybug --no-revert   # stay in loose until manually switched
```

`--for` and `--no-revert` are mutually exclusive. A value over 24h, or one
JailBee cannot parse, exits 2 without switching.

With **neither** flag, JailBee asks — but only when all of these hold: stdin is
a TTY, `JAILBEE_NONINTERACTIVE` is unset, and the effective policy is enabled.
The prompt offers a preset list with `after` pre-selected and labelled
*(config default)*, plus `no auto-revert`, `custom…` (type any accepted
duration) and `cancel` (aborts without switching). Anywhere else — a script,
a CI job, `JAILBEE_NONINTERACTIVE=1`, or the Qt dashboard, which launches actions
detached with no stdin — no question is asked and `after` applies. To stay
non-interactive *and* explicit, pass `--for` or `--no-revert`.

The Qt dashboard asks in a dialog instead, pre-selecting the same configured
default and validating a typed value with the same parser as the CLI.

##### With `enabled: false`

A disabled policy means JailBee **schedules** no TTL of its own: `jailbee net loose`
writes no labels, and neither the CLI prompt nor the GUI dialog appears.
It does not veto an explicit request — `--for 2h` still writes the labels and
the timer still reverts the container when they expire. A stated intent wins
over the config switch, so what `jailbee ls` and `jailbee net status` display always
matches what will happen.

`jailbee ls` shows the remaining TTL in a dedicated column (visible only
when at least one container is in loose mode), and `jailbee net status`
lists each loose container with its expiry time.

### `defaults`

Per-container defaults.

| Key | Type | Default | Description |
|---|---|---|---|
| `memory` | string | `16GiB` | Memory limit for new containers. |
| `cpu` | int | `8` | CPU limit for new containers. |
| `network` | enum | `strict` | Initial network mode: `strict` \| `loose`. |
| `storage_pool` | string | `default` | Incus storage pool for new containers. |

### `golden`

Golden image build params.

The golden image is **stack-neutral by default**: only locale, prompt,
GUI libs, and GitHub CLI are installed out of the box (see
[Provisioning snippets](#provisioning-snippets-installd) below).
Language runtimes and cloud helpers (Java, Node, Python, Docker, ECR
helper, registry-mirror CA) ship in the image but stay off until
enabled — via `golden.stacks` (recommended, see
[Stacks](#stacks-goldenstacks)) or directly via `golden.enable_snippets`
(low-level).

| Key | Type | Default | Description |
|---|---|---|---|
| `alias` | string | `<container_prefix>-base` | Image alias used by `jailbee base build`. |
| `ubuntu_version` | string | `26.04` | Ubuntu image tag pulled from `images:`. |
| `java` | string | `amazon-corretto-17` | Java identifier. `amazon-corretto-N` maps to apt package `java-N-amazon-corretto-jdk`; everything else is passed through as an apt package name. Only takes effect when the matching snippet is staged (`golden.stacks.java` or `golden.enable_snippets`). |
| `node` | int | `24` | Node.js major version (used by NodeSource). Only takes effect when the `nodejs` snippet is staged (`golden.stacks.node` or `golden.enable_snippets`). |
| `python` | string | `""` | **Deprecated and ignored.** The container's Python is always the base image's system `python3` (its version is a function of `ubuntu_version` — the Ubuntu archive ships one `python3.X` per release). Setting this raises a soft warning in `jailbee config validate` and `jailbee base build`; the value has no effect. Need a different Python? Add it via `extra_apt_packages` (e.g. `python3.12`, if the base archive has it). |
| `provision_script` | path | `null` (= bundled `install.sh`) | Path to an alternative provisioning script. Relative paths resolve against the repo root. |
| `provision_env` | map | `{}` | Extra env vars passed to the provisioning script. Reserved keys — `CONTAINER_UID`, `CONTAINER_GID`, `JAVA_PACKAGE`, `NODE_MAJOR`, `EXTRA_APT_PACKAGES`, `JAILBEE_USER_HOME`, `JAILBEE_PROVISION_DIR` — raise `ConfigError` (the bundled `install.sh` relies on them). |
| `extra_apt_packages` | list[string] | `[]` | Extra apt package names installed by the bundled `05-extra-apt.sh` snippet (via `EXTRA_APT_PACKAGES`). Each entry must match `[a-z0-9][a-z0-9+\-.]*`. |
| `disable_snippets` | list[string] | `[]` | Names of bundled snippets to drop from the effective set at `jailbee base build`. Matches the logical name (`"registry-mirror-ca"`), the numbered name (`"90-registry-mirror-ca"`), or the full filename (`"90-registry-mirror-ca.sh"`) — same name forms `enable_snippets` accepts. Also drops snippets auto-added by `golden.stacks`. See [Disabling bundled snippets](#disabling-bundled-snippets). |
| `enable_snippets` | list[string] | `[]` | Names of opt-in `install.d.available/` snippets to stage into the effective set (by logical name, e.g. `"nodejs"`, or full filename, e.g. `"30-nodejs"`/`"30-nodejs.sh"`). Unioned with the snippets `golden.stacks` implies. See [Opt-in snippets](#opt-in-snippets-installdavailable). Unknown names are ignored with a warning. |
| `stacks` | object | all fields off | High-level `java`/`node`/`python`/`docker`/`ecr` toggles — the recommended way to enable a runtime. See [Stacks](#stacks-goldenstacks). |

### Master switches — opt-in by default

Every host-tooling block (`gpg`, `ssh`, `jetbrains`, `chrome`) ships with
`enabled: false`. None of them does anything until the user opts in,
typically at the global layer (`~/.config/jailbee/global.yaml`). Per-repo
overrides can also turn a block on for repos that need it. The point is
to keep the container minimal until the user explicitly says "yes, wire
this host integration in."

`jailbee config init --global` writes a template that flips all four to
`enabled: true` — the generated file is a working starting point for a
typical developer setup, not a literal echo of the built-in defaults.

### `gpg`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | RO bind-mount `~/.gnupg`, attach the host `/run/user/<uid>/gnupg` socket dir as the `gpg-socket` device (read-only), and set `SSH_AUTH_SOCK` in the base profile to the host gpg-agent's SSH socket. Disables the doctor `gpg-agent socket` check when `false`. |

When `enabled: true`, the host gpg-agent provides SSH authentication
inside the container (YubiKey / GPG-SSH keys work transparently). The
auto-added bind-mount can be overridden by adding a manual entry to
`host_mounts` with `container: /home/dev/.gnupg` — the manual entry
wins.

The `gpg-socket` device is mounted **read-only**, and so is `pulse-socket`.
Both are directories inside the host's *own* `/run/user/<uid>`, and the
container runs its own `systemd --user`: its `gpg-agent.socket`,
`dirmngr.socket` and `pulseaudio.socket` listen on paths inside those mounts
and unlink whatever file is already there before binding — which would take
the host's agent down (`socket file has been removed - shutting down`) on
every container boot. Read-only makes that unlink `EROFS` while leaving the
socket fully usable, since a unix-socket client needs no writable filesystem,
and the host stays free to re-create its own sockets. The golden image also
masks those user units, so the container does not even try.

With `enabled: false` nothing gpg-related reaches the container: no
`~/.gnupg` mount, no `gpg-socket` device, and no `SSH_AUTH_SOCK`. The
golden image's `/etc/profile.d/jailbee-env.sh` fallback only sets
`SSH_AUTH_SOCK` when the variable is still unset *and* the gpg-agent
socket is actually present, so login shells stay clean on hosts that run
no gpg-agent.

### `ssh`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Bind-mount `<shared_dir>/ssh` as the container user's `~/.ssh` and enforce `0700` on each `jailbee init`. `false` skips the mount and the perms check; the container will have no `~/.ssh`. |
| `seed_from_host` | bool | `true` | On first `jailbee init`, copy host `~/.ssh/{config, known_hosts, config.d/}` into the shared dir. Private keys, `authorized_keys`, and sockets are never seeded. Ignored if `enabled: false`. |

### `jetbrains`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. When `false`, `jailbee ide` exits 2 with a clear message, autostart skips the IDE launch, the userprefs / toolbox auto-mounts are omitted from `effective_host_mounts`, and the JetBrains egress hosts are NOT auto-added to `egress_allow`. The individual sub-toggles below have no effect. |
| `ide` | enum | `idea` | Which JetBrains binary `jailbee ide` (no `--app`) and autostart launch. Supported: `idea \| webstorm \| pycharm \| goland \| clion \| phpstorm \| rider \| rubymine \| datagrip \| rustrover \| aqua \| dataspell \| studio`. |
| `userprefs_from_host` | bool | `false` | Opt-in RW bind-mount of `~/.java/.userPrefs/jetbrains/` (host JetBrains Account / license tokens). Enable only if you want to reuse the host's JBA login state across containers. Does not affect egress (license-host egress is gated only on `enabled`). Ignored when `enabled: false`. |
| `share_idea` | bool | `true` | Bind-mount `<shared_dir>/jetbrains-idea` over `~/<container_prefix>/.idea` inside each container so project-side JetBrains state (run configs, code styles, inspection profiles, project view) survives `jailbee destroy` / `jailbee new` cycles. The mount is attached as a per-container device *after* `git clone` (it cannot live in the binds profile — Incus pre-creating the target would break the clone). Skipped automatically in `--mount` mode so the host's own `.idea/` wins. Set to `false` if the source repo tracks `.idea/*` files in VCS that should remain visible to the IDE — the shared dir starts empty and would otherwise shadow them on first launch. Ignored when `enabled: false`. |
| `ai_enabled` | bool | `false` | Opt-in: also auto-extend strict-mode `egress_allow` with the JetBrains AI Assistant backend hosts (`api.app.prod.grazie.aws.intellij.net`, `api.jetbrains.ai`). Leave off unless you actually use AI Assistant. Ignored when `enabled: false`. |
| `autostart` | bool | `false` | Launch the IDE after autostart steps. No-op if no graphical session is detected. Ignored when `enabled: false`. |
| `toolbox_host_path` | path \| null | `~/.local/share/JetBrains/Toolbox` | Host path RO-mounted to `/opt/jetbrains-toolbox`. The container-side path is hardcoded because `jailbee`'s IDE launcher looks for binaries there. `null` disables the auto-mount. Ignored when `enabled: false`. |

When `jetbrains.enabled` is true, `egress_allow` is automatically
extended (in strict-mode ACL generation only — the YAML field is not
modified) with the JetBrains hosts the IDE needs for account/license
activation, plugin marketplace and installer CDNs:
`account.jetbrains.com`, `oauth.account.jetbrains.com`,
`cloudconfig.jetbrains.com`, `plugins.jetbrains.com`,
`downloads.marketplace.jetbrains.com`, `www.jetbrains.com`,
`resources.jetbrains.com`, `download.jetbrains.com`,
`download-cf.jetbrains.com`, `download-cdn.jetbrains.com`,
`frameworks.jetbrains.com`, `data.services.jetbrains.com`,
`api.jetbrains.cloud` (all port 443). The list is sourced from
JetBrains' published allowlist guidance plus empirical observation of
the JBA sign-in flow. Without these, the IDE falls back to "Start free
trial" after the locally cached license expires (~30 days for paid
plans), and plugin updates / framework dependency lookups fail in
strict mode. `resources.jetbrains.com` serves the OAuth provider icons
rendered in the JBA sign-in dialog — blocking it prevents the login UI
from finishing, so the IDE silently stays in trial state.
`oauth.account.jetbrains.com` is the OAuth sign-in endpoint (AWS ELB
in eu-west-1, distinct IP space from `account.jetbrains.com`); without
it the sign-in handshake cannot complete. `downloads.marketplace.jetbrains.com`
is the CloudFront-backed plugin payload CDN, separate from
`plugins.jetbrains.com`. `api.jetbrains.cloud` (note the `.cloud` TLD)
hosts the license trace-status endpoint.

When `jetbrains.ai_enabled` is also true, the AI Assistant backend
hosts are appended too: `api.app.prod.grazie.aws.intellij.net`,
`api.jetbrains.ai` (both port 443).

When `jetbrains.userprefs_from_host` is true, concurrent host+container
IDE writes to the same files are possible but rare in practice (login
tokens are written once per session). If both write at the same time,
last-flush wins; the loser's in-memory state diverges until the next
IDE restart.

### `chrome`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. When `false`, `jailbee chrome` exits 2 with a clear message, autostart skips the Chrome launch, and the `host_path` auto-mount is omitted from `effective_host_mounts`. |
| `url` | string \| null | `null` | URL Chrome opens. `null` = no URL. `jailbee chrome <name> <URL>` overrides this per-call. |
| `dark_mode` | bool | `false` | Pass `--force-dark-mode --enable-features=WebContentsForceDark` regardless of host GTK theme. |
| `autostart` | bool | `false` | Launch Chrome after autostart steps. No-op if no graphical session, or when `enabled: false`. |
| `host_path` | path \| null | `/opt/google/chrome` | Host path RO-mounted to `/opt/google/chrome`. The container-side path is hardcoded because `gui.open_chrome` invokes `/opt/google/chrome/google-chrome` directly. Override for non-standard installs (e.g. a chromium dir); `null` disables the auto-mount. Ignored when `enabled: false`. A manual `host_mounts` entry with `container: /opt/google/chrome` wins. |

### `agents`

Generic hook for terminal coding agents — Claude Code plus five untested
templates (`codex`, `gemini`, `aider`, `opencode`, `grok`), or one you define
yourself. A mapping keyed by agent name, valid at both this file and
`~/.config/jailbee/global.yaml`, and it merges over a shipped preset
(deep-merge — see [Merge rules](#merge-rules) above) rather than needing
every field spelled out. Full mechanism, the preset table, the "which paths
to share" rule, and a worked example live in
[Generic agent support](agents.md) — this entry is the schema reference.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch: gates the shared mount, the strict-mode egress add, install/update at `jailbee new` time, and the `jailbee doctor` shared-dir check. |
| `autostart` | bool | `false` | Launch `command` in a background autostart tmux window. Requires `enabled: true`. |
| `command` | string | `""` | The command line the autostart window execs; also the default source for `install_check`. Required when `enabled: true`. |
| `install` | string \| null | `null` | Shell command run at `jailbee new` time when `install_check` fails. |
| `install_check` | string \| null | `null` | Probe deciding install-vs-update. Defaults to `command -v <first token of command>`. |
| `update` | string \| null | `null` | Shell command run at `jailbee new` time when `install_check` succeeds and `auto_update` is true. |
| `auto_update` | bool | `true` | `false` leaves an existing install untouched; a missing one is still installed. |
| `install_network` | `strict` \| `loose` | `strict` | Network mode for the install/update step only. |
| `shared` | list of `{subpath, path, type, seed}` | `[]` | Bind mounts from `<shared_dir>/<subpath>` to `<path>`. `type: dir` (default) or `file`; `seed` (file only) is written once if the target is absent. |
| `egress_allow` | list[string] | `[]` | Strict-mode allowlist entries added while this agent is enabled. Same grammar as top-level [`egress_allow`](#egress_allow). |
| `env` | map[string, string] | `{}` | Env vars passed to the install/update step and the autostart launch step. |

An agent name that matches one of the six shipped presets is deep-merged
over that preset (preset → global.yaml → repo, same append/reset rules as
every other list field); any other name is used as-is with no preset base.
`jailbee config validate` additionally rejects a name outside
`[a-z0-9-]+`, `enabled: true` with an empty `command`, `autostart: true`
without `enabled: true`, and a `shared` subpath that collides with a
built-in shared subdir or with a different mount target another agent
already claimed.

```yaml
agents:
  codex:
    enabled: true
    autostart: true
```

### `claude`

**`agents.claude` is the preferred spelling of this block.** The top-level
`claude:` key documented below is a supported **legacy alias** — moved to
`agents.claude` at config-load time, before validation. Defining both
`claude:` and `agents.claude` in the merged config is a `ConfigError`.
Everything below applies identically under either spelling, and `claude`
also carries the generic `agents` fields from the table above
(`install`, `update`, `install_check`, `install_network`, `shared`,
`egress_allow`, `env`) — not repeated here since they mean the same thing
for every agent. See [Generic agent support](agents.md#9-claude) for the
short version of this same note.

Claude Code CLI integration. The schema default is disabled, so a repo
with no `claude:`/`agents.claude` block anywhere gets no Claude Code.
Opt-in belongs in `~/.config/jailbee/global.yaml` — and the template
written by `jailbee config init --global` already carries
`claude.enabled: true` (or `agents.claude.enabled: true`), so the usual
first-run path turns it on. Delete or flip that block to keep Claude Code
out.

| Key | Type | Default | Description |
|---|---|---|---|
| `claude.enabled` | bool | `false` | Master switch. When `true`, JailBee mounts `<shared_dir>/claude` → `~/.claude` and `<shared_dir>/claude-install` → `~/.local/share/claude` as shared caches, auto-extends strict-mode `egress_allow` with `api.anthropic.com:443` + `code.claude.com:443` + `claude.ai:443` + `downloads.claude.ai:443` (the last two cover the `install.sh` bootstrap and the native CLI's self-update), creates an empty `<shared_dir>/claude` on `jailbee init`, and includes it in `jailbee doctor` checks. Claude Code's global config (`.claude.json`) lives **inside** the shared `~/.claude` mount: the golden image exports `CLAUDE_CONFIG_DIR=$HOME/.claude`, and Claude Code reads `(CLAUDE_CONFIG_DIR || $HOME)/.claude.json`. Host `~/.claude` is **not** read — Claude Code runs its onboarding flow inside the first container from a clean state, and subsequent containers in the same repo inherit that state via the shared cache. |
| `claude.plugins_enabled` | bool | `true` | When `true` (and `claude.enabled` is `true`), also auto-extends `egress_allow` with the GitHub + npm hosts Claude Code's plugin marketplace, skills and SessionStart hooks reach (`github.com`, `api.github.com`, `raw.githubusercontent.com`, `objects.githubusercontent.com`, `codeload.github.com`, `registry.npmjs.org`). Set to `false` to keep the API reachable while blocking marketplace traffic. Has no effect when `claude.enabled: false`. |
| `claude.autostart` | bool | `false` | When `true` (requires `claude.enabled: true`), `jailbee` appends a synthetic `claude` window to the `autostart` tmux session on every container start; `jailbee tmux <c>` lands in that window. `validate_runtime` rejects `autostart: true` with `enabled: false`. |
| `claude.command` | string | `"claude"` | Command line executed in the `claude` autostart window — override to pass flags (e.g. `claude --dangerously-skip-permissions`) or an env-prefix wrapper. Ignored when `claude.autostart` is `false`. |
| `claude.auto_update` | bool | `true` | When `true`, `jailbee new` runs `claude update` inside the container so the shared install advances to the latest release. When `false`, an existing install is left untouched, but a missing one is still installed. Has no effect when `claude.enabled: false`. |
| `claude.install_jailbee_skills` | bool | `true` | When `true` (requires `claude.enabled: true`), `jailbee new` and `jailbee apply` copy JailBee's bundled Claude skills (`jailbee-usage`, `jailbee-repo-setup`) into `<shared_dir>/claude/skills/` so the in-container Claude understands jailbee. Host-side file copy only — no network. Has no effect when `claude.enabled: false`. The pre-1.0 name `claude.install_gie_skills` was retired in 1.1.0: a config still using it fails to load with an error naming this key. |
| `claude.ai_pr_description` | bool | `true` | When `true` (and `claude.enabled` is `true`), `jailbee pr` generates the PR title and body by invoking Claude inside the container, showing a spinner while it runs. Falls back to commit-subject title + placeholder body on any Claude failure with a warning. Pass `--no-ai` to opt out per-invocation without changing config. Has no effect when `claude.enabled: false`. |
| `claude.ai_pr_branch` | bool | `true` | When `true` (and `claude.enabled` is `true`), `jailbee pr` asks the in-container Claude to propose a convention-following PR head branch name when opening a **new** PR. Has no effect when `claude.enabled: false`. |
| `claude.ai_pr_model` | string \| null | `"sonnet"` | Model passed to `claude --model` when generating the PR text. Writing a description is a bounded job, and pinning it means the generation does not compete for the same budget as the coding work that just happened in the container. Accepts an alias (`sonnet`, `opus`, `haiku`) or a full model ID; `null` omits the flag so the container's own default model applies. `haiku` works but has a smaller context window, so a large cumulative diff may not fit. Rejected at load if it is not a single whitespace-free token. Has no effect when `claude.enabled: false` or `claude.ai_pr_description: false`. |
| `claude.pr_prompt` | string \| null | `null` | Project-specific PR-writing instructions, usually a YAML block scalar in a repo's `.jailbee/config.yaml`. Embedded in JailBee's own prompt as a delimited section that **outranks** the generic title/body guidance, so a project can dictate the shape of its descriptions — but it is placed before the JSON response contract, which it cannot override. Whitespace-only is treated as unset; capped at 20 000 characters. Has no effect when `claude.enabled: false` or `claude.ai_pr_description: false`. |
| `claude.ai_pr_timeout` | int | `600` | Seconds `jailbee pr` gives the in-container Claude to produce the PR text before falling back to a placeholder. Generation is an agentic run, not one model call — it reads the log, the cumulative diff, the PR template, the branch's spec and the CI config across a dozen-plus turns, so the cost scales with the repository, not just with the diff. Measured in JailBee's own repo on a 21-file/+940 diff: 109 s. Raise it for a large tree, or when `claude.pr_prompt` asks for work that takes longer. Must be positive — to switch generation off use `claude.ai_pr_description: false`. Has no effect when `claude.enabled: false` or `claude.ai_pr_description: false`. |

Example global config:

```yaml
claude:
  enabled: true
  plugins_enabled: true
```

### Encoding a project's PR standard

`jailbee pr` already reads `.github/pull_request_template.md`, the spec or
issue a branch implements, and `CONTRIBUTING.md` / `CLAUDE.md` / `AGENTS.md`
before writing anything. `claude.pr_prompt` is for the rules that live in
none of those files — commit them to the repo's `.jailbee/config.yaml` so
every container generates descriptions the same way:

```yaml
claude:
  pr_prompt: |
    Body sections, in this order and with these exact headings:
      ## Why      — the user-visible problem, one paragraph, no implementation
      ## What     — bullets, each naming the file or symbol it changed
      ## Testing  — the commands you actually ran, verbatim
    Never use the word "comprehensive". Link the Jira ticket from the branch
    name as `[ABC-123](https://example.atlassian.net/browse/ABC-123)`.
```

These instructions win over JailBee's generic guidance where the two
disagree, which is why the block cannot break generation: the response
format Claude has to return is stated after it and stays JailBee's.

The claude shared caches are not present in the `shared_caches:` default
list — they are auto-added by `Config.effective_shared_caches()` when
`claude.enabled` is `true`. Manual entries in `shared_caches:` with names
`claude` or `claude-install` suppress the auto-add (same precedent as
`effective_host_mounts`).

### `terminal` / `terminal.kitty`

When a developer runs `jailbee shell` / `jailbee tmux` from a kitty terminal on
the host, `TERM=xterm-kitty` propagates into the container via `incus
exec`. The base image's terminfo database doesn't ship the `xterm-kitty`
entry, so curses-aware tools warn `terminal is not fully functional` and
degrade. When active, this block RO bind-mounts the host's `xterm-kitty`
terminfo file into every container so the entry resolves naturally.

| Key | Type | Default | Description |
|---|---|---|---|
| `terminal.kitty.enabled` | `"auto"` \| bool | `"auto"` | `"auto"` activates iff the host terminfo file can be located. `true` activates and fails `jailbee config validate` if no file is found. `false` disables the integration unconditionally. |
| `terminal.kitty.host_terminfo_path` | path \| null | `null` | Explicit host path to the `xterm-kitty` terminfo file. When `null` (default), autodetect probes `/usr/share/terminfo/x/xterm-kitty`, `~/.local/kitty.app/lib/kitty/terminfo/x/xterm-kitty`, and `~/.terminfo/x/xterm-kitty` in that order. |

### `autostart`

IDE and Chrome launch decisions live in `jetbrains.autostart` and
`chrome.autostart` (not here). The `autostart` block describes the shell
steps that run inside the container during startup.

Top-level keys:

| Key | Type | Default | Description |
|---|---|---|---|
| `on_create` | list[Step] | `[]` | Steps run once after `jailbee new` provisions the container. |
| `on_start` | list[Step] | `[]` | Steps run on every stopped→running transition: both `jailbee new` (after `on_create`) and `jailbee start`. Put one-shot setup in `on_create` and recurring launches (dev servers, etc.) in `on_start` — don't duplicate. |
| `step_timeout` | int | `600` | Default per-step timeout in seconds (overridable per step). |
| `env` | map | `{}` | Global env merged into every step (per-step `env` wins on key collisions). |

Each step is `{name, run, ...}`:

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Identifier; unique within the trigger. |
| `run` | string | required | Shell command run as the dev user. Always `cd`'d into `working_dir` first. |
| `network` | `strict`/`loose`/`null` | `null` | Swap the container's net profile to this mode for the step's duration; restored after. `null` keeps the current profile. |
| `mounts` | list[string] | `[]` | `optional_mounts` keys to attach for the step's duration. Validated against `optional_mounts`. |
| `env` | map | `{}` | Per-step env (merged on top of `autostart.env`). |
| `working_dir` | string | `""` | Path relative to `repo_dir`. Empty = `repo_dir` itself. |
| `background` | bool | `false` | Run in a detached tmux window; do not wait for completion. Attach via `jailbee tmux <container>` to see output. |
| `timeout` | int | `null` (= `step_timeout`) | Per-step timeout override in seconds. |
| `continue_on_error` | bool | `false` | If `true`, a non-zero exit warns instead of aborting subsequent steps. |

#### Where do my autostart steps run?

All steps run inside a container-local tmux session named `autostart`. One
window per step. To attach interactively:

```bash
jailbee tmux <container>
```

Sync steps' output stays visible in their window after they finish (the
session has `remain-on-exit on`). Background steps (e.g. `pnpm dev`) keep
running in their own window until the container stops.

#### Where does the autostart config come from?

In clone mode, `jailbee new <branch>` reads `autostart` from the **target
branch's** committed `.jailbee/config.yaml`, at the exact commit it is about to
clone — not from your checkout. The container runs the branch's files, so it
runs the branch's startup steps. Every other key (mounts, network defaults,
`cpu`/`memory`, `container_prefix`, and host-level keys like
`docker_registry_mirror`, `ls`, `dashboard`) still comes from the checkout you
run `jailbee new` from; a branch cannot change how the operator runs containers.

If the branch's autostart deviates from your checkout's, `jailbee new` prints a
compact diff naming what it read from — the branch ref (`refs/heads/feat/x`)
when cloning a local branch as-is, or a short sha with the branch name in
parens (`a1b2c3d4e5f6 (feat/x)`) when the clone resolved to a pinned commit
(origin-mode, a PR review, or an explicit `--pr`/commit checkout):

```
autostart config comes from a1b2c3d4e5f6 (feat/x), not your checkout:
  + on_create[migrate]
  - on_start[old-watcher]
  ~ on_create[build]: run changed
  ! step_timeout: 600 → 900
  ! env: NODE_ENV
```

Step names are trigger-qualified (`on_create[build]` vs `on_start[build]`)
because the same name can exist under both triggers as distinct steps.

##### The privilege check is a separate comparison

That diff explains a surprise: why the container runs steps you don't have.
Whether the branch *gains* anything by them is a different question, and it is
answered against a different reference — the repo's reviewed baseline,
`refs/remotes/<upstream_remote>/<default_branch>`, rather than your checkout:

```
branch autostart widens privileges beyond refs/remotes/origin/main:
  ⚠ network access 'loose' in: on_start[warmup]
  ⚠ attaches host mount(s): aws
```

Your checkout is one snapshot of one branch: it may lag the upstream, run
ahead of it, or be an unrelated feature branch with local edits. Measuring
privileges against it made the same `jailbee new` ask one developer and not
another, and turned "my checkout is a few commits behind" into an escalation.
The default branch on the upstream is what review and CI gate, so that is the
baseline. If it carries no usable config, the check falls back to comparing
against your checkout and says so in that line. When the baseline ref cannot
be read at all — no such remote, or a default branch never fetched — that
fallback is a genuinely weaker gate, so it is warned about rather than only
noted in the line.

Two kinds of widening are reported, and they are weighed differently:

- **a step attaching an `optional_mounts` entry** the baseline's same-named
  step does not — these are typically personal credential directories
  (`~/.aws`, `~/.m2`), and the step's command line comes from the same branch.
  This **always asks for confirmation**, defaulting to **no**; declining
  aborts the whole `jailbee new` with nothing created. Attaching a mount is what
  *creates* the asset — a credential directory the container would not
  otherwise hold — and no network mode protects against it.
- **a step widening network access** from `strict` to `loose`. This asks only
  for an **untrusted head**: `jailbee new --pr N` where the PR's head lives in a
  **fork** (`isCrossRepository`) — code nobody with push access to your repo has
  vouched for. Everything else warns and proceeds. Once the container runs the
  branch's code, `strict` is an egress allowlist of package registries and
  forges that all accept uploads, so it is no boundary against that code —
  while `loose` is the ordinary way a step installs dependencies.

  A PR *number* is not the signal, deliberately: an internal PR's head is a
  branch in your own origin, byte-identical to what `jailbee new <branch>` would
  clone and pushed by someone who can already run code in your containers.
  Gating one spelling and not the other would ask about how the command was
  typed rather than about risk — and a question that fires routinely trains you
  to click through the mount one, which is the question that matters.

In both cases a brand-new step counts as widening, since the baseline has no
counterpart to compare against. Everything else a step controls (`run`, `env`,
`working_dir`, `background`, `timeout`, `continue_on_error`) only warns: it is
container-internal, and adds nothing beyond the code execution a cloned branch
inherently has.

`--yes`/`-y` accepts without asking (in addition to its original job of
skipping the "branch already exists" prompt). `--no-autostart` skips the branch
config entirely: none of its steps run, so there is nothing to diff or confirm.

With `jailbee new --background` the question is asked **before** the run detaches,
in the terminal you are still sitting at — a detached worker has no stdin, so a
question left for it could only ever be answered "no". Declining exits without
creating a container or recording a job. The answer is pinned to the commit you
were shown: if the branch moves between the confirmation and provisioning, the
worker aborts naming the move instead of provisioning a config nobody saw. When
there is no terminal to ask on at all (a script, CI), `jailbee new` says so and
tells you to pass `--yes`.

A branch with no committed `.jailbee/config.yaml` falls back silently to your
checkout's autostart — a branch need not define one. A branch config that
exists but can't be used (invalid YAML, a validation failure, or an
autostart step naming an `optional_mounts` key your host config doesn't
define) warns and falls back the same way.

`--mount` and `--no-clone` are unaffected — they share the host working tree,
so there is no distinct target branch to read from. `jailbee start`, `jailbee
restart`, and `jailbee apply` always use your checkout's config; only creation
reads the branch.

### `container_prefix`

| Key | Type | Default | Description |
|---|---|---|---|
| `container_prefix` | string | derived from `repo_root.name` | Prefix for all jailbee-owned Incus resources (containers, profiles, ACL). Must match `[a-z0-9][a-z0-9-]*`. Override only if `repo_root.name` doesn't match the regex (e.g. underscore, dot, or capital letter). |

### `docker_registry_mirror.extra_registries`

```yaml
docker_registry_mirror:
  extra_registries:
    - 803520778560.dkr.ecr.eu-north-1.amazonaws.com
```

| Key | Type | Default | Description |
|---|---|---|---|
| `extra_registries` | list[string] | `[]` | Extra registry hostnames this repo pulls images from but which rpardini does not cache out of the box. Entries must be bare hostnames, optionally with `:port` — no scheme, no path. |

rpardini's image defaults cache only Docker Hub, `registry.k8s.io`,
`gcr.io`, `quay.io`, and `ghcr.io`. Hostnames outside that set
(notably AWS ECR — `*.dkr.ecr.<region>.amazonaws.com`) are
CONNECT-tunneled without caching, so every `jailbee new` re-pulls those
images from the internet. Listing them here pushes them into the
mirror's `REGISTRIES` env on the next `jailbee new` / `jailbee apply`, after
which pulls hit the rpardini cache on second run.

Mechanics: `jailbee new` and `jailbee apply` write the union of these entries
into `/etc/jailbee-registry-proxy.env` inside the
`jailbee-registry-mirror` container and restart `jailbee-registry-proxy.service`.
The mirror is host-global, so the set accumulates across repos —
once added, a hostname stays until the mirror container is recreated.

### `new`

Policy for what state `jailbee new` starts a new container from when the
container's branch does not already exist in the source repo (the
"default-branch fallback" path).

```yaml
new:
  clone_from: origin   # 'origin' (default) | 'local'
  autofetch: true      # default true
  background: false    # default false
  submodules: true     # default true
```

| Key | Type | Default | Description |
|---|---|---|---|
| `clone_from` | enum | `origin` | With `origin`, the new container is checked out at `refs/remotes/origin/<default_branch>` on the host, so the working tree reflects the upstream tip. With `local`, the classic behaviour applies: `refs/heads/<default_branch>` (whatever the host's local default branch points at). |
| `autofetch` | bool | `true` | When `true` and `clone_from='origin'`, `jailbee new` runs `git fetch origin <default_branch>` on the host before resolving the ref, so a stale host doesn't propagate into the container. Set `false` to skip and rely on whatever the host already has. |
| `background` | bool | `false` | Run `jailbee new` detached in the background by default. Overridable per-invocation with `--background` / `--no-background`. An explicit `--attach shell`/`--attach tmux`, `--tmux`, or `--shell` also forces foreground, since a detached run has no terminal to attach; `--attach none` / `--no-attach` don't, and combine fine with `--background`. |
| `submodules` | bool | `true` | Initialize the superproject's git submodules (recursively, offline from the host bind mount) in the new container. Set `false` to skip. |

Scope of `clone_from` / `autofetch`: these two apply **only** to the
default-branch fallback path — i.e. when no `--base` is given and the
requested branch does not yet exist in the source repo. `--base <X>`
always uses `refs/heads/<X>` (local) by design, since the user has
explicitly picked a local starting point. `--pr <N>` performs its own
fetch (`gh`-driven) and is unaffected. `background` and `submodules`
apply to every `jailbee new` invocation regardless of the starting-point
path.

Errors:

- If `autofetch=true` and the fetch fails (no network, ACL denial,
  bad credentials, …), `jailbee new` aborts before touching Incus state.
  Resolve the underlying issue or set `autofetch: false`.
- If `clone_from='origin'` but `refs/remotes/origin/<default_branch>`
  does not exist in the host repo, `jailbee new` aborts. Fetch first, or
  set `clone_from: local`.

### `destroy`

| Key | Type | Default | Description |
|---|---|---|---|
| `background` | bool | `false` | Run `jailbee destroy` detached in the background by default. Overridable per-invocation with `--background` / `--no-background`. |

### `boot`

| Key | Type | Default | Description |
|---|---|---|---|
| `background` | bool | `false` | Run `jailbee start` and `jailbee restart` detached in the background by default. Overridable per-invocation with `--background` / `--no-background`. |

One key covers both commands: what makes either slow is the autostart run that
follows the boot, and it is the same run. A detached boot is refused while
another background job for that container is still live — two of them would
interleave their autostart steps.

### `after_new`

| Key | Type | Default | Description |
|---|---|---|---|
| `after_new` | `"shell"` \| `"tmux"` \| `"none"` | `"none"` | After a successful `jailbee new`, automatically attach to the new container. `"tmux"` attaches to the autostart tmux session (creating it on demand), `"shell"` opens an interactive bash login shell, `"none"` (default) returns to the host prompt. Override per-invocation with `jailbee new --attach <mode>`, the `--tmux` / `--shell` shorthands, or `--no-attach`. Unlike `--attach shell`/`--attach tmux`, `--tmux`, and `--shell` — which force foreground — this config default yields silently to a background run, same as `--attach none` / `--no-attach`. |

### `confirm`

```yaml
confirm:
  auto_target: true    # confirm push/pull/checkout when jailbee picks the container
```

`jailbee git push` / `pull` / `checkout` settle on the single existing container
without showing a picker. With `confirm.auto_target` on (the default) they
first print a plan block — both branch names, both tips, the action — and ask
`[Y/n]`. Declining aborts before anything reaches the container or a host
branch (though on the push path, the host's `origin/<source>` fetch already
ran). Per-invocation overrides: `--confirm` / `--no-confirm`. Off a TTY,
`pull`/`checkout` print the block and only skip the prompt; `push` requires
an explicit name off a TTY in the first place, so it never reaches this
confirmation there. See
[Confirming an auto-picked container](git-bridge.md#confirming-an-auto-picked-container).

### `pull`

Controls `jailbee git pull`'s post-merge cleanup prompts after a successful
merge from a container's branch into the container's recorded **base
branch** (`user.jailbee.base_branch`, set at `jailbee new` time); override the
merge target for a single invocation with `--into <branch>`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `destroy_container` | `"prompt"` \| `"always"` \| `"never"` | `"prompt"` | Whether to destroy the container after a successful merge. |
| `delete_branch` | `"prompt"` \| `"always"` \| `"never"` | `"prompt"` | Whether to delete the merged local host branch. |

`--cleanup` on the CLI forces both keys to `always`; `--no-cleanup`
forces both to `never`. Cleanup failures are warnings, not errors.

Example (`~/.config/jailbee/global.yaml`):

```yaml
pull:
  destroy_container: prompt
  delete_branch: prompt
```

> Migration note: this block was previously called `merge:`. A config
> file that still uses `merge:` fails to load with a clear error
> naming both the old and new key and the file path. Rename the block
> to `pull:` to fix.

### `push`

Controls `jailbee git push`'s default behavior when called with partial
arguments. Each key may be `"ask"` (open an interactive prompt) or a
concrete value; `default_action` defaults to `"ask"`, `default_source`
defaults to `"base"`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `default_action` | `"merge"` \| `"rebase"` \| `"plain"` \| `"ask"` | `"ask"` | What to do after pushing the ref. `"plain"` is transport-only (no merge/rebase). |
| `default_source` | `"default-branch"` \| `"current"` \| `"base"` \| `"ask"` | `"base"` | Which branch to push. `"base"` resolves to each container's recorded base branch (`user.jailbee.base_branch`), so the host pushes exactly what the container was branched from. `"default-branch"` always uses the repo's default branch, regardless of the container's base. `"current"` uses `git symbolic-ref --short HEAD`. `"ask"` opens an interactive picker every time. |
| `push_from` | `"origin"` \| `"local"` | `"origin"` | Which *copy* of that branch to push. `"origin"` sends `refs/remotes/origin/<source>` and falls back to `refs/heads/<source>` when the branch has no upstream copy; `"local"` reverses the order. |
| `autofetch` | bool | `true` | Run `git fetch origin <source>` on the host before resolving the ref, so the remote-tracking copy is current. Only applies in `push_from: origin` mode. Best-effort — the push proceeds with the refs already present; a failure is reported only when the origin ref is what ended up travelling (and may therefore be stale), not when resolution fell back to the local branch because the source simply isn't on origin. |

CLI flags (`--merge`, `--rebase`, `--plain`, `--from`, `--current`,
`--from-origin`, `--from-local`, `--fetch`/`--no-fetch`) always win over
the configured defaults. With `"ask"`, the command opens a `questionary`
prompt; in a non-TTY environment, the command errors and points at the
relevant config key.

The dashboards follow the same rule from the other side. `jailbee dashboard`
hands over the real terminal, so the `questionary` prompt appears exactly as it
would on the command line. The Qt dashboard cannot — its child process has no
stdin — so it asks in a dialog instead and passes the answer as a flag, and
**only** for a key that is `"ask"`: pin `default_action` or `default_source` and
the GUI stops asking about it. Its source dialog offers the container's recorded
base branch and the host's checked-out branch, the two choices it can express
without reading the host repo; for `"default-branch"`, set `default_source` in
the config rather than answering per push.

#### Why `push_from` defaults to `origin`

`git fetch` updates `refs/remotes/origin/<branch>`; the local
`refs/heads/<branch>` only moves on `git pull`. For any branch you do
not check out on the host — typically the base branch a container is
pushed with — the local ref is stale exactly when you just fetched. With
`push_from: origin` (plus `autofetch`), `jailbee git push` sends the upstream
tip, matching what `jailbee new` already does via
[`new.clone_from`](#new)`: origin`.

This also protects `jailbee ls`: when the source equals the container's base
branch, the push force-updates `refs/jailbee/base/<base>`, so pushing a local
base that trails origin would move that anchor *backwards* and inflate
the AHEAD counts.

`--current` (or `default_source: current`) always resolves locally
regardless of these keys: the host's checked-out branch is the work in
progress, so the local ref is the fresher one by construction.

`--pr` ignores them entirely — `jailbee` fetches the PR head into
`refs/jailbee/pr/<N>/head` and pushes exactly that ref, so there is no
local-vs-origin choice to make (passing `--from-local`/`--from-origin`
with `--pr` is rejected).

Use `--from-local` when the host has commits not yet pushed to origin.
`jailbee` warns when it pushes an origin ref while the local branch holds
commits that ref lacks, so nothing is dropped silently.

Lives in either `~/.config/jailbee/global.yaml` (user-wide) or
`<repo>/.jailbee/config.yaml` (per-repo). The repo file overrides the
global file via the standard deep-merge pipeline.

Example (`~/.config/jailbee/global.yaml`):

```yaml
push:
  default_action: merge
  default_source: current
```

With the above, `jailbee git push feat-foo` runs `git merge` in the
container using the host's currently checked-out branch as the
source — no prompts, and no fetch (`current` implies the local ref).

### `ls:` / `dashboard:` — remembered columns

Which columns `jailbee ls` and the dashboards show, by default.

```yaml
ls:
  fields: null      # explicit ordered list, or null for the built-in default
  hide: []          # subtractive; applies only when `fields` is null
```

`fields`, when set, wins outright: naming a column is a request for exactly
that column, in that order, even one that is off by default (`local_diff`,
`local_count`, …) or would otherwise be hidden by a dynamic rule (e.g. `pr`
with no container carrying one). `hide` is subtractive and only prunes the
*built-in* default set — a dynamic rule such as `pr`'s "show only when
something has one" still applies to a hidden-by-config column, unlike
`fields`. This one rule is implemented once, in
`table_format.apply_column_config`, and used by `jailbee ls`; the deprecated
`dashboard:` block followed the same rule for its one-time import into
`view_prefs` (see below).

**The two views have different built-in defaults.** `jailbee ls` is a
one-shot listing and stays narrow: NAME, BASE, STATE, CREATED, NETWORK, WT,
AHEAD ±, ↑, MERGE. The dashboards differ in exactly one column: they add MEM,
because a live number is worth its width in a view that refreshes and is a
stale sample in one that does not. IP is off in both — enable it in the
dashboard settings UI, or ask for it from `ls` with `--fields ip`.

Four columns are dynamic and appear only when they have
something to say: `job` (a background job is running), `ttl` (a container is
in loose mode), `pr` (a container tracks a PR) and `mode` (a mount-mode
container exists — on a clone-only host the column would be a constant).

**Table output only.** `jailbee ls --format json` always emits its own built-in
field set (`FieldSpec.default_json`), regardless of `ls.fields`/`ls.hide` —
a personal display preference in `global.yaml` must never silently narrow
machine-readable output a script depends on. An explicit `--fields` flag on
the command line still wins in **every** format, table or JSON.

Allowed names (also the `jailbee ls --fields` vocabulary): `name`, `full_name`,
`repo`, `mode`, `base`, `state`, `created`, `job`, `network`, `ttl`,
`loose_until`, `ip`, `memory_limit`, `mem`, `wt`, `ahead_diff`,
`ahead_count`, `conflict`, `local_diff`, `local_count`, `git_status`, `pr`.

Three things are problems: an unknown name (reported with the allowed set
listed), `fields: []` (a table with no columns at all — write `fields: null`
if you want the built-in default set back), and the same name twice in
`fields` (it would render that column twice). None of these three is fatal
at load time any more, in either file — a column choice is a personal
display preference, and a typo in it must never break an unrelated command.
Both `~/.config/jailbee/global.yaml` and a repo's `.jailbee/config.yaml` recover the
same way: an unknown name is dropped, a duplicate collapsed to its first
occurrence, and an empty (or emptied-by-dropping) `fields` reset to the
built-in default set (`hide` is never reset this way — an explicitly empty
`hide` is a real value, not the same footgun). The command proceeds with
whatever remains valid, and the fix is printed as a warning naming the file
it came from, so a global-layer fix isn't confused with a repo-layer one.
Either way, `jailbee config validate` is where all three are still reported as
errors, with the allowed set listed for the unknown-name case — the one
command whose job is telling you what's wrong.

`ls:` exists in `~/.config/jailbee/global.yaml` **and** in a repo's
`.jailbee/config.yaml`, merged the same field-by-field way as
`loose_auto_revert`: the repo's block overrides the global one per field
(setting only `hide` in the repo still inherits the global `fields`, and
vice versa). Note the key is **not** part of the general deep-merge
pipeline used by the rest of the file — that pipeline *appends* list
values, which would concatenate the two `fields` lists instead of
replacing one with the other. A repo block that names `fields` replaces the
global list outright; `fields: null` in the repo discards the global list
and restores the built-in default set. Column choice is a personal
preference, so the normal home is `global.yaml`; a repo that sets the block
does so for everyone working in that repo — deliberate, and rare.

`--fields` on the CLI beats both `ls:` blocks outright — this is a
remembered preference, not a lock.

### The dashboards remember their own columns

`jailbee dashboard` and `jailbee gui` do **not** read a `dashboard:` block.
Each remembers its own columns and its own folded repo groups, because a
live view can own the state you are looking at:

- In the TUI, press **F2** (or `S`) for the settings overlay: `↑`/`↓` moves,
  `Space` toggles, `Tab` switches between Fields and Repos, `Esc` closes.
  Changes apply immediately — the table stays on screen behind the panel.
- In the GUI, use **View ▸ Columns**.

The two are independent on purpose: a wide Qt table and a narrow TUI is a
supported setup. State lives in `state.sqlite`'s `view_prefs` table, one row
per front-end — machine-written, so it stays out of your hand-edited config.

Enabling a column means "show it when it has something to say": the four
dynamic columns (`job`, `ttl`, `pr`, `mode`) still appear only when they
apply, and the overlay marks them so. This differs from `ls --fields`, where
naming a column forces it on — there a name is a one-shot request, here it is
a standing preference.

**`dashboard:` is deprecated.** The key is still accepted, so an existing
config keeps loading, but it is ignored: it is imported into each
front-end's own settings the first time you open that dashboard after
upgrading, and can be deleted once both have been opened at least once.
`jailbee config validate` says so. Only `~/.config/jailbee/global.yaml` is
imported this way — the setting is personal and applies in every repo, so a
repo-level `dashboard:` block is reported and dropped rather than seeded.
`ls:` is unaffected and still lives in config.

The Qt dashboard's **Compact** card style is the one exception: it renders a
hardcoded selection — name, state, `mode`/`base`/`network`, a job badge and
a folded `wt`/`ahead_diff`/`ahead_count`/`conflict` summary — so a
configured column outside that set (`local_diff`, say) reaches the tree and
the Grid card style but never Compact. Switch card style to see it.

## Computed attributes

The `Config` object exposes four attributes set at load time, not from
YAML:

- `repo_root` — directory containing `.jailbee/`.
- `upstream_remote` — which of the repo's git remotes jailbee treats as the
  upstream. See [Which remote is the upstream?](#which-remote-is-the-upstream)
  below. Fallback `origin`.
- `default_branch` — auto-detected via
  `git symbolic-ref refs/remotes/<upstream_remote>/HEAD`. Fallback `main`.
- `container_prefix` — defaults to `repo_root.name`, overridable via the
  optional `container_prefix:` YAML key. Used as the prefix for every
  jailbee-owned Incus resource (containers, profiles, ACL).

### Which remote is the upstream?

`origin` is only the name `git clone` picks by default, and `git remote
rename` is an ordinary thing to do. jailbee therefore resolves the name
instead of assuming it, once per invocation, taking the first of:

1. the sole remote, when the repo has exactly one;
2. `origin`, when it exists;
3. `remote.pushDefault`;
4. the current branch's `branch.<branch>.remote`;
5. the one remote carrying a `refs/remotes/<remote>/HEAD` symref — the signal
   that survives both a rename and a branch that was never pushed.

A candidate naming a remote that no longer exists is skipped, so a stale
`remote.pushDefault` cannot win.

`origin` sits ahead of every other signal on purpose: a repo that has one
behaves exactly as it always did. In particular, a fork checkout where
`origin` is your fork and branches track the canonical repo keeps pushing to
the fork.

There is no config key for this — git already holds the answer, and a
submodule may answer differently from its superproject (each is resolved
against its own directory). If jailbee cannot tell, it falls back to the
literal `origin` and `jailbee doctor` reports the ambiguity; disambiguate with
`git config remote.pushDefault <name>` or by giving the branch an upstream.

## `github`

GitHub CLI (`gh`) integration. When enabled, `jailbee`:

- Opens `api.github.com:443` in the strict-mode egress allowlist.
- Injects `GH_TOKEN` into the container at autostart via
  `/etc/profile.d/jailbee-github.sh` so login shells (and AI agents
  launched through them) authenticate without `gh auth login`.
- Runs `jailbee doctor` checks for token presence, perms, and PAT shape.

```yaml
github:
  enabled: true
  api_tokens:
    sampleapp:     github_pat_AAA...   # one entry per GitHub owner
    personal-tool: github_pat_BBB...
```

Keys are `container_prefix` values from `.jailbee/config.yaml`; each
container picks the token matching its prefix. One entry per GitHub
resource owner (fine-grained PATs are scoped per-owner).

**Placement constraint:** the `github` block must live in
`~/.config/jailbee/global.yaml`. Placing it in any repo's
`.jailbee/config.yaml` is rejected at load time — committing a repo file
with a token would leak it.

**Permissions:** when `api_tokens` is non-empty, `~/.config/jailbee/global.yaml`
must be mode `0600`. `jailbee config validate` / `load_config` fail loudly
otherwise; run `chmod 600 ~/.config/jailbee/global.yaml` after editing.

**Token shape:** prefer fine-grained PATs (`github_pat_*`) scoped to
"Only select repositories" with Contents:Read, Issues:RW, Pull
requests:RW, Metadata:Read. Classic PATs (`ghp_*`) get a doctor
warning because they cannot be scoped per-repo.

Field defaults:

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. Opt-in via global.yaml. |
| `api_tokens` | dict[str, SecretStr] | `{}` | Map from `container_prefix` to PAT. Values masked in `repr(cfg)` to avoid accidental log leaks. |

`enabled: true` with empty `api_tokens` is rejected at load time.
A repo whose `container_prefix` is not in `api_tokens` produces an
info-level doctor message ("no token configured") — `gh` still runs
but cannot authenticate, which is the legitimate "this repo doesn't
use gh" state.

## Global config (`~/.config/jailbee/global.yaml`)

Optional. Host-global settings shared across all repos. It is the required
home for the [`github`](#github) block (above) and the usual home for the
opt-in integration blocks (`gpg`, `ssh`, `jetbrains`, `chrome`, `agents`).
`agents:` is valid at both layers, though — see [`agents`](#agents) above —
and a repo entry merges over a global one, so a team default set globally
can still be adjusted per repo.
Two blocks are unique to this file: the Docker registry mirror overrides,
and `claude_credentials` (below).

```yaml
docker_registry_mirror:
  enabled: auto                                  # auto | true | false
  port: 3128                                     # rpardini default
  image: rpardini/docker-registry-proxy:0.6.5    # OCI image pin
  data_dir: ~/.local/share/jailbee/registry          # cache + CA storage
```

| Key | Default | Description |
|---|---|---|
| `enabled` | `auto` | `auto` wires the mirror only into repos that ask for it: a golden image that would contain Docker (`golden.stacks.docker`, an `enable_snippets`/`install.d` `50-docker`, a `golden.extra_apt_packages` entry starting with `docker`, minus `disable_snippets`), a non-empty per-repo [`docker_registry_mirror.extra_registries`](#docker_registry_mirrorextra_registries), or `golden.stacks.ecr` (which stages a Docker credential helper). `true` forces it on, `false` skips all mirror-related work. **Both are host-global** — this file is host-level, so `true` set for one undetectable repo also re-imposes the strict-mode `jailbee new` abort on every other repo on the machine; `extra_registries` is the per-repo way to opt in. Mirror container lifecycle is unaffected either way (use `jailbee registry up/down`). |
| `port` | `3128` | Port the rpardini proxy listens on inside the mirror container. |
| `image` | `rpardini/docker-registry-proxy:0.6.5` | OCI image podman runs inside the mirror Incus container. Pin to a specific tag — upgrades are deliberate. |
| `data_dir` | `~/.local/share/jailbee/registry` | Host directory bind-mounted into the mirror for cache + CA storage. |

`auto` cannot see every route to Docker. A differently-named `install.d`
snippet (`55-docker-ce.sh` resolves to the logical name `docker-ce`, not
`docker`), a custom `golden.provision_script` that installs Docker without
`golden.stacks.docker` being set, and Docker installed by hand inside a running
container are all invisible to it. Those repos need `enabled: true` — or, for
the first two, a declared stack and a golden-image rebuild.

When a repo wants the mirror but the mirror container is stopped or missing,
`jailbee init`, `jailbee apply` and the background egress refresh warn and
continue — the ACL simply omits the mirror rule until a later run finds it
running. `jailbee start` / `jailbee restart` never aborted on this and stay
silent: they skip the `/etc/hosts` mirror pin without comment. `jailbee net
strict` warns, since switching to strict is what removes the container's direct
route to Docker Hub. Only `jailbee new` refuses, and only in strict mode: the
default egress allowlist contains no registry hosts, so there the mirror is the
container's only route to Docker Hub. In loose mode it is a pull cache, so
`jailbee new` warns and proceeds.

The remedy in every case is `jailbee registry up && jailbee apply`. Note that
`apply` only re-pins `/etc/hosts` and re-installs the dockerd proxy on
*running* containers, so a container that was stopped at the time is not fixed
by it — start it and run `jailbee apply` again.

Lifecycle commands: `jailbee registry up`, `jailbee registry down`,
`jailbee registry status` (`running` / `stopped` / `degraded` / `missing`).

### `claude_credentials`

Lets several repos on this host share one Claude Code login. Host-level
only, like `docker_registry_mirror`: setting `claude_credentials` or the
computed `claude_credentials_dir` in a repo's `.jailbee/config.yaml` is
rejected at load time, because a repo config is typically committed and a
group name is a property of this one machine, not the team.

```yaml
claude_credentials:
  group: work                    # default for every repo on this host
  repos:                         # exceptions, keyed by container_prefix
    my-side-project: personal
    solo: null                   # opt this one repo out — keep its own credential
```

| Key | Type | Default | Description |
|---|---|---|---|
| `group` | `str \| None` | `None` (unset); `default` in a freshly generated `global.yaml` | Default credential group for every repo on the host. Absent means no sharing. |
| `repos` | `dict[str, str \| None]` | `{}` | Per-repo override keyed by `container_prefix`. Wins over `group`, **including when the value is `null`** — that is the only way to keep one repo on its own credential while the rest of the host shares one. |

A group name must match `[a-z0-9][a-z0-9-]*`: it becomes a directory name
under `<xdg_data_home>/jailbee/claude-credentials/<group>/`.

**New hosts share by default.** `jailbee config init --global` writes
`claude_credentials: {group: default}` into the generated `global.yaml`, so
every repo on a fresh host shares one login without any configuration: the
first `/login` in any container lands in the group directory, and the next
repo is already logged in. The *schema* default is still `None` — an
existing `global.yaml` that predates the key keeps every repo on its own
credential, and `write_global_template` refuses to overwrite an existing file
without `--force`. That asymmetry is deliberate: turning sharing on for a
host that already has several logged-in repos means answering the
two-credential prompt below on every repo but the first, which is a
migration, not a default. To opt a whole host out, set `group: null`.

Only the *credential* is shared — each repo keeps its own `~/.claude`, so
project history, MCP config, sessions and onboarding state never cross
repos. See [Shared credential groups](agents.md#shared-credential-groups-claude_credentials)
in `agents.md` for the mechanism.

Joining a group requires `jailbee apply`: it creates the group directory
(mode `0700`) and **moves** this repo's `.credentials.json` into it.

If both the group directory and this repo already hold a credential, only
one of the two logins can be shared and the other becomes unused, so
`apply` asks which to keep:

* **the group's login** — this repo's copy is deleted; the repo adopts the
  account every other member already uses. This is the usual answer.
* **this repo's login** — the group's copy is deleted and this repo's is
  moved in, which re-points *every* member repo at this account.
* **cancel** — nothing changes and `apply` aborts. To keep this repo on
  its own login instead, add it under `repos:` as `null` (the prompt prints
  the exact block) and re-run `apply`.

The losing credential is deleted, not archived. The two are *independent*
grants — two `/login`s to one account each mint their own refresh-token
lineage — so removing one never disturbs the survivor, and a stale
credential left in the shared tree is read by nothing. Restoring it means
one `/login`. Without a TTY to ask on (a piped or CI `apply`), the prompt
is skipped and `apply` refuses instead, changing nothing.

Every *successful* join leaves
this repo's own config home with no `.credentials.json` of its own — either
it had none to begin with, or the move took it. There is no restore-on-leave:
leaving a group (remove the key, re-run `apply`) unmounts the shared
directory and the repo's config home is still empty, so the container finds
no credential and needs one `/login`. This is deliberate — moving a
credential back on leave would have to guess which of several repos that
have been sharing it should get it, and a `/login` is cheap.
`jailbee doctor` names the group, its directory, and the other member
repos.

If the file is absent, defaults apply silently. Invalid YAML → error.

## `--config / -c` override

`jailbee -c /path/to/config.yaml <subcommand>` bypasses discovery and uses
the given path. `repo_root` is derived as the path's grandparent (i.e.
the path is assumed to end in `.jailbee/config.yaml`). The flag is intended
for tests and edge cases — odd paths produce odd `repo_root` values
without complaint.
