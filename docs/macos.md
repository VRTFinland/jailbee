# Running JailBee on macOS (Apple Silicon)

> **Status: experimental / unverified.** This is the recommended setup based
> on how JailBee is architected; it has not yet been validated end-to-end on real
> Apple hardware. Treat the caveats in [Known rough edges](#known-rough-edges)
> as things to confirm, not as solved problems. Corrections welcome.

JailBee itself only runs on Linux, because the Incus **daemon** is Linux-only. The
supported way to use JailBee from a Mac is therefore to run JailBee **inside a Linux
VM**, keep the git repo on the macOS filesystem, share it into the VM, and
drive JailBee from the macOS terminal through the builtin macOS bridge
(`jailbee mac` commands) — see [below](#recommended-setup-colima--builtin-bridge).

## Why in-VM, not native macOS

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
assume a local Linux display server. All of these hold when JailBee runs *inside*
the Linux VM and break when it runs on macOS against a remote daemon.

So the working model is: **the Mac is where the repo lives and where you type
`jailbee`; the VM is where JailBee, the Incus daemon, and the branch containers all
run — co-located, exactly as JailBee expects.** See
[Architecture](architecture.md) for why co-location matters.

## Recommended setup: Colima + builtin bridge

[Colima](https://colima.run/) wraps Lima and ships an Ubuntu VM with Incus
preinstalled, using the Apple `Virtualization.framework` backend and virtiofs
for host-folder sharing. The built-in JailBee macOS bridge (`jailbee mac` commands)
handles communication automatically — no manual shell wrapper needed.

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

### 4. Use JailBee normally from any repo under your macOS $HOME

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

### Disable the GUI / GPG surface (optional)

macOS has no local Linux display server, so if you need to turn off display
features, edit `.jailbee/config.yaml` (see [config reference](config.md)):

```yaml
gpg:
  enabled: false
jetbrains:
  enabled: false
chrome:
  enabled: false
```

Core JailBee — containers, the host↔container [git bridge](git-bridge.md),
network modes, build/test — is unaffected. Only `jailbee ide` / `jailbee chrome` and
GPG commit signing are lost.

## Known rough edges

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

## Alternative hosts

| Host | Notes |
|---|---|
| **Colima `--runtime=incus`** | Recommended above. Incus preinstalled, least setup. |
| **Lima (vz backend)** | Same engine and kernel as Colima; install Incus yourself (`apt`, or the [zabbly](https://github.com/zabbly/incus) repo for 7.x). More control over guest tuning. |
| **multipass** | Works (`apt install incus`), but host-folder sharing is sshfs/9p only — noticeably slower than virtiofs for I/O-heavy repos. |
| **UTM** | Fully manual OS + Incus install; arm64 virtiofs has been flaky. Not recommended for this workflow. |
| **Apple `container`** | Can host a persistent systemd VM (a "container machine") with an rw `--volume` mount, and its kernel has everything Incus *system* containers need — **except** AppArmor (no LSM at all), loadable modules, and btrfs/zfs (so `dir` storage only, no copy-on-write, and Incus runs unconfined). Viable if you specifically want Apple's own tool and accept the degradation, or build a custom kernel (`--kernel`) with `CONFIG_SECURITY_APPARMOR=y` / `CONFIG_BTRFS_FS=y` / `CONFIG_MODULES=y`. Requires macOS 26. No known precedent of Incus running inside it — expect to debug. |

## What is not supported

- Running JailBee **natively on macOS** against a remote Incus daemon (see
  [Why in-VM](#why-in-vm-not-native-macos)).
- `jailbee ide`, `jailbee chrome`, and GPG commit signing inside containers (no local
  Linux display server / gpg-agent socket to bridge to macOS).

## Manual end-to-end verification (real Apple hardware)

The bridge is unit-tested with a mocked transport; these steps confirm real
behavior and are the acceptance gate before treating the feature as supported.

1. `jailbee version` on macOS with the VM stopped → prints the "VM not running"
   remediation and exits non-zero.
2. `colima start …`, then `jailbee mac bootstrap` → installs JailBee in the VM.
3. `jailbee version` → prints the in-VM version (delegated).
4. `jailbee mac doctor` → all checks OK.
5. In a repo under `$HOME`: `jailbee doctor`, then `jailbee new feat/smoke`, then
   `jailbee shell feat-smoke` → interactive shell works (a pty is requested by
   default, i.e. `tty_flag: ['-t']`; if your colima/ssh version rejects `-t`,
   disable it with `tty_flag: []` in `~/.config/jailbee/macos.yaml`).
6. From a directory OUTSIDE `$HOME` → `jailbee` prints the "must live under" error.
