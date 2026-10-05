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
  Windows App (formerly Microsoft Remote Desktop), with a shared clipboard.

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
uv tool install 'jailbee[ssh]'
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

`files` is read at start-up, so run `jb remote ssh restart` after changing
it; `gui` and `default_entrypoint` take effect on the next connection. Then
start the display:

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
  LocalForward 3389 127.0.0.1:13389
```

- `ProxyJump` connects to the host's `sshd` first and from there to the
  JailBee service on the host's loopback.
- `HostKeyAlias` keeps the service's host key apart from anything else you
  reach as `127.0.0.1:8022`.
- `LocalForward` carries the shared display to the Mac's `localhost:3389` for
  as long as any `ssh jb` session is open. A second session opened alongside
  prints `bind ... Address already in use` for the forward; that is harmless,
  the first session's forward is still there. The destination must be written
  `127.0.0.1`, not `localhost` (see [Remote GUI](remote-gui.md#troubleshooting)).

`ssh jb help` now lists what the service offers.

### 3. On the Mac: Windows App

Install Windows App (formerly Microsoft Remote Desktop) from the App Store,
then:

1. **+** → **Add PC**. PC name: `localhost:3389`.
2. Leave the credentials at *Ask when required*. If asked, any user name and
   password will do — the display checks none (see
   [Security](security.md#remote-gui)).
3. Connect while an `ssh jb` session is open (or keep `ssh -N jb` running in a
   terminal tab). Accept the self-signed certificate warning.

You see an empty weston desktop until a container app is launched onto it.

## Setup B: a Linux VM on the Mac (Colima)

> **Experimental, not verified on real Apple hardware.** The caveats in
> [Known rough edges](#known-rough-edges-setup-b) are things to confirm, not
> solved problems. Corrections welcome.

Here JailBee, the Incus daemon and the containers all run in a Linux VM on the
Mac; the git repo stays on the macOS filesystem and is shared into the VM. You
type `jailbee` in the macOS terminal, and the builtin macOS bridge re-runs it
inside the VM.

[Colima](https://colima.run/) wraps Lima and ships an Ubuntu VM with Incus
preinstalled, using the Apple `Virtualization.framework` backend and virtiofs
for host-folder sharing.

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

This boots an Ubuntu guest (stock kernel — AppArmor, nftables, and btrfs all
present, so strict-egress ACLs, container confinement, and copy-on-write
`jailbee new` all work), starts the Incus daemon, and mounts your macOS `$HOME`
into the VM read-write via virtiofs.

### 3. Install JailBee inside the VM once

```sh
jailbee mac bootstrap
```

This installs JailBee in the VM and configures the bridge transport.

### 4. Use JailBee from any repo under your macOS $HOME

```sh
cd ~/code/your-repo
jailbee doctor      # delegated into the VM automatically
jailbee new feat/x
jailbee shell feat-x
```

Commands are delegated transparently via the bridge. Diagnose the bridge with
`jailbee mac doctor` (checks that the transport is configured, the VM is running,
JailBee is installed in the VM, and your working directory is under the shared
mount). For non-Colima transports or custom settings, edit
`~/.config/jailbee/macos.yaml` with keys: `transport`, `tty_flag`, `workdir_flag`,
`shared_root`.

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

There is no gpg-agent socket to bridge from macOS into the VM, so turn GPG
signing off in `.jailbee/config.yaml` (see [config reference](config.md)):

```yaml
gpg:
  enabled: false
