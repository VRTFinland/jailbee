# Setting up JailBee in your own project

`jailbee` is project-agnostic — every repo provides its own `.jailbee/config.yaml`.
The built-in defaults are stack-neutral: the golden image installs only
JailBee's own plumbing, and language toolchains (JDK, Node, Python, Docker)
are bundled but opt-in per repo via
[`golden.stacks`](config.md#stacks-goldenstacks).

## 1. Drop a config into your repo

```bash
cd /path/to/your/repo
uv tool run jailbee config init   # writes .jailbee/config.yaml
```

## 2. Set `container_prefix` if needed

Defaults to `repo_root.name`. Required override if your directory has
uppercase, dots, or underscores (Incus accepts only `[a-z0-9-]`):

```yaml
container_prefix: sampleapp
```

## 3. Configure host mounts

Bind your dev credentials and tools into the container. User-declared
`host_mounts` are **read-write by default** — set `readonly: true`
explicitly for anything sensitive:

```yaml
host_mounts:
  - { host: ~/.gnupg,    container: /home/dev/.gnupg,    readonly: true }
  - { host: ~/.gitconfig, container: /home/dev/.gitconfig, readonly: true }
  - { host: ~/.ssh,      container: /home/dev/.ssh,      readonly: true }
```

### Sharing files with `.local`

If a `.local/` directory exists at the repo root, `jailbee new` automatically
bind-mounts it **read-write** into each new container at
`~/<container_prefix>/.local` (e.g. `~/SampleApp/.local`). It's a quick
host⇄container scratch channel — drop a script there from inside the
container and run it on the host, or vice-versa. The directory is added to
the container clone's `.git/info/exclude`, so it never shows up as untracked
in `jailbee ls` or `jailbee git diff`.

Presence-triggered: nothing happens unless `.local/` already exists (it is
never auto-created). Disable with `share_local: false` in `.jailbee/config.yaml`.
`--mount` containers already expose it via the full-repo bind, so the
auto-mount is skipped there. Existing containers pick it up on the next
`jailbee new`.

### Sharing host sockets

A container shares the host's kernel, so a **unix socket on the host can be
used from inside it** — the container connects to the host process on the
other end instead of running its own copy. JailBee already does this for you:
the Wayland, PulseAudio, D-Bus and GnuPG sockets under `/run/user/<uid>` are
attached to each container at the same path, which is what makes
`jailbee chrome` render on your desktop, and what makes `git commit -S` and
`ssh` work inside a container while the key stays on the host. With
`gpg.enabled`, `SSH_AUTH_SOCK` points at the host gpg-agent's SSH socket, so
a smartcard-backed key authenticates from inside the container without any
private key being copied into it — the agent signs, the container only asks.

The same trick is yours to use for anything else the host has a socket for.
An `adb` example follows; a host database socket, a media daemon or a
license server work the same way. Note that this is a container-only
capability: a VM-based sandbox has no host kernel to share, so it cannot
mount a host socket at all.

#### Talking to Android devices over `adb`

`adb` inside a container can drive a device or emulator attached to the
**host** — no USB passthrough, no second adb server. Forward the host's adb
server port into the container:

```yaml
host_ports:
  - { name: adb, port: 5037 }
```

That's the whole recipe. The host's adb server already listens on
`127.0.0.1:5037` by default, so once the forward is attached, plain
`adb devices` inside the container lists whatever the host has plugged
in — no `ADB_SERVER_SOCKET`, no mount. For existing containers, run
`jailbee apply` to attach the forward (proxy devices hotplug, so no
restart is needed). See
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
  - { host: ~/.android/adb.sock, container: /home/dev/.adb.sock, readonly: false }

container:
  env:
    ADB_SERVER_SOCKET: "localfilesystem:/home/dev/.adb.sock"
```

The mount is read-write on purpose: a socket the container can only read is
a socket it cannot talk on. On the host, the adb server has to be listening
on that same socket rather than on its default port — start it with
`adb -L localfilesystem:$HOME/.android/adb.sock start-server` (or export the
same `ADB_SERVER_SOCKET` on the host). After that, `adb devices` inside the
container lists what the host has plugged in, and every container sharing
the socket sees the same devices.

To run the emulator *inside* the container instead, pass the KVM node
through with `host_devices: [{ path: /dev/kvm }]` — see
[`host_devices`](config.md#host_devices). That gives the container its own
emulator and its own adb server, isolated from the host's.

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

If `extra_apt_packages` isn't enough, replace the whole `install.sh`:

```yaml
golden:
  provision_script: ./.jailbee/install.sh
  provision_env:
    REGION: eu-north-1   # whatever your script reads
```

## 4c. Optional — enable a coding agent

`agents:` is valid at both `~/.config/jailbee/global.yaml` (your personal
default across every repo) and this repo's `.jailbee/config.yaml` (a
team default for everyone working on it) — a repo entry merges over a
global one for the same agent, field by field, same as the rest of
JailBee's config layering. Enabling a shipped preset is usually two lines:

```yaml
agents:
  claude:
    enabled: true
    autostart: true
```

Six presets ship (`claude`, `codex`, `gemini`, `aider`, `opencode`, `grok`);
only `claude` is exercised in production, the rest are untested starting
points. See [Generic agent support](agents.md) for the full mechanism, the
preset table, and how to write your own agent entry.

## 5. Define autostart steps

Each step is a shell command run as the dev user inside the container.
`on_create` fires on `jailbee new`; `on_start` fires on `jailbee start`.

```yaml
autostart:
  step_timeout: 600
  env:
    NODE_ENV: development
  on_create:
    - name: setup
      run: "make setup"
    - name: server
      run: "make run"
      working_dir: backend
      background: true
      mounts: [aws]      # attach optional_mounts.aws for this step only
```

Auto-launching an IDE or Chrome is configured **outside** the `autostart`
block: set `jetbrains.ide` + `jetbrains.autostart` and `chrome.autostart`
in your config. See [`config.md`](config.md) for the full step-field
reference and those keys.

## 6. Build the image and create your first container

```bash
jailbee init                          # create profiles + ACL
jailbee base build                    # 10–15 min, one-time
jailbee new feat/x                    # new branch off default (e.g. dev)
jailbee new feat/x feat/wip-bar       # new branch off feat/wip-bar
jailbee new feat/jokufeat             # check out existing branch for review
jailbee new --current                 # use host repo's currently checked-out branch
```
