# Setting up JailBee in your own project

`jailbee` is project-agnostic. A project's shared settings live in
`.jailbee/config.yaml`; this guide sets up that file. For directories without
one, see [scratch environments](config.md#scratch).
The built-in stack defaults are disabled: bundled toolchain snippets are
opt-in via [`golden.stacks`](config.md#stacks-goldenstacks), in either global
or repo config. The base OS and JailBee's plumbing still include system
packages; "stack-neutral" does not mean an empty image.

## 1. Drop a config into your repo

Run these commands on the **host**, from the repository root, after
[installing JailBee and setting up Incus](installation.md):

```bash
cd /path/to/your/repo
jailbee config init   # writes .jailbee/config.yaml in the current directory
```

The command refuses to overwrite an existing config unless you pass
`--force`. Commit the repo config for the team; keep personal settings in
`~/.config/jailbee/global.yaml` or the
[host-local repo override](config.md#host-local-overrides). If the global
file does not exist yet, `jailbee config init --global` creates it. The
repo template is a starting point, not a dump of the effective config:
global settings can still enable integrations.

## 2. Set `container_prefix` if needed

For a repo with a config file, the prefix defaults to the repo directory's
name. JailBee requires `[a-z0-9][a-z0-9-]*`; set an explicit override if the
name contains uppercase letters, dots, underscores, spaces, or a leading
hyphen. Choose a distinct prefix for each project on the host:

```yaml
container_prefix: sampleapp
```

## 3. Configure host mounts

Mount only the files and tools the project needs. User-declared
`host_mounts` are **read-write by default**; set `readonly: true` when the
container must not modify the host files. For example, in your personal
global config (the generated global template already includes this mount):

```yaml
host_mounts:
  - { host: ~/.gitconfig, container: /home/dev/.gitconfig, readonly: true }
```

Read-only prevents modification, **not disclosure**: credentials embedded in
a file remain readable and copyable. Avoid mounting whole `~/.ssh` or
`~/.gnupg` directories casually. JailBee's SSH integration normally seeds a
separate shared SSH directory instead of mounting all of `~/.ssh`; see
[`ssh`](config.md#ssh) and the GPG caveat below. Keep personal paths in global
or host-local config; reserve the committed repo file for team-wide mounts.

### Sharing files with `.local`

If a `.local/` directory exists at the repo root, `jailbee new` automatically
bind-mounts it **read-write** into each new container at
`~/<container_prefix>/.local` (e.g. `~/sampleapp/.local`). It is a shared
host/container scratch channel, not a private directory for each branch.
Container writes change host files too; review scripts produced there
before running them on the host. For clones, JailBee adds `/.local/` to
`.git/info/exclude` to hide untracked files there, but already tracked files
remain tracked.

Presence-triggered: nothing happens unless `.local/` already exists (it is
never auto-created). Disable with `share_local: false` in `.jailbee/config.yaml`.
`--mount` containers already expose it via the full-repo bind, so the
auto-mount and clone-specific exclude are skipped; the host repo's ignore
rules apply. The automatic mount is attached during creation, not
retroactively when `.local/` appears. `jailbee new` refuses an existing
container name rather than updating that container.

### Sharing host sockets

A container shares its Incus host's kernel, so a **filesystem unix socket
on that host can be used from inside it** when mounted and permitted by
socket permissions. The client talks to the host process, not a separate
copy inside the container. This exposes the service's operations even
through a read-only mount; it is not restricted by the TCP/IP egress ACL.

With `gpg.enabled`, JailBee mounts the host GnuPG socket directory under
`/run/user/<uid>` and points `SSH_AUTH_SOCK` at the host gpg-agent's SSH
socket. A smartcard private key stays on the card while the agent signs on
request. However, GPG integration also mounts the host's **entire
`~/.gnupg` directory read-only**: readable disk-stored secret keys are exposed,
not just sockets or public keys.

For a local desktop, the Wayland socket is attached on demand by default
(or at boot with `gui.wayland: always`); opt-in PulseAudio and D-Bus mounts
are handled at boot. See [`gui`](config.md#gui). A remote desktop session
uses a different display path; see [Remote GUI](remote-gui.md).

The same approach can expose a host database socket or another local
service. The shared kernel here is the **Incus host's**: when Incus runs in
a VM, this does not directly expose sockets from the physical host outside
that VM; use an appropriate forwarding mechanism instead.

#### Talking to Android devices over `adb`

`adb` inside a container can drive a device or emulator attached to the
**host** — no USB passthrough, no second adb server. Forward the host's adb
server port into the container:

```yaml
host_ports:
  - { name: adb, port: 5037 }
```

Install compatible adb versions on the host and in the container, and
start the host server with `adb start-server`. Its default endpoint is
localhost port 5037. With the forward attached, plain `adb devices` in the
container uses that server without a socket mount or an
`ADB_SERVER_SOCKET` override. This grants access to the host server's
devices; it does not create a separate device sandbox.

For existing containers, run `jailbee apply` to reconcile the forward.
The port change itself can hotplug into a running container without a
restart; stopped containers use it on their next start. `apply` may still
offer restarts for other changes. See
[`host_ports`](config.md#host_ports) for the full field grammar.

If something inside the container is already listening on 5037 — most
likely its own adb server, started by an `adb` invocation before the
forward was attached — the forward cannot be attached, because instance-side
proxy listeners and pre-existing listeners on the same port can't coexist.
Incus's own message for that case names neither the port nor the cause
(`Failed to receive fd from listener process: Failed to receive file
descriptor via abstract unix socket`), which is why `jailbee port
to-container` translates it into something actionable: stop the container's
own adb server (or forward to a different container port) and retry.

**Fallback: bind-mount the socket.** `host_ports` only forwards TCP/UDP
ports; the schema deliberately does not expose unix-socket endpoints. For a
service that is only ever reachable over a unix socket, bind-mount it
instead and point the client at it explicitly:

```yaml
host_mounts:
  - { host: ~/.android/adb.sock, container: /home/dev/.adb.sock, readonly: true }

container:
  env:
    ADB_SERVER_SOCKET: "localfilesystem:/home/dev/.adb.sock"
```

Read-only prevents filesystem changes through the mount, not socket
communication; socket permissions must still allow the container user to
connect. Create the parent directory on the host and start adb on that
socket with `adb -L "localfilesystem:$HOME/.android/adb.sock" start-server`
before attaching the mount. Point host clients at the same endpoint with
`ADB_SERVER_SOCKET` too. Every client using this server shares its device
access. A direct socket-file mount can become stale if the server recreates
the socket; reattach it after such a restart.

To run an emulator *inside* the container instead, pass the KVM node
through with `host_devices: [{ path: /dev/kvm }]` — see
[`host_devices`](config.md#host_devices). This exposes host KVM for hardware
acceleration; it does not install or start an emulator. Install the Android
tools yourself and use a container-local adb server without the host
forward or `ADB_SERVER_SOCKET` override. Device passthrough increases the
host-kernel interfaces accessible to container processes.

## 4. Optional — stack runtimes and extra apt packages

The golden image is **stack-neutral by default** (locale, prompt, GUI
libraries, `gh`, tmux, build-essential). Language runtimes and cloud
helpers — Node, Java, Docker, Python, the ECR helper — are bundled but
**opt-in**. The recommended way to turn them on is `golden.stacks`, one
key per runtime — it also wires up the matching shared caches and
build-env values (`JAVA_PACKAGE`, `NODE_MAJOR`):

```yaml
golden:
  stacks:
    node: 22          # major version, or `true` for the default
    docker: true
  extra_apt_packages:
    - mariadb-client
    - postgresql-client
```

See [Stacks](config.md#stacks-goldenstacks) in `config.md` for the full
grammar, including the Java `openjdk-N` / `corretto-N` forms and the
`java` + `docker` → `registry-mirror-ca` auto-add. `golden.enable_snippets`
(stage a snippet by name) plus manual `shared_caches` remain available
directly as the low-level escape hatch for anything `stacks` doesn't
cover.

Package names for `extra_apt_packages` are validated against the Debian
grammar (`[a-z0-9][a-z0-9+\-.]*`) — anything else is rejected at
config-load time. Run `jailbee base build` after editing to rebuild the image.

## 4b. Optional — override the provisioning script entirely

Prefer `golden.stacks`, `extra_apt_packages` or
[provisioning snippets](config.md#golden) for additions. For a full
replacement, set `provision_script`:

```yaml
golden:
  provision_script: ./.jailbee/install.sh
  provision_env:
    REGION: eu-north-1   # whatever your script reads
```

The path is relative to the repo root. Only this script is copied, not
adjacent files, and it runs as root via `bash /provision/install.sh`.
This bypasses the bundled script and snippet staging, including stack
installation and the normal `extra_apt_packages` installation. Your script
must create the `dev` user using `CONTAINER_UID` / `CONTAINER_GID` and supply
the required sudo, Git, tmux and other plumbing itself. The build still
passes environment variables such as `EXTRA_APT_PACKAGES`, but the script
must consume them explicitly.

## 4c. Optional — enable a coding agent

`agents:` is valid at both `~/.config/jailbee/global.yaml` (your personal
default across every repo) and this repo's `.jailbee/config.yaml` (a
team default for everyone working on it). Agent entries merge field by
field over shipped presets and global settings; the host-local repo layer
can override them again. Other config types have their own merge rules,
so see [Configuration layers](config.md#configuration-layers) rather than
assuming every list is merged. To enable and auto-launch a preset:

```yaml
agents:
  claude:
    enabled: true
    autostart: true
```

Six presets ship (`claude`, `codex`, `gemini`, `aider`, `opencode`, `grok`),
with different verification coverage and prerequisites. The generated
global config already enables Claude; this example additionally requests
autostart. A preset does not necessarily install its package manager:
Gemini needs Node/npm, for example, while Aider needs `uv`. See
[Generic agent support](agents.md) for the preset table and custom entries,
and [LiteLLM](litellm.md) for routing supported agents through the proxy.

If your project has PR-description rules that no template or
`CONTRIBUTING.md` states, commit them as `pr.prompt` to guide AI-generated
PR text. `jailbee pr` runs on the host; these instructions have no effect
when AI generation is disabled or unavailable. See
[Encoding a project's PR standard](config.md#encoding-a-projects-pr-standard).

What is personal *and* specific to this repo — `github.token`, a credential
group, an extra egress host only you need — belongs in the host-local
`~/.config/jailbee/repos/<container_prefix>.yaml`
(`jailbee config edit --local`). The `github`, `credentials` and `litellm`
blocks are rejected in committed repo config. LiteLLM's proxy and account
settings belong in `global.yaml`; the local file permits only its
`routes`, `profiles`, `default_profile` and `autostart` overrides. See
[Host-local overrides](config.md#host-local-overrides).

## 5. Define autostart steps

Each step is a shell command run as the dev user in a tmux window inside
the container. With autostart enabled, `on_create` runs during creation;
`on_start` follows it and runs again through `jailbee start` and
`jailbee restart`. `--no-autostart` skips these steps. Starting directly
through Incus does not run JailBee's autostart hooks.

Steps are grouped into **stages**; a stage owns the network mode and the
`optional_mounts` for all of its steps. A step's `working_dir` is relative
to the container's repo directory.

```yaml
autostart:
  step_timeout: 600
  env:
    NODE_ENV: development
  on_create:
    - stage: setup
      steps:
        - { name: setup, run: "make setup" }
  on_start:
    - stage: run
      steps:
        - name: server
          run: "make run"
          working_dir: backend
          background: true
```

Replace `make setup`, `make run` and `backend` with your project's commands
and directory. `background: true` starts a long-running step without
waiting for it to finish; it does not keep the stage open. Stage mounts
are detached and its temporary network mode is restored when the stage
ends, so do not rely on them for a background server's ongoing access.
Stage `detach: true` is different: it hands that stage and all later stages
to a background worker; `jailbee autostart status <container>` reports its
progress.

For a cloned branch, `new` can take the autostart block from the source
commit rather than the host checkout; other settings still come from the
host's config layers. Commit the setup you want a new clone to use, review
untrusted branch commands, and see
[Where does the autostart config come from?](config.md#where-does-the-autostart-config-come-from)
for fallback and approval rules.

Auto-launching an IDE or a browser is configured **outside** the `autostart`
block: enable the integration, then set `jetbrains.ide` +
`jetbrains.autostart`, or `browsers.chrome.autostart` /
`browsers.firefox.autostart` (a plain `apps:` entry has its own
`apps.<name>.autostart`). See
[`config.md`](config.md#stages-and-chains) for stages, parallel chains and the
full step-field reference, and [`config.md`](config.md) for those keys.

## 6. Build the image and create your first container

Validate the effective configuration, initialize this repo's resources, then
build its golden image. `init` is for first use and refuses existing
profiles; use `jailbee apply` to update them later.

```bash
jailbee config validate
jailbee init                          # profiles, ACL and shared directories
jailbee base build                    # duration depends on packages and host
jailbee new feat/x                    # create a new branch from the default
```

The last command assumes `feat/x` does not already exist. These are
alternative creation examples, not commands to run on the same container:

```bash
jailbee new feat/child feat/wip-bar    # new branch from an existing base
jailbee new feat/existing             # clone an existing branch for review
jailbee new --current                 # clone the host's checked-out branch
```

An existing branch is cloned as-is (after confirmation); an explicit base
then sets the comparison baseline rather than changing the branch's starting
commit. Clone mode does not copy the host's uncommitted work. By default,
new branches from the default branch use the upstream tip fetched on the
host; an explicit local base or an existing local feature branch need not
be refreshed. See [Git bridge](git-bridge.md) for source selection and
synchronization.

`feat/x` is the Git branch; its default container name is `feat-x`, or
`sampleapp-feat-x` in Incus for the prefix above. Use `jailbee ls` to see
creation/autostart progress, then `jailbee shell feat-x` or
`jailbee tmux feat-x` to enter it. Rebuild the image after changing golden
provisioning; that affects future containers, not those already created.
