# JailBee configuration schema — full reference

Distilled from `docs/config.md` of the JailBee repo. Read this when SKILL.md doesn't cover a field you need.

## File locations

- Per-repo: `<repo>/.jailbee/config.yaml` (required for `jailbee` to work in that repo)
- Global: `~/.config/jailbee/global.yaml` (optional, user-level, applies to every repo)

Both files are deep-merged at load time. Repo wins on scalars, repo list appends to global list (`[]` resets), dicts merge recursively per key.

## Top-level keys

| Key | Type | Default | Where it lives by convention |
|---|---|---|---|
| `container_user` | `{uid, gid}` | host uid/gid | global |
| `container` | `{env}` | `env: {}` | repo |
| `shared_dir` | path | `~/.local/share/jailbee/shared/<repo-name>` | repo (auto-derived) |
| `gpg` | `{enabled}` | `enabled: false` (opt-in) | global |
| `ssh` | `{enabled, seed_from_host}` | `enabled: false`, `seed_from_host: true` | global |
| `jetbrains` | `{enabled, ide, userprefs_from_host, autostart, toolbox_host_path}` | `enabled: false` (opt-in), rest see below | mixed (see per-key table) |
| `chrome` | `{enabled, url, dark_mode, autostart, host_path}` | `enabled: false` (opt-in), rest see below | mixed (see per-key table) |
| `host_mounts` | list of `{host, container, readonly}` | `[]` | global for personal, repo for stack |
| `optional_mounts` | dict of name → `{host, container, readonly, description}` | `{}` | repo |
| `host_devices` | list of `{path, source, type, mode, gid, uid, group}` | `[]` | repo |
| `host_ports` | list of `{name, port, host_port, proto, host_address, container_address}` | `[]` | repo |
| `shared_caches` | list of `{name, host_subpath, container_path}` | bundled defaults (see below) | repo (rarely overridden) |
| `share_local` | bool | `true` | repo |
| `egress_allow` | list of strings | `[]` | global for cross-cutting, repo appends |
| `defaults` | `{memory, cpu, network, storage_pool}` | `16GiB/8/strict/default` | repo |
| `golden` | see below | see below | repo |
| `autostart` | see below | empty triggers | repo |
| `docker_registry_mirror.extra_registries` | list of `host[:port]` | `[]` | repo |
| `container_prefix` | string | `repo_root.name` | repo (only if name doesn't match regex) |
| `agents` | dict of name → `{enabled, autostart, command, install, install_check, update, auto_update, install_network, shared, egress_allow, env}` | `{}` (six presets available: `claude`, `codex`, `gemini`, `aider`, `opencode`, `grok`) | global for the master switch, repo appends |
| `claude` | **legacy alias for `agents.claude`** — same fields, plus Claude-only ones (`plugins_enabled`, `install_jailbee_skills`, `ai_pr_description`, `ai_pr_branch`, `ai_pr_model`, `pr_prompt`, `ai_pr_timeout`) | `enabled: false`, rest see below | global (`pr_prompt` belongs in the repo) |
| `github` | `{enabled, api_tokens}` | `enabled: false` (opt-in) | global |
| `terminal` | `{kitty: {enabled, host_terminfo_path}}` | `kitty.enabled: "auto"` | global |
| `loose_auto_revert` | `{enabled, after}` | `enabled: true`, `after: "5m"` | global/repo |
| `ls` / `dashboard` | `{fields, hide}` | `fields: null`, `hide: []` (`dashboard.hide` defaults to `[repo, full_name, git_status, created, ttl]`) | global (personal display preference) |
| `pull` | `{destroy_container, delete_branch}` | both `prompt` | mixed |
| `confirm` | `{auto_target}` | `auto_target: true` | global (personal preference) |
| `push` | `{default_action, default_source, push_from, autofetch}` | `ask` / `base` / `origin` / `true` | mixed |
| `new` | `{clone_from, autofetch, background, submodules}` | `origin/true/false/true` | repo |
| `destroy` | `{background}` | `background: false` | repo |
| `after_new` | enum `shell\|tmux\|none` | `none` | repo |

The schema is **fail-closed** — unknown keys are rejected at load. An empty file (`{}`) is valid and yields full defaults.

## `container_user`

```yaml
container_user:
  uid: 1000
  gid: 1000
```

UID/GID of the `dev` user inside the container. Default = host uid/gid so bind-mounted files are readable. Username is hardcoded to `dev` (not configurable).

## `container`

```yaml
container:
  env:
    NODE_OPTIONS: "--max-old-space-size=4096"
    EXAMPLE_VAR: "bar"
```

Container-wide settings applied via the base Incus profile.

- `env` (map, default `{}`) — env vars injected into every process Incus starts in the container: `jailbee shell`, `jailbee tmux`, autostart steps, and any nested tmux/shell window. Values are passed through verbatim (no shell expansion). Key names must match `[A-Za-z_][A-Za-z0-9_]*`. Per-step `autostart.env` and step `env` override on collision.

`jailbee apply` re-applies the profile and prompts to restart running containers so they pick up the new env.

## `shared_dir`

Host path bind-mounted into every container under `/mnt/shared`. Holds caches, JetBrains config, Chrome pool, Claude state. A `.owner` stamp file is written here on first `jailbee init`; two repos with conflicting `shared_dir` paths fail on the second `jailbee init`.

## `host_mounts`

```yaml
host_mounts:
  - { host: ~/.gnupg, container: /home/dev/.gnupg, readonly: true }
```

Each entry is bind-mounted into every container. `readonly: false` is allowed but rare for credentials.

## `optional_mounts`

```yaml
optional_mounts:
  aws:
    host: ~/.aws
    container: /home/dev/.aws
    readonly: true
    description: "AWS creds for ECR pulls"
```

Named, not attached by default. Attach per-container with `jailbee new --mount aws` or per-autostart-step with `mounts: [aws]`. `description` shows in `jailbee new --help` listings.

## `host_devices`

```yaml
host_devices:
  - { path: /dev/kvm }                        # Android emulator / KVM VMs
  # - { source: /dev/bus/usb/001/004, path: /dev/bus/usb/001/004 }
```

Pass arbitrary host character/block devices into every container as Incus `unix-char` / `unix-block` devices (the same mechanism as the GPU render-node passthrough). Fields: `path` (in-container, absolute, required), `source` (host path, absolute, defaults to `path`), `type` (`unix-char` default, or `unix-block`), `mode` (octal string, default `"0666"`), `gid`/`uid` (Incus profile-device owner), `group` (container group the `dev` user is added to; auto-derived from the host node when unset). Layered like `host_mounts`; `[]` resets.

A device whose host `source` is absent is **skipped** (JailBee does not fail); `jailbee config validate` reports it as an advisory, so a team-shared config still works on hosts lacking the device.

**Access is via group membership, not `mode`.** Devices with a udev `static_node` rule (`/dev/kvm`, `/dev/net/tun`, `/dev/fuse`, …) get reset to their distro default (e.g. `root:kvm 0660`) by the container's `systemd-udevd` on every boot, overriding the profile `mode`. So JailBee adds the `dev` user to the device's owning group — auto-derived from the host node (`/dev/kvm` → `kvm`), or set `group:` to override. Applied on `jailbee new` and `jailbee apply`; a new `jailbee shell`/`jailbee tmux` session picks the group up (an already-open shell must be reopened — check with `id`). `mode`/`gid`/`uid` still apply to the profile device and matter for non-`static_node` devices (e.g. render nodes keep `0666`).

**Security:** opt-in, default empty. Containers are unprivileged, so `/dev/kvm` doesn't grant escape on its own, but each device widens the host-kernel attack surface (KVM ioctls run in host-kernel context). List only what the repo needs.

## `host_ports`

```yaml
host_ports:
  - { name: adb, port: 5037 }                  # host adb server, reachable inside
  # - { name: api, port: 3000, host_port: 9000 }
```

Make a host TCP/UDP service reachable **inside** every container of the repo. Each entry becomes one Incus `proxy` device named `port-cfg-<name>`: the container listens on `container_address:port` and Incus's forkproxy connects to `host_address:host_port` on the host. So `port`/`container_address` describe the container-side listener, `host_port`/`host_address` the host service it reaches. Fields: `name` (handle, `[a-z0-9][a-z0-9-]*`, max 40 chars, unique — also the `jailbee port rm` key), `port` (container-side, 1–65535, required), `host_port` (defaults to `port`), `proto` (`tcp` default, or `udp`), `host_address` (default `127.0.0.1`), `container_address` (default `127.0.0.1`). Both addresses must be IP literals — a hostname is rejected rather than resolved once and pinned into the device. Layered like `host_mounts`; `[]` resets.

**Only this direction is configurable.** A host-side listener is machine-wide, so a repo declaring one would make every branch container of that repo fight over the same host port. The reverse — a container service reachable on the host — is `jailbee port to-host`, run per container. A `direction:`/`to_host:`/`bind:` key in an entry is rejected with that explanation, not a generic "unknown field".

Entries are attached by `jailbee new` and reconciled by `jailbee apply` (added / replaced / removed to match the config) — proxy devices hotplug, so no image rebuild and no restart. Reconciliation only touches `port-cfg-*` devices; a forward added by hand with `jailbee port` is left alone.

**Security:** a forward is a hole through the `net strict` ACL's egress half by construction — forkproxy connects straight out of the container's netns, so the bridge ACL never sees the traffic. Opt-in, default empty; list only what the repo's workflow needs.

## `shared_caches`

Bind-mounts from `<shared_dir>/<host_subpath>` into the container at `container_path`. `container_path` may start with `~` (expands to `/home/dev`). Default set:

| name | host_subpath | container_path |
|---|---|---|
| `pnpm-store` | `caches/pnpm-store` | `~/.local/share/pnpm/store` |
| `gradle` | `caches/gradle` | `~/.gradle` |
| `npm` | `caches/npm` | `~/.npm` |
| `m2` | `caches/m2` | `~/.m2` |
| `jetbrains-config` | `jetbrains-config` | `~/.config/JetBrains` |
| `jetbrains-data` | `jetbrains-data` | `~/.local/share/JetBrains` |
| `ssh` | `ssh` | `~/.ssh` |

> The claude shared caches are *not* defaults; they are appended by
> `Config.effective_shared_caches()` when `claude.enabled: true`. See
> `## claude` below.

Override with `shared_caches: [...]` to replace entirely, or `shared_caches: []` to disable. `name` must match `[a-z0-9][a-z0-9-]*` and be unique. `container_path` must be absolute or start with `~`.

## `egress_allow` — strict-mode allowlist

Entry forms (six variants):

| Form | Meaning |
|---|---|
| `<hostname>` | All TCP ports to IPv4(s) of that hostname |
| `<hostname>:<port>` | Single TCP port to that hostname |
| `<ipv4>` | All TCP ports to that IPv4 |
| `<ipv4>:<port>` | Single TCP port to that IPv4 |
| `<cidr>` | All TCP ports to that CIDR |
| `<cidr>:<port>` | Single TCP port to that CIDR |

Resolution: hostname entries are DNS-resolved to IPv4 at `jailbee init` / `jailbee apply` time. All A records returned are inserted. If a CDN rotates, `jailbee apply --no-restart` re-resolves and updates the ACL live.

Errors: if any hostname fails to resolve, the entire apply aborts (non-zero exit); previous ACL stays. Treat broken entries as real errors.

Limitations: IPv6 unsupported (`:` clash). UDP egress beyond DNS to the bridge is not user-configurable.

`loose` network mode ignores this list (everything reachable).

## Network modes

Two hardcoded modes, not user-extendable:

| Mode | Behaviour |
|---|---|
| `strict` | Default-deny ACL on `incusbr0`. Only `egress_allow` destinations reachable. |
| `loose` | Wider egress on dedicated `jailbee-loose` bridge. |

Per-container default: `defaults.network`. Per-step override: `autostart.<trigger>[].network` (restored after the step).

## `defaults`

```yaml
defaults:
  memory: 16GiB
  cpu: 8
  network: strict
  storage_pool: default
```

`storage_pool` is the Incus storage pool name. Stick to `default` unless the host has multiple pools.

## `golden`

```yaml
golden:
  alias: ""                       # defaults to <container_prefix>-base
  ubuntu_version: "26.04"
  java: amazon-corretto-17
  node: 24
  # (no `python:` key — the container Python is the base image's system
  #  python3, determined by ubuntu_version; a stale `python:` is ignored
  #  with a soft deprecation warning)
  provision_script: null          # null = bundled install.sh
  provision_env: {}
  extra_apt_packages: []
  disable_snippets: []
  enable_snippets: []             # low-level: opt-in snippets from install.d.available/ (by name)
  stacks: {}                      # recommended: see "Stacks" section below
```

- `java` / `node`: version pins. Only take effect once the matching snippet is staged (via `stacks` or `enable_snippets`). `amazon-corretto-N` maps to apt package `java-N-amazon-corretto-jdk`; anything else is passed through as a literal apt package name.
- `provision_script`: relative paths resolve against repo root. When set, `install.d/` snippets are not staged.
- `provision_env`: extra env vars for the provisioning script. Reserved keys (`CONTAINER_UID`, `CONTAINER_GID`, `JAVA_PACKAGE`, `NODE_MAJOR`, `PYTHON_VERSION`, `EXTRA_APT_PACKAGES`, `JAILBEE_USER_HOME`, `JAILBEE_PROVISION_DIR`) raise `ConfigError`.
- `extra_apt_packages`: validated against `[a-z0-9][a-z0-9+\-.]*` (Debian package grammar).
- `disable_snippets`: names to drop from the resolved snippet set — logical name (`"registry-mirror-ca"`), numbered name (`"90-registry-mirror-ca"`), or full filename (`.sh` optional); also drops snippets `stacks` auto-added.
- `enable_snippets`: low-level escape hatch. Names of opt-in snippets from `install.d.available/` to install (friendly name like `nodejs`/`docker`, or full filename `30-nodejs`); unioned with the snippets `stacks` implies; complements `disable_snippets`.
- `stacks`: recommended high-level toggles — see below.

## Stacks (`golden.stacks`)

The recommended way to enable a language runtime or cloud helper — one
field pins the version *and* stages the snippet, plus the shared caches
and `JAVA_PACKAGE`/`NODE_MAJOR` build-env values it needs:

```yaml
golden:
  stacks:
    java: corretto-17    # or "openjdk-17" | true (→ default-jdk, openjdk snippet)
    node: 24              # or true (→ major 24)
    python: true
    docker: true
    ecr: true
```

| Key | Type | Values | Effect |
|---|---|---|---|
| `java` | bool \| string | `false` (default) \| `true` \| `"openjdk-N"` \| `"corretto-N"` | `true`/`"openjdk-N"` stage `20-openjdk` (apt `default-jdk`/`openjdk-N-jdk`); `"corretto-N"` stages `20-corretto` (apt `java-N-amazon-corretto-jdk`). Adds the `gradle`/`m2` shared caches. |
| `node` | bool \| int | `false` (default) \| `true` \| `N` | Stages `30-nodejs`; `NODE_MAJOR` is `N`, or `24` when `true`. Adds the `npm`/`pnpm-store` shared caches. |
| `python` | bool | `false` (default) \| `true` | Stages `40-python`. |
| `docker` | bool | `false` (default) \| `true` | Stages `50-docker`. |
| `ecr` | bool | `false` (default) \| `true` | Stages `80-ecr-helper`. |

`java` + `docker` together auto-stage `90-registry-mirror-ca` (imports
the registry mirror's CA into the JDK truststore) — opt out with
`golden.disable_snippets: ["90-registry-mirror-ca"]`.

`golden.enable_snippets`/`disable_snippets`/`shared_caches` (and bare
`golden.java`/`golden.node` version pins) remain available directly as
the low-level escape hatch for anything `stacks` doesn't cover.

## Master switches: every host-tooling block is opt-in

`gpg`, `ssh`, `jetbrains`, and `chrome` all ship with `enabled: false`. A blank or `{}` config has none of them turned on — the container is minimal until the user explicitly enables an integration, typically in `~/.config/jailbee/global.yaml`. The `jailbee config init --global` template flips all four to `enabled: true` to give new users a working starting point.

## `gpg`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | RO bind-mount `~/.gnupg`, attach the `gpg-socket` runtime device, and set `SSH_AUTH_SOCK` in the base profile. `false` skips all three and the doctor `gpg-agent socket` check. |

## `ssh`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Bind-mount `<shared_dir>/ssh` as `~/.ssh` and enforce `0700` on each `jailbee init`. |
| `seed_from_host` | bool | `true` | First-init copy of host `~/.ssh/{config, known_hosts, config.d/}`. Keys never seeded. Ignored if `enabled: false`. |

## `jetbrains`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. When `false`, `jailbee ide` exits 2, autostart skips the IDE launch, the userprefs / toolbox auto-mounts are omitted, and JetBrains license hosts are not auto-added to `egress_allow`. |
| `ide` | enum | `idea` | Which JetBrains binary `jailbee ide` (no `--app`) and autostart launch. Supported: `idea \| webstorm \| pycharm \| goland \| clion \| phpstorm \| rider \| rubymine \| datagrip \| rustrover \| aqua \| dataspell`. |
| `userprefs_from_host` | bool | `true` | RW bind-mount `~/.java/.userPrefs/jetbrains/`. Auto-extends strict-mode `egress_allow` with JetBrains license hosts. Ignored when `enabled: false`. |
| `autostart` | bool | `false` | Launch the IDE after autostart steps. No-op without a graphical session or when `enabled: false`. |
| `toolbox_host_path` | path \| null | `~/.local/share/JetBrains/Toolbox` | Host path RO-mounted to `/opt/jetbrains-toolbox`. `null` disables the auto-mount. Ignored when `enabled: false`. |

When `userprefs_from_host` is on, `egress_allow` is auto-extended (in strict-mode ACL only — YAML field unchanged) with `account.jetbrains.com`, `data.services.jetbrains.com`, `plugins.jetbrains.com`, `download.jetbrains.com` (all port 443).

## `chrome`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. When `false`, `jailbee chrome` exits 2, autostart skips the Chrome launch, and the `host_path` auto-mount is omitted. |
| `url` | string \| null | `null` | URL Chrome opens. `jailbee chrome <name> <URL>` overrides. |
| `dark_mode` | bool | `false` | Pass `--force-dark-mode --enable-features=WebContentsForceDark` regardless of host GTK theme. |
| `autostart` | bool | `false` | Launch Chrome after autostart steps. Ignored when `enabled: false`. |
| `host_path` | path \| null | `/opt/google/chrome` | Host path RO-mounted to `/opt/google/chrome` (container-side path is hardcoded — `gui.open_chrome` invokes `/opt/google/chrome/google-chrome`). Override for non-standard installs (chromium etc.); `null` disables the auto-mount. Ignored when `enabled: false`. A manual `host_mounts` entry with `container: /opt/google/chrome` wins. |

## `autostart`

IDE and Chrome launches are controlled by `jetbrains.autostart` / `chrome.autostart` (not here). The `autostart` block describes the in-container shell steps.

```yaml
autostart:
  on_create: []         # list of Step
  on_start: []          # list of Step
  step_timeout: 600     # default per-step timeout in seconds
  env: {}               # global env merged into every step
```

Step schema:

```yaml
- name: dev                    # required, unique within trigger
  run: "pnpm dev"              # required, shell command as dev user
  network: null                # strict|loose|null (null keeps current)
  mounts: []                   # optional_mounts keys to attach for this step
  env: {}                      # per-step env (merged on top of autostart.env)
  working_dir: ""              # relative to repo_dir; empty = repo_dir itself
  background: false            # detach in tmux window; don't wait
  timeout: null                # per-step override; null = step_timeout
  continue_on_error: false     # non-zero exit warns instead of aborting
```

Steps run inside a container-local tmux session named `autostart`. Attach with `jailbee tmux <container>`. Sync steps' output stays visible after completion (`remain-on-exit on`). Background steps keep running until the container stops.

**Source, in clone mode:** `jailbee new <branch>` reads this block from the target branch's committed `.jailbee/config.yaml` at the commit it clones, not from the operator's checkout. Every other key in this document stays operator-controlled regardless of branch. A branch step that widens `network` to `loose` prompts for confirmation (`--yes` skips); no committed config, or one that fails validation, falls back to the operator's own autostart. See `docs/config.md#where-does-the-autostart-config-come-from` in the JailBee repo.

## `push`

Policy for `jailbee git push`'s interactive default-picker. Layered: `~/.config/jailbee/global.yaml` sets a user-wide default; a repo's `.jailbee/config.yaml` may override.

```yaml
push:
  default_action: ask    # "merge" | "rebase" | "plain" | "ask"
  default_source: base   # "default-branch" | "current" | "base" | "ask"
  push_from: origin      # "origin" (default) | "local"
  autofetch: true        # fetch origin/<source> on the host first
```

`default_source: "base"` (the default) resolves to each container's recorded base branch (`user.jailbee.base_branch`); `"default-branch"` always uses the repo's default branch regardless of the container's base; `"current"` uses the host's currently checked-out branch; `"ask"` opens an interactive picker every time. `default_action` defaults to `"ask"`; `"plain"` is transport-only (no merge/rebase applied in the container).

`default_source` picks *which branch*, `push_from` picks *which copy of it*. With `push_from: origin` + `autofetch: true` (the defaults) the host fetches and pushes `refs/remotes/origin/<source>`, mirroring `new.clone_from: origin` — a local `refs/heads/<base>` only advances on `git pull`, so after a plain `git fetch` it is the stale copy, and pushing it force-moves the container's `refs/jailbee/base/<base>` anchor backwards. `push_from: local` restores the reverse order. Per-invocation overrides: `--from-local`, `--from-origin`, `--fetch`/`--no-fetch`. `--current` always resolves locally; `--pr` ignores these keys entirely and pushes `refs/jailbee/pr/<N>/head`.

## `new`

Defaults for `jailbee new`.

```yaml
new:
  clone_from: origin    # "origin" (default) | "local"
  autofetch: true       # git fetch before cloning so origin/<default> is current
  background: false     # provision detached by default (like always passing -b)
  submodules: true      # bring the repo's submodules along into the clone
```

- `clone_from`: `origin` clones the **upstream** tip (`origin/<default>`); `local` clones your possibly-stale local branch.
- `autofetch`: when `origin`, fetch first so the clone is current. `--no-autofetch` skips per-invocation.
- `background`: makes detached provisioning the default; `jailbee new --no-background` forces foreground for one run.
- `submodules`: `false` skips submodule handling entirely.

## `destroy`

```yaml
destroy:
  background: false     # detached destroy by default (like always passing -b)
```

`jailbee destroy --no-background` forces foreground for one run.

## `ls` / `dashboard`

Which columns `jailbee ls` and the dashboards (`jailbee dashboard`, `jailbee gui`) show
by default.

```yaml
ls:
  fields: null      # explicit ordered list, or null for the built-in default
  hide: []          # subtractive; applies only when `fields` is null

dashboard:
  fields: null
  hide: [repo, full_name, git_status, created, ttl]   # dashboard's built-in default
```

`fields`, when set, wins outright and may name a column that's off by
default (`local_diff`, `local_count`) or would otherwise be hidden by a
dynamic rule (e.g. `pr` with nothing open) — naming a column is a request
for exactly that column. `hide` only prunes the *built-in* default set.

Allowed names: `name`, `full_name`, `repo`, `mode`, `base`, `state`,
`created`, `job`, `network`, `ttl`, `loose_until`, `ip`, `memory_limit`,
`mem`, `wt`, `ahead_diff`, `ahead_count`, `conflict`, `local_diff`,
`local_count`, `git_status`, `pr`. Three things are problems: an unknown
name (reported with the allowed set listed), `fields: []` (no columns at
all — use `fields: null` for the built-in default set), and a name repeated
inside `fields`. None of these is fatal at load time, in either file — a
column choice is a personal display preference, and a typo must never break
an unrelated command. Both `~/.config/jailbee/global.yaml` and a repo's
`.jailbee/config.yaml` recover the same way (unknown name dropped, duplicate
collapsed, an empty/emptied `fields` reset to the default set — `hide`
itself is never reset, since an explicitly empty `hide` is a real value) and
print a warning naming the file it came from. `jailbee config validate` is
where all three are still errors, for both files.

Applies to **table** output only — `jailbee ls -o json` always emits its own
built-in field set regardless of this block, so a personal display
preference can't silently narrow a script's expected JSON shape. An
explicit `--fields` flag on the CLI beats this block in every format.

Both blocks live in either layer, merged field-by-field like
`loose_auto_revert` — *not* through the general deep-merge pipeline, which
appends lists and would concatenate the two `fields` lists. A repo `fields`
replaces the global one; `fields: null` in the repo restores the built-in
default set. `hide` replaces rather than extends at either layer, so
`dashboard: {hide: [ip]}` brings REPO / FULL NAME / GIT STATUS / CREATED /
TTL back — copy the default list above and append if you meant "one more".
Column choice is a personal preference, so the normal home is
`~/.config/jailbee/global.yaml`; setting the block in a repo's
`.jailbee/config.yaml` overrides it for everyone working there — deliberate and
rare. The dashboards render one shared table across every repo, so they
resolve `dashboard:` against the repo you launched from, falling back to
the global file when there is none. The Qt dashboard's Compact card style
renders a hardcoded field selection and ignores `fields` — use another card
style to see a configured column such as `local_diff`.

## `pull`

Post-merge cleanup policy for `jailbee git pull`. Each step is `prompt` (ask, the default), `always`, or `never`.

```yaml
pull:
  destroy_container: prompt    # destroy the container after a successful merge?
  delete_branch: prompt        # delete the merged host branch after the merge?
```

`jailbee git pull --cleanup` forces both; `--no-cleanup` skips both, overriding this block.

## `confirm`

Confirmation for `push`/`pull`/`checkout` when JailBee picks the target container
itself (exactly one exists, no name given) rather than the user naming it or
picking it from a list.

```yaml
confirm:
  auto_target: true    # ask before push/pull/checkout when only one container exists
```

With `confirm.auto_target` on (the default), the command prints a plan block
— both branch names, both tips, the action — and asks `[Y/n]` before doing
anything. `--confirm` / `--no-confirm` override it per invocation. Off a TTY,
`pull` and `checkout` print the plan block and only skip the prompt; `push`
needs an explicit container name off a TTY in the first place, so it never
reaches this confirmation there.

## `loose_auto_revert`

Controls how `jailbee net loose` reverts to the previous mode.

```yaml
loose_auto_revert:
  enabled: true    # auto-revert loose → previous mode after the TTL
  after: "5m"      # duration string ("5m", "30s", "1h") or bare integer minutes
```

`after` is only the **default** TTL: `jailbee net loose <name> --for <dur>` sets it per invocation (`30s`/`45m`/`4h`, max 24h, or `never`), and `--no-revert` stays loose indefinitely. Given neither flag, JailBee asks on a TTY (with `JAILBEE_NONINTERACTIVE` unset and `enabled: true`) and otherwise applies `after` silently. `enabled: false` means JailBee schedules no TTL and never asks — an explicit `--for` is still honoured. `jailbee ls` shows the remaining TTL while any container is loose.

## `share_local`

```yaml
share_local: true
```

When `true` (default) and a `.local/` directory exists at the repo root, it's bind-mounted RW into the clone at `~/<container_prefix>/.local` — a git-excluded host⇄container scratch channel. Set `false` to disable.

## `after_new`

```yaml
after_new: none        # "none" (default) | "shell" | "tmux"
```

What `jailbee new` does after a successful provision: `none` returns to the host prompt; `shell` opens an interactive bash login shell in the container; `tmux` attaches the autostart tmux session (creating it on demand). Override per-invocation with `jailbee new --attach <mode>`, the `--tmux` / `--shell` shorthands, or `jailbee new --no-attach`. `--attach shell`/`--attach tmux`, `--tmux`, and `--shell` force foreground provisioning (overriding `new.background`); `--attach none` / `--no-attach` don't, since there's nothing to attach to, and — like those — this config default yields silently to a background run rather than erroring.

## `github`

GitHub CLI (`gh`) integration inside containers.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. When `true`: auto-adds `api.github.com:443` to strict-mode egress, writes `/etc/profile.d/jailbee-github.sh` (exports the token) at autostart, and enables the `github` doctor checks. The `gh` binary is always in the golden image regardless. |
| `api_tokens` | map `container_prefix` → PAT | `{}` | Fine-grained personal access token per GitHub resource owner (org or account). Values are `SecretStr` (masked in config dumps). **Permitted only in `~/.config/jailbee/global.yaml`** — `load_config` rejects it in the per-repo file (it would be committed to git). |

Even with a token, GitHub network calls from inside a container still need **loose** mode (the strict allowlist deliberately omits `github.com`). See the strict gate in the jailbee-usage skill.

## `terminal`

Terminal-emulator integrations. Currently only kitty.

```yaml
terminal:
  kitty:
    enabled: auto              # "auto" (default) | true | false
    host_terminfo_path: null   # explicit host path, or null to autodetect
```

When you run `jailbee shell`/`jailbee tmux` from a kitty terminal, `TERM=xterm-kitty` propagates into the container but the base image lacks that terminfo entry (curses tools warn and degrade). This block RO bind-mounts the host's `xterm-kitty` terminfo file into every container so the entry resolves.

- `kitty.enabled`: `"auto"` activates iff the host terminfo file is found; `true` activates and **fails validation** if none is found; `false` disables.
- `kitty.host_terminfo_path`: explicit path, else autodetect probes `/usr/share/terminfo/x/xterm-kitty`, `~/.local/kitty.app/lib/kitty/terminfo/x/xterm-kitty`, `~/.terminfo/x/xterm-kitty` in order.

## `container_prefix`

Defaults to `repo_root.name`. Must match `[a-z0-9][a-z0-9-]*`. Override when the directory has uppercase, dots, or underscores. Prefix appears on every `jailbee`-owned Incus resource (containers `<prefix>-<branch-slug>`, profiles `<prefix>-net-strict`, ACLs `<prefix>-strict`).

## `docker_registry_mirror.extra_registries`

```yaml
docker_registry_mirror:
  extra_registries:
    - 803520778560.dkr.ecr.eu-north-1.amazonaws.com
```

Bare hostnames optionally with `:port`. No scheme, no path. Pushes into rpardini mirror's `REGISTRIES` env on next `jailbee new` / `jailbee apply`; second pull hits the cache.

## `agents`

Generic hook for terminal coding agents. A mapping keyed by agent name
(`{codex: {...}, gemini: {...}}`), not a list — this is what lets the repo
layer tweak one field of an agent the global layer already turned on
without the deep-merge pipeline's list-append rule producing a duplicate
entry. Six presets ship built in: `claude`, `codex`, `gemini`, `aider`,
`opencode`, `grok`. **Only `claude` is exercised in production** — the
other five are untested templates, correct as needed.

An agent name matching one of the six presets is deep-merged over that
preset (preset → global.yaml → repo, same append/reset rules as every
other list field); any other name is used as-is with no preset base.

```yaml
agents:
  codex:
    enabled: true
    autostart: true
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. Gates the shared mount, the strict-mode egress add, install/update at `jailbee new` time, and the `jailbee doctor` shared-dir check. |
| `autostart` | bool | `false` | Launch `command` in a background autostart tmux window. Requires `enabled: true`. |
| `command` | string | `""` | The command line the autostart window execs; also the default source for `install_check`. Required when `enabled: true`. |
| `install` | string \| null | `null` | Shell command run at `jailbee new` time when `install_check` fails. |
| `install_check` | string \| null | `null` | Probe deciding install-vs-update. Defaults to `command -v <first token of command>`. |
| `update` | string \| null | `null` | Shell command run at `jailbee new` time when `install_check` succeeds and `auto_update` is true. |
| `auto_update` | bool | `true` | `false` leaves an existing install untouched; a missing one is still installed. |
| `install_network` | `strict` \| `loose` | `strict` | Network mode for the install/update step only. |
| `shared` | list of `{subpath, path, type, seed}` | `[]` | Bind mounts from `<shared_dir>/<subpath>` to `<path>`. `type: dir` (default) or `file`; `seed` (file only) is written once if the target is absent. Share the agent's auth/settings surface only — never a cache, history, log, or a generically-named file like `~/.env`. |
| `egress_allow` | list[string] | `[]` | Strict-mode allowlist entries added while this agent is enabled. Same grammar as top-level [`egress_allow`](#egress_allow--strict-mode-allowlist). |
| `env` | map[string, string] | `{}` | Env vars passed to the install/update step and the autostart launch step. |

`jailbee config validate` additionally rejects: an agent name outside
`[a-z0-9-]+`; `enabled: true` with an empty `command`; `autostart: true`
without `enabled: true`; and a `shared` subpath colliding with a built-in
shared subdir or with a different mount target another agent already
claimed.

A full custom (non-preset) entry:

```yaml
agents:
  my-agent:
    enabled: true
    autostart: true
    command: my-agent
    install: "npm i -g my-agent-cli"
    update: "npm i -g my-agent-cli@latest"
    shared:
      - { subpath: my-agent, path: "~/.config/my-agent" }
    egress_allow:
      - api.my-agent.example:443
```

Full field-by-field detail, the preset table, and the "which paths to
share" rule live in `docs/agents.md` of the JailBee repo — read that if
this summary doesn't cover what you need.

## `claude`

**`agents.claude` is the preferred spelling of this block.** A top-level
`claude:` block is still accepted as a **legacy alias**: it is translated
into `agents.claude` at config-load time, before validation. Defining
**both** `claude:` and `agents.claude` in the merged config (global + repo
combined) is a `ConfigError` naming both spellings — pick one, and prefer
`agents.claude`. Everything below applies identically under either
spelling, and `claude` also carries every generic field from the `agents`
table above (`install`, `update`, `install_check`, `install_network`,
`shared`, `egress_allow`, `env`), not repeated here.

Claude Code CLI integration. Defaults to disabled — opt-in via
`~/.config/jailbee/global.yaml`.

| Key | Type | Default | Description |
|---|---|---|---|
| `claude.enabled` | bool | `false` | Master switch. When `true`, JailBee mounts `<shared_dir>/claude` → `~/.claude`, `<shared_dir>/claude.json` → `~/.claude.json`, and `<shared_dir>/claude-install` → `~/.local/share/claude` as shared caches, auto-extends strict-mode `egress_allow` with `api.anthropic.com:443` + `code.claude.com:443` + `claude.ai:443` + `downloads.claude.ai:443` (the last two cover the `install.sh` bootstrap and the native CLI's self-update), creates an empty `<shared_dir>/claude` on `jailbee init`, and includes it in `jailbee doctor` checks. Host `~/.claude` is **not** read — Claude Code runs its onboarding flow inside the first container from a clean state. |
| `claude.plugins_enabled` | bool | `true` | When `true`, `effective_egress_allow` also opens the Claude plugin-marketplace hosts so the in-container CLI can install plugins. |
| `claude.autostart` | bool | `false` | When `true` (and `enabled`), `run_autostart` launches the `claude` CLI as an autostart step. |
| `claude.command` | string | `"claude"` | Command line executed in the `claude` autostart step. |
| `claude.auto_update` | bool | `true` | When `true`, `jailbee new` runs `claude update` inside the container so the CLI is current. |
| `claude.install_jailbee_skills` | bool | `true` | When `true` (requires `enabled`), `jailbee new` and `jailbee apply` copy JailBee's bundled Claude skills (`jailbee-usage`, `jailbee-repo-setup`) into `<shared_dir>/claude/skills/` so the **in-container Claude understands JailBee** and can help edit `.jailbee/config.yaml`. Host-side file copy only, no network. **This is the mechanism that makes these very skills available inside a container** — the container has no `jailbee` binary, so this doc set is its only source of JailBee knowledge. The pre-1.0 name `claude.install_gie_skills` was retired in 1.1.0: a config still using it fails to load with an error naming this key. |
| `claude.ai_pr_description` | bool | `true` | When `true` (requires `enabled`), `jailbee pr` generates a new PR's title and body with the container's Claude CLI (opt out per-invocation with `jailbee pr --no-ai`). |
| `claude.ai_pr_branch` | bool | `true` | When `true` (requires `enabled`), `jailbee pr` asks Claude to propose a convention-following PR head branch name (confirmed interactively; `--as` / `--no-ai` override). |
| `claude.ai_pr_model` | string \| null | `"sonnet"` | Model passed to `claude --model` for PR-text generation. An alias (`sonnet`, `opus`, `haiku`) or a full model ID; `null` inherits the container's own default model. Must be a single whitespace-free token or config load fails. |
| `claude.pr_prompt` | string \| null | `null` | Project-specific PR-writing instructions, embedded in JailBee's prompt as a section that outranks its generic title/body guidance. A repo-level key — this is where a project's PR standard belongs. Max 20 000 characters. |
| `claude.ai_pr_timeout` | int | `600` | Seconds `jailbee pr` gives the in-container Claude to produce the PR text before falling back to a placeholder. Generation is an agentic run over the log, the diff, the PR template, the branch's spec and the CI config — a dozen-plus turns, so cost scales with the repository, not just the diff (109 s in JailBee's own repo on a 21-file diff). Raise it for a large tree, or when `pr_prompt` asks for slower work. Must be positive; to disable generation use `ai_pr_description: false`. |

Example global config:

```yaml
claude:
  enabled: true
```

Example repo-level PR standard in `.jailbee/config.yaml`:

```yaml
claude:
  pr_prompt: |
    Body sections, in this order and with these exact headings:
      ## Why      — the user-visible problem, one paragraph
      ## What     — bullets, each naming the file or symbol it changed
      ## Testing  — the commands you actually ran, verbatim
```

`jailbee pr` already reads `.github/pull_request_template.md`, the spec or
issue the branch implements, and `CONTRIBUTING.md` / `CLAUDE.md` /
`AGENTS.md` on its own. Reach for `pr_prompt` only for rules that live in
none of those files, and never restate the JSON response format in it —
JailBee owns that and states it after the project block.

The claude shared caches are not present in the `shared_caches:` default
list — they are auto-added by `Config.effective_shared_caches()` when
`claude.enabled` is `true`. Manual entries in `shared_caches:` with names
`claude`, `claude-json`, or `claude-install` suppress the auto-add (same
precedent as `effective_host_mounts`).

## install.d snippet resolution

The golden image is **stack-neutral by default**. Bundled snippets are split
into two libraries, both shipped in the wheel, plus two override tiers.
Resolution order (later tier overrides earlier by filename):

1. Bundled, always-on: `src/jailbee/provision/install.d/*.sh` — stack-neutral plumbing only (locale, prompt, GUI libs, GitHub CLI, extra apt packages). No language runtime or cloud helper lives here.
2. Bundled, opt-in: `src/jailbee/provision/install.d.available/*.sh` — language runtimes and cloud helpers, staged via `golden.stacks` (recommended) or named directly in `golden.enable_snippets` (by logical name, e.g. `nodejs`, or full filename, e.g. `30-nodejs`).
3. User: `~/.config/jailbee/install.d/*.sh` — overrides bundled by filename.
4. Repo: `<repo>/.jailbee/install.d/*.sh` — overrides user (and bundled) by filename.

After resolution, names listed in `golden.disable_snippets` are dropped
(suffix `.sh` optional); `disable_snippets` wins over `enable_snippets` for
the same name. Empty (zero-byte) snippet files are skipped at runtime —
useful for low-effort disable via shadow.

### Bundled snippets (`install.d/` — always on)

| Name | Installs | Env consumed |
|---|---|---|
| `05-extra-apt.sh` | `golden.extra_apt_packages` | `EXTRA_APT_PACKAGES` |
| `10-locale.sh` | `en_US.UTF-8` locale | — |
| `15-prompt.sh` | Bash prompt branch indicator (`$JAILBEE_BRANCH`) | `CONTAINER_USER` |
| `60-gui-libs.sh` | JetBrains/Chrome runtime libs + fonts | — |
| `75-github-cli.sh` | GitHub CLI (`gh`) from `cli.github.com` | — |

### Opt-in snippets (`install.d.available/`)

Low-level escape hatch — `golden.stacks` (above) is the recommended way
to enable these. Bundled but off by default; stage one directly by
adding its logical name (or full filename) to `golden.enable_snippets`:

```yaml
golden:
  enable_snippets: [nodejs, docker]
```

| Name | Logical name | Installs | Env consumed |
|---|---|---|---|
| `20-openjdk.sh` | `openjdk` | OpenJDK from the Ubuntu archive | `JAVA_PACKAGE` |
| `20-corretto.sh` | `corretto` | Amazon Corretto JDK | `JAVA_PACKAGE` |
| `30-nodejs.sh` | `nodejs` | Node.js + per-user `~/.npmrc` | `NODE_MAJOR`, `JAILBEE_USER_HOME`, `CONTAINER_USER` |
| `40-python.sh` | `python` | `python${PYTHON_VERSION}` + venv + pip | `PYTHON_VERSION` |
| `50-docker.sh` | `docker` | Docker Engine + AppArmor override; dev user → docker group | `CONTAINER_USER` |
| `80-ecr-helper.sh` | `ecr-helper` | `amazon-ecr-credential-helper` | — |
| `90-registry-mirror-ca.sh` | `registry-mirror-ca` | Imports `/opt/jailbee-mirror-ca.crt` into Java truststore (no-op if absent) | — |

Unknown names in `enable_snippets` are ignored with a warning at
`jailbee base build` time.

**Dependency note:** `90-registry-mirror-ca.sh` (`registry-mirror-ca`) needs
`keytool` from a JDK — either `20-openjdk.sh` (`openjdk`) or
`20-corretto.sh` (`corretto`) — enable one of them too, or
`registry-mirror-ca` will fail at build time. `golden.stacks` auto-stages
`registry-mirror-ca` whenever both `java` and `docker` are on.

## Layered config — merge rules

| Source type | Merge rule | How to reset |
|---|---|---|
| Scalar (str/int/bool/path/enum) | Repo replaces global | `null` in repo clears |
| List | Repo appends to global | `[]` in repo replaces with empty |
| Map / dict | Recursive deep-merge per key | `{}` in repo replaces with empty |

Example: global `host_mounts` entry + repo `host_mounts` entry → two mounts after merge. To *exclude* a global mount, repo must `host_mounts: []` and re-list everything it wants.

### Host-level vs per-repo `docker_registry_mirror`

The key is ambiguous between the two layers:
- In global file: `GlobalConfig` shape (`{enabled, port, image, data_dir}`). Not merged into Config.
- In repo file: `Config.docker_registry_mirror` shape (`{extra_registries}`).

There is no global default for `extra_registries`; set it per-repo.

## Computed (non-YAML) Config attributes

Set at load time by `_build_config_from_dict`:

- `repo_root` — directory containing `.jailbee/`
- `upstream_remote` — the remote jailbee treats as the upstream. Resolved
  in order: the sole remote / `origin` / `remote.pushDefault` /
  `branch.<current>.remote` / the one remote with a `refs/remotes/<r>/HEAD`
  symref. Fallback `origin`. Not a YAML key — git already holds the answer,
  and each submodule resolves against its own directory
- `default_branch` — `git symbolic-ref refs/remotes/<upstream_remote>/HEAD`,
  fallback `main`
- `container_prefix` (when omitted) — `repo_root.name`
- `shared_dir` (when omitted) — `~/.local/share/jailbee/shared/<repo-name>`
- `golden.alias` (when omitted) — `<container_prefix>-base`

## Inspecting layers

- `jailbee config show --layer global` — raw user-level YAML
- `jailbee config show --layer repo` — raw repo YAML
- `jailbee config show` (or `--layer effective`) — merged result

## Initialising files

- `jailbee config init` — write `<cwd>/.jailbee/config.yaml`
- `jailbee config init --global` — write `~/.config/jailbee/global.yaml`
- `--force` overwrites an existing file
