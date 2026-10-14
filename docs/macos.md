# Using JailBee from a Mac

JailBee only runs on Linux, because the Incus **daemon** is Linux-only (see
[Why not native macOS](#why-not-native-macos)). A Mac reaches it in one of two
setups:

| Setup | Where JailBee runs | Status |
|---|---|---|
| [A. A Linux host, the Mac as client](#setup-a-a-linux-host-the-mac-as-client) | A Linux workstation, server or cloud VM you can SSH into | Recommended. Windows App against the shared display, clipboard included, is confirmed; the Mac-side recipe below is still being run end to end |
| [B. A Linux VM on the Mac](#setup-b-a-linux-vm-on-the-mac-colima) | A Colima VM on the same Mac, with the repo shared from macOS | **Experimental, not verified on real Apple hardware** |

Either way, day-to-day use from the Mac goes through the same two things:

- the [SSH service](installation.md#optional-ssh-service) — the dashboard, a
  JailBee console, one-shot commands and, if enabled, file transfer;
- the [shared RDP display](remote-gui.md) — GUI apps from the containers (an
  IDE, Chrome, Firefox, your `apps:` entries), viewed in
  Windows App (formerly Microsoft Remote Desktop), with a shared clipboard —
  or, optionally, as [native macOS windows](#4-on-the-mac-native-windows-optional)
  through waypipe and the Cocoa-Way compositor.

See [Daily use from the Mac](#daily-use-from-the-mac) for what that looks like,
and [Limits and workarounds](#limits-and-workarounds) for what it does not do.

## Setup A: a Linux host, the Mac as client

The Linux host is set up exactly as in [Installation](installation.md); the
steps below add remote access to it. You need an ordinary SSH login to the
host (its own `sshd`) from the Mac.

### 1. On the Linux host

Install the SSH extra, authorize the Mac's public key (paste the contents of
the Mac's `~/.ssh/id_ed25519.pub` when asked) and start the service:

```bash
uv tool install --force 'jailbee[ssh]'
jb remote ssh key add
jb remote ssh enable
```

Turn on the shared display, and optionally file transfer, in
`~/.config/jailbee/global.yaml` (see [`remote.ssh`](config.md#remotessh)):

```yaml
remote:
  ssh:
    gui: true
    files: true                  # optional: sftp/scp into containers
    default_entrypoint: dashboard  # optional: a bare `ssh -t jb` opens the dashboard
```

Enabling `files` requires `jb remote ssh restart` so the service registers
file-transfer support. Disabling it blocks new file-transfer channels without
a restart, but does not close existing ones. Restart after changing it to
apply the setting consistently. `gui` and `default_entrypoint` are reloaded for
new requests. Then start the display:

```bash
jb display up
```

The service keeps its default loopback listener (`127.0.0.1:8022`). The Mac
reaches it through the host's own `sshd`, so nothing new is exposed on the
network — see [Remote SSH](security.md#remote-ssh) before changing `listen`.

### 2. On the Mac: one SSH config entry

Add to `~/.ssh/config`, with `you@devbox` replaced by your normal SSH login
to the Linux host:

```text
Host jb
  HostName 127.0.0.1
  Port 8022
  User jailbee
  ProxyJump you@devbox
  HostKeyAlias jailbee-devbox
  LocalForward 127.0.0.1:3389 127.0.0.1:13389
```

- `ProxyJump` connects to the host's `sshd` first and from there to the
  JailBee service on the host's loopback.
- `HostKeyAlias` keeps the service's host key apart from anything else you
  reach as `127.0.0.1:8022`.
- `LocalForward` binds the display tunnel to the Mac's `127.0.0.1:3389` and
  keeps it open for the lifetime of the SSH connection that created it.
  Another session may warn `bind ... Address already in use`; if your first
  session owns that port, its tunnel still works. Otherwise choose a free
  local port and use that port in Windows App too. With
  `ExitOnForwardFailure yes`, a duplicate forward can prevent the second SSH
  connection. The destination must be written `127.0.0.1`, not `localhost`
  (see [Remote GUI](remote-gui.md#troubleshooting)).

`ssh jb help` now lists what the service offers.

### 3. On the Mac: Windows App

Install Windows App (formerly Microsoft Remote Desktop) from the App Store,
then:

1. **+** → **Add PC**. PC name: `127.0.0.1:3389`.
2. Leave the credentials at *Ask when required*. If asked, any user name and
   password will do — the display checks none (see
   [Security](security.md#remote-gui)).
3. Connect while an `ssh jb` session is open (or keep `ssh -N jb` running in a
   terminal tab). Accept the self-signed certificate warning.

You see an empty weston desktop until a container app is launched onto it.

### 4. On the Mac: native windows (optional)

Instead of the shared display, container apps can open as ordinary macOS
windows, one per app window, through a [waypipe](remote-gui.md#native-windows-with-waypipe)
session and [Cocoa-Way](https://github.com/J-x-Z/cocoa-way), a third-party
Wayland compositor for macOS. The windows close when the SSH session ends;
keep Windows App for windows that must survive a disconnect.

Tested with Cocoa-Way 2.0.3 and waypipe-darwin 0.11.2 on macOS 26, Apple
silicon.

Install both from the Cocoa-Way author's Homebrew tap:

```bash
brew tap J-x-Z/tap
brew trust --tap J-x-Z/tap
brew install cocoa-way waypipe-darwin
```

The tap is a third-party repository, not part of Homebrew's official formulae.
`brew trust` permits Homebrew to load its Ruby code as your user, including
updated formulae in later upgrades; it is not a security review or a version
pin. Homebrew can also trust individual formulae (`brew trust --formula
J-x-Z/tap/cocoa-way`, and likewise for `waypipe-darwin`). Building both from
reviewed, pinned source avoids the tap but still runs third-party build code.

Start the compositor in rootless mode, so every app window becomes a macOS
window of its own:

```bash
COCOA_WAY_PRESENTATION=rootless cocoa-way
```

Without the variable, every app opens inside a single Cocoa-Way window. The
variable is read by Cocoa-Way 2.0.3 but not documented upstream, so check it
after an upgrade.

Cocoa-Way does not export its socket to your terminal, so a plain
`waypipe ssh` can fail with `WAYLAND_DISPLAY is not set`. For the default
socket location, add this to `~/.zshrc`, then open a new terminal or run
`source ~/.zshrc`. It selects the first socket under `$TMPDIR/cocoa-way/`;
use an explicit socket instead if you run several compositor instances or
set a custom runtime directory:

```bash
cwaypipe() {
  local dir="${TMPDIR:-/tmp}"
  dir="${dir%/}/cocoa-way"
  local sock
  sock=$(find "$dir" -maxdepth 1 -type s -name 'wayland-*' 2>/dev/null | sort | head -n1)
  [ -n "$sock" ] || { echo "Cocoa-Way is not running (no socket in $dir)" >&2; return 1; }
  XDG_RUNTIME_DIR="$dir" WAYLAND_DISPLAY="$(basename "$sock")" \
    waypipe --compress=zstd ssh -o StreamLocalBindUnlink=yes "$@"
}
```

Then, with the `jb` entry from step 2:

```bash
cwaypipe -t jb dashboard                         # launch apps from the menu
cwaypipe jb chrome <container> --repo PREFIX     # or one app directly
```

With `--repo` after the command, no client `--` separator is needed. It stays
open until the app exits. Window titles carry the full Incus name, for example
`[PREFIX-feat-x] `. The rest of
[Native windows with waypipe](remote-gui.md#native-windows-with-waypipe)
applies unchanged, including `-o ControlMaster=no` if your ssh config shares
connections. Cocoa-Way's repository also ships `run_waypipe.sh`, which handles
additional runtime locations and display selection; the function above is
only a minimal default-location helper.

## Setup B: a Linux VM on the Mac (Colima)

> **Experimental, not verified on real Apple hardware.** The caveats in
> [Known rough edges](#known-rough-edges-setup-b) are things to confirm, not
> solved problems. Corrections welcome.

Here JailBee, the Incus daemon and the containers all run in a Linux VM on the
Mac; the git repo stays on the macOS filesystem and is shared into the VM. You
type `jailbee` in the macOS terminal, and the builtin macOS bridge re-runs it
inside the VM.

[Colima](https://colima.run/) wraps Lima. Its Incus runtime uses an Ubuntu
guest image with Incus preinstalled. The recipe below selects Apple's
`Virtualization.framework` backend (`vz`, requiring macOS 13 or later) and
virtiofs for host-folder sharing.

### 1. Install the tools on macOS

```sh
brew install colima incus
uv tool install jailbee   # the macOS bridge
```

### 2. Start the VM once

```sh
colima start --runtime=incus --vm-type=vz --mount-type=virtiofs \
  --cpu 4 --memory 8 --disk 60
```

For a new default Colima profile, this starts the Incus VM and shares your
macOS home directory read-write through virtiofs. Existing profiles may have
custom mounts or storage settings. Confirm that the repo is accessible inside
the VM at the **same absolute path** as on the Mac.

Do not infer working confinement, strict-egress filtering or copy-on-write
from the guest OS alone. Check the guest's AppArmor and networking support,
and inspect the Incus storage pool before relying on them. Colima's current
Incus initialization uses ZFS for a new pool, not btrfs; existing pools may
differ. See [Storage](storage.md) and the setup B verification checklist.

### 3. Install JailBee inside the VM once

```sh
jailbee mac bootstrap
```

This installs or upgrades JailBee inside the already-running VM, installing
`uv` there if needed. It installs from JailBee's Git repository, not a pinned
copy of the Mac's PyPI release; a version mismatch produces a warning, not an
automatic version sync. The bridge uses `colima ssh` by default; bootstrap does
not write or change its transport configuration.

Bootstrap only installs the package. Complete the Linux-side
[installation](installation.md) and [first-time setup](getting-started.md)
inside the VM, including the base image and repository profiles, before
creating a configured-repo container. Set the GPG option described below
before setup if you do not have a signing agent inside the VM.

### 4. Use JailBee from any repo under your macOS $HOME

```sh
cd ~/code/your-repo
jailbee doctor      # delegated into the VM automatically
jailbee new feat/x
jailbee shell feat-x
```

Commands are delegated via the bridge, with the Mac's working-directory path
passed unchanged to the guest. `jailbee mac doctor` checks the transport
executable, VM reachability, guest JailBee availability, and whether the local
working directory is under the configured `shared_root`. It does **not** check
that the directory is actually mounted in the VM.

For non-Colima transports or custom settings, edit
`~/.config/jailbee/macos.yaml` with keys: `transport`, `tty_flag`, `workdir_flag`,
`shared_root`. `shared_root` defaults to the Mac's home directory; changing it
neither creates a VM mount nor translates paths. The default transport requests
a PTY with `tty_flag: ['-t']`; if your transport version rejects that flag,
adjust it (or set `tty_flag: []`) and verify interactive commands separately.

### 5. GUI apps: go through the SSH service

A command delegated by the bridge is **not** an SSH-service session, so
`jailbee ide`, `jailbee chrome` and `jailbee apps run` typed in the macOS
terminal try to draw on the VM's own display, which does not exist, and no
window appears. GUI apps reach the Mac only when launched from a session of
the [SSH service](installation.md#optional-ssh-service) running inside the VM:

1. Do [step 1 of setup A](#1-on-the-linux-host) inside the VM, for example
   with `colima ssh`. JailBee was installed there without the `ssh` extra, so
   reinstall it with `uv tool install --force 'jailbee[ssh]'`.
2. Add an SSH config entry on the Mac like the one in
   [setup A](#2-on-the-mac-one-ssh-config-entry), with `HostKeyAlias
   jailbee-colima` and either:
   - no `ProxyJump` at all, relying on Lima forwarding the VM's loopback port
     8022 to the Mac's (Lima forwards guest ports by default), or
   - `ProxyJump colima`, after adding Colima's own entry with `colima
     ssh-config >> ~/.ssh/config`.
3. Use Windows App as in [setup A](#3-on-the-mac-windows-app), and launch GUI
   apps through `ssh jb ...` (see [Daily use](#daily-use-from-the-mac)), not
   through the bridge.

None of these three steps has been verified on a Mac yet: whether the service's
user unit runs in the Colima VM (it may need `sudo loginctl enable-linger
$USER` there), and which of the two routes in step 2 works, are open.

### GPG signing

The macOS bridge does not forward the Mac's gpg-agent socket into the VM.
Unless you arrange a signing agent inside the VM, disable JailBee's automatic
GPG integration in `.jailbee/config.yaml` (see [config reference](config.md)):

```yaml
gpg:
  enabled: false
```

This disables the automatic GPG directory/socket mounts, not Git's signing
settings or manually configured mounts. If the Git configuration visible in
the container enables `commit.gpgsign`, disable signing separately or provide
a working signing setup.

If you do not use GUI apps through either RDP or waypipe, you can also set
`jetbrains.enabled: false` and `browsers.chrome.enabled` /
`browsers.firefox.enabled: false` to drop those apps from the image.

### Known rough edges (setup B)

These are additional checks for the macOS-shared-folder path, not guarantees
that the rest of the VM setup is equivalent to a tested Linux host.

- **UID/GID mapping across virtiofs.** JailBee maps the configured container
  UID and GID to the same IDs on the VM host. `jailbee doctor` compares them
  with the VM user's IDs and checks subordinate-ID delegation; it does not
  validate ownership across virtiofs. Test file creation and permissions from
  both macOS and the container. Do not assume a `shift=true` device option or
  a different `raw.idmap` is a working remedy on your backend.
- **`git clone --shared` over the share.** In the default clone mode, Git
  borrows objects from the source repository through
  `.git/objects/info/alternates`. Reads of borrowed objects cross virtiofs, and
  the clone depends on that source object store unless dissociated. Check
  performance and the [shared-clone cautions](git-bridge.md) on your setup.
  `jailbee new --mount` uses the host repo directly as the working tree; it is
  not an equivalent isolated clone and is unavailable through the restricted
  SSH service.
- **Performance and cache placement.** Large trees (`node_modules`, build
  output) on virtiofs may be slower than a native Linux disk. Keep heavy caches
  on VM-local storage. JailBee's default `shared_dir` is the guest's
  `$XDG_DATA_HOME/jailbee/shared/<prefix>` (normally under `~/.local/share`),
  but an explicit setting can point elsewhere. Verify the selected path is
  not on the macOS share; configure a VM-local path if needed.

### Alternative VM hosts

| Host | Notes |
|---|---|
| **Colima `--runtime=incus`** | The experimental recipe above; supplies an Incus guest image. |
| **Lima (vz backend)** | Configure a suitable Linux guest, install Incus, and provide same-path repo mounts. The selected image and kernel need not match Colima's. |
| **Multipass or UTM** | Candidate Linux VM hosts, not verified JailBee recipes. Check guest kernel facilities, Incus setup, same-path folder sharing, ownership and I/O performance. |
| **Apple `container`** | Not a verified Incus host. Do not assume its Linux container VMs provide the systemd, kernel security, networking and storage facilities needed for this setup; a custom kernel alone is not proof of compatibility. |

These alternatives require their own transport configuration and end-to-end
validation; they are not supported substitutes merely because they run Linux.

## Daily use from the Mac

With the `jb` entry from setup A (or B), from a macOS terminal:

| To | Run |
|---|---|
| Open the dashboard | `ssh -t jb dashboard` |
| Open a JailBee console (pick a repo, run commands) | `ssh -t jb console`, or `ssh -t jb console --repo PREFIX` |
| Run one command | `ssh jb ls --repo PREFIX` |
| Get a shell inside a container | `ssh -t jb shell feat-x --repo PREFIX` |
| Open the IDE or a browser on the shared display | `ssh jb ide feat-x --repo PREFIX`, `ssh jb chrome feat-x [url] --repo PREFIX`, `ssh jb apps run APP --container feat-x --repo PREFIX` |
| Open them as native macOS windows instead ([step 4](#4-on-the-mac-native-windows-optional)) | `cwaypipe -t jb dashboard`, or `cwaypipe jb chrome feat-x --repo PREFIX` |
| Copy files in or out (`files: true`) | `sftp jb`, or `scp ./notes.md jb:/PREFIX-feat-x/docs/` (the destination directory must exist) |
| Copy and paste | Works between the Mac and the shared display through Windows App |

`PREFIX` is the repo's `container_prefix`; the console and dashboard let you
pick it instead. `ssh jb repos` lists visible repos and `ssh jb help` lists
allowed commands. One-shot commands without `--repo` use the only visible repo,
or ask with `ssh -t` when there are several (without a PTY: exit 2 lists them).
The old `ssh jb -- --repo PREFIX COMMAND` form still works; its `--` keeps
OpenSSH from reading `--repo` as its own option. When launching onto the shared
RDP display with no RDP client connected, the command prints the connection
recipe and waits up to two minutes for one. Native waypipe launches do not
need an RDP connection.

File transfer lists eligible running clone-mode containers from visible repos
by their **full Incus names**. `/PREFIX-feat-x/` maps to that container's repo
directory, not its root filesystem. Mount-mode containers are excluded.

With the default `restrict_host: true`, the remote surface is deliberately
narrower than a local terminal: host management commands (`apply`,
`config edit`, `display up`, ...) are refused,
the git bridge moves refs but never the host's checked-out tree, and a
`--mount` container cannot be entered. Run those on the host itself (setup A)
or through the bridge (setup B). The full list is in
[Remote SSH](security.md#remote-ssh).

## Limits and workarounds

| Not available from the Mac | Instead |
|---|---|
| VS Code or JetBrains *Remote-SSH* through the JailBee service (it routes JailBee commands, not a general-purpose shell/exec environment or arbitrary port forwards) | Run the IDE in the container through `ssh jb ide feat-x --repo PREFIX`, on RDP or via waypipe |
| Arbitrary TCP forwarding through the JailBee SSH service | Use the container's browser, or publish the port to the Linux host and tunnel it through the host's ordinary SSH service (example below) |
| Telling windows apart by container on the shared display | Window titles do not name the container; keep one app per container on screen, or check the address bar / project name. [Native windows](#4-on-the-mac-native-windows-optional) carry a `[<container>] ` prefix |
| Audio, GPU acceleration | None; the display renders in software |
| A separate screen per container | One shared screen and clipboard for every container (see [Security](security.md#remote-gui)) |
| GPG signing with a key held on the Mac (e.g. a YubiKey) | Not bridged. In setup A, containers sign with the Linux host's gpg-agent as usual |
| The Qt dashboard (`jb gui`) | Host only; use `ssh -t jb dashboard` |

### A dev server in the Mac's browser (setup A)

With the dev server listening on port 3000 inside `feat-x`, publish it to the
Linux host's loopback, then open a tunnel through the host's ordinary `sshd`:

```bash
ssh jb port to-host 3000 feat-x --repo PREFIX
ssh -o ExitOnForwardFailure=yes -N -L 127.0.0.1:3000:127.0.0.1:3000 you@devbox
```

Keep the second command running and open `http://127.0.0.1:3000` on the Mac.
This is **not** a tunnel through `ssh jb`: that service's TCP forwarding is
restricted to the shared RDP display (waypipe uses a separate Unix-socket
forward). Both examples bind to loopback; do not switch to `0.0.0.0` unless
you intend to expose the service. If port 3000 is occupied on the Linux host,
choose another `--host-port` and adjust the tunnel's destination. If it is
occupied on the Mac, change the first `3000` in `-L` and the browser URL.
The Incus forward persists after the SSH tunnel closes; remove the TCP
to-host device for container port 3000 when finished:

```bash
ssh jb port rm port-th-tcp-3000 feat-x --repo PREFIX
```

See the [command reference](commands.md) for the full `port` command set.

## Why not native macOS

It is tempting to install the native macOS `incus` client (it exists —
`brew install incus` ships a client-only build) and point it at a daemon
running in a VM. **This does not work for JailBee**, for one decisive reason:

> Incus resolves `disk` device `source:` paths on the **daemon** host, not on
> the client. JailBee mounts many host paths into each container (the repo, shared
> caches, `/etc/localtime`, GnuPG, runtime sockets, …). A native macOS client
> cannot make the Linux daemon bind-mount a macOS path — the path has to exist
> inside the VM the daemon runs in.

On top of that, JailBee assumes the machine it runs on shares one uid namespace
with the containers (`raw.idmap`, `/etc/subuid`), reads Linux-only host state
(`/var/lib/incus`, `/proc` keyring, `systemctl --user`), and its GUI features
assume a Linux display server. All of these hold when JailBee runs on a Linux
machine — a separate host or a VM on the Mac — and break when it runs on macOS
against a remote daemon. See [Architecture](architecture.md) for why
co-location matters.

## Verification

The bridge is unit-tested with a mocked transport, the shared display has
been used from Windows App, and native windows through Cocoa-Way have been
opened from the dashboard. The end-to-end checks for both setups on real Apple
hardware are in [Manual testing](https://github.com/VRTFinland/jailbee/blob/main/docs/manual-testing.md#macos-client); they are the
acceptance gate before setup B is treated as supported.