```

If you do not use the shared display at all, you can likewise set
`jetbrains.enabled: false` and `browsers.chrome.enabled` /
`browsers.firefox.enabled: false` to drop those apps from the image.

### Known rough edges (setup B)

These are the parts specific to the macOS-shared-folder path that need
attention; the rest of JailBee behaves as on a native Linux host.

- **uid / gid mapping across virtiofs.** virtiofs collapses file ownership to
  a single guest user, while JailBee assumes the container user's uid equals the
  VM user's uid (it emits `raw.idmap: uid <uid> <uid>`). `jailbee doctor` reports
  a mismatch if this is off. You may need `shift=true` / a `raw.idmap` entry on
  the repo disk device so files written in a container show sane ownership back
  on macOS and vice-versa. Confirm this on your setup before relying on it.
- **`git clone --shared` over the share.** In the default (clone) mode a
  container clones the repo with `--shared`, so its
  `.git/objects/info/alternates` points at the host repo's object store and
  every git operation reads objects through the virtiofs mount for the life of
  the container. This works but a large `.git` over virtiofs can be slow.
  `jailbee new --mount` (bind the repo directly as the working tree) is an
  alternative to evaluate.
- **Performance.** Large trees (`node_modules`, build output) on virtiofs are
  slower than a native Linux disk. Prefer keeping heavy caches on the VM's own
  disk (JailBee's `<shared_dir>` lives in the VM, not on the share) rather than on
  the macOS-shared path.

### Alternative VM hosts

| Host | Notes |
|---|---|
| **Colima `--runtime=incus`** | Recommended above. Incus preinstalled, least setup. |
| **Lima (vz backend)** | Same engine and kernel as Colima; install Incus yourself (`apt`, or the [zabbly](https://github.com/zabbly/incus) repo for 7.x). More control over guest tuning. |
| **multipass** | Works (`apt install incus`), but host-folder sharing is sshfs/9p only — noticeably slower than virtiofs for I/O-heavy repos. |
| **UTM** | Fully manual OS + Incus install; arm64 virtiofs has been flaky. Not recommended for this workflow. |
| **Apple `container`** | Can host a persistent systemd VM (a "container machine") with an rw `--volume` mount, and its kernel has everything Incus *system* containers need — **except** AppArmor (no LSM at all), loadable modules, and btrfs/zfs (so `dir` storage only, no copy-on-write, and Incus runs unconfined). Viable if you specifically want Apple's own tool and accept the degradation, or build a custom kernel (`--kernel`) with `CONFIG_SECURITY_APPARMOR=y` / `CONFIG_BTRFS_FS=y` / `CONFIG_MODULES=y`. Requires macOS 26. No known precedent of Incus running inside it — expect to debug. |

## Daily use from the Mac

With the `jb` entry from setup A (or B), from a macOS terminal:

| To | Run |
|---|---|
| Open the dashboard | `ssh -t jb dashboard` |
| Open a JailBee console (pick a repo, run commands) | `ssh -t jb console`, or `ssh -t jb console --repo PREFIX` |
| Run one command | `ssh jb -- --repo PREFIX ls` |
| Get a shell inside a container | `ssh -t jb -- --repo PREFIX shell feat-x` |
| Open the IDE or a browser on the shared display | `ssh jb -- --repo PREFIX ide feat-x`, `... chrome feat-x [url]`, `... apps run APP --container feat-x` |
| Copy files in or out (`files: true`) | `sftp jb` (the top level lists the running containers), or `scp ./notes.md jb:/<container>/docs/` |
| Copy and paste | Works between the Mac and the shared display through Windows App |

`PREFIX` is the repo's `container_prefix`; the console and dashboard let you
pick it instead. The `--` keeps OpenSSH from reading `--repo` as its own
option. If no RDP client is connected when a GUI app is launched, the command
prints the connection recipe and waits up to two minutes for one, so you can
launch first and open Windows App second.

The remote surface is deliberately narrower than a local terminal: host
management commands (`apply`, `config edit`, `display up`, ...) are refused,
the git bridge moves refs but never the host's checked-out tree, and a
`--mount` container cannot be entered. Run those on the host itself (setup A)
or through the bridge (setup B). The full list is in
[Remote SSH](security.md#remote-ssh).

## Limits and workarounds

| Not available from the Mac | Instead |
|---|---|
| VS Code or JetBrains *Remote-SSH* into a container (the service offers no shell, exec channel or general port forwarding) | Run the IDE in the container: `jb ide` onto the shared display |
| Forwarding a container's dev server port (`:3000`) to the Mac's browser | Open it in the container's own browser on the shared display: `jb chrome feat-x http://localhost:3000` |
| Telling windows apart by container | Window titles do not name the container; keep one app per container on screen, or check the address bar / project name |
| Audio, GPU acceleration | None; the display renders in software |
| A separate screen per container | One shared screen and clipboard for every container (see [Security](security.md#remote-gui)) |
| GPG signing with a key held on the Mac (e.g. a YubiKey) | Not bridged. In setup A, containers sign with the Linux host's gpg-agent as usual |
| The Qt dashboard (`jb gui`) | Host only; use `ssh -t jb dashboard` |

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

The bridge is unit-tested with a mocked transport, and the shared display has
been used from Windows App. The end-to-end checks for both setups on real Apple
hardware are in [Manual testing](https://github.com/VRTFinland/jailbee/blob/main/docs/manual-testing.md#macos-client); they are the
acceptance gate before setup B is treated as supported.
