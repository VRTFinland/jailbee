# Apple container machine with a custom kernel

This recipe runs JailBee and Incus inside an Apple `container machine`, with
repositories and caches on the VM's Linux disk. Use the machine's terminal or
JailBee's SSH service; SFTP moves files between the Mac and a container's
repository.

[`jailbee mac setup`](macos.md#setup-c-apples-container-machine-one-command)
automates everything on this page in one command. Read on for what it does, or
to do it by hand.

This is an experimental setup, tested on one Apple silicon Mac on
2026-10-21. It does not establish compatibility with every project. The tested
machine was created without the Mac home mounted, by the tester's preference;
the recipe does not depend on that, but a machine that shares the Mac home was
not tested. See [Using JailBee from a Mac](macos.md) for the other
arrangements.

The trial ran JailBee 1.7.7. JailBee 1.7.8 fixes three problems the recipe
works around: the GID collision, the fonts mount and the display's cloud-init
race. Each workaround is marked where it appears; on 1.7.8 it is optional
and harmless. From 1.7.8, `jailbee doctor` also reports the kernel gaps
described below: its `kernel bridge conntrack` and `apparmor` rows, and
`kernel btrfs ACLs` once a btrfs pool exists. The recipe has not been re-run
end to end on 1.7.8.

## Tested versions and resources

| Component | Tested |
|---|---|
| macOS | 26.6.2, arm64 |
| Apple `container` | 1.6.0 |
| Machine | 6 CPUs, **12 GiB total VM memory**, home mount `none` |
| Linux userland | Ubuntu 26.04 |
| Incus Ubuntu package | 6.0.5-8 |
| JailBee | 1.7.7, Python 3.14.4 |
| Kernel | Linux 6.18.35, built from Apple's running kernel configuration |
| Storage | btrfs loop pool, 30 GiB |
| RDP client | Windows App 11.4.4 |

The [Apple machine documentation](https://github.com/apple/container/blob/1.6.0/docs/container-machine.md)
describes its init process and capabilities. No `--virtualization` option or
KVM is needed for Incus system containers and nested Docker. Those are Linux
containers inside the machine, not another layer of hardware virtualisation.

### Why the default kernel was insufficient

The default kernel used in this test was 6.18.35. An ordinary unprivileged
Incus container and nested Docker ran, but a real JailBee strict container
failed when Incus installed its per-device nftables rules:

```text
Failed adding bridge filter rules ...
ct state established,related accept
Error: Could not process rule: Protocol error
```

The running configuration had `CONFIG_NF_CONNTRACK_BRIDGE` disabled. It also
lacked AppArmor and btrfs. The custom kernel enables:

```text
CONFIG_NF_CONNTRACK_BRIDGE=y
CONFIG_SECURITY_APPARMOR=y
CONFIG_BTRFS_FS=y
CONFIG_BTRFS_FS_POSIX_ACL=y
```

POSIX ACL support is required as well as btrfs itself: without it, copying an
instance failed with `lsetxattr(..., "system.posix_acl_access")` and
`Operation not supported`. A bridge-wide allowlist probe alone did not expose
the missing connection-tracking feature; create an actual JailBee container.

## 1. Build the Linux host image — on the Mac

Install Apple's signed `container` package from its
[release page](https://github.com/apple/container/releases/tag/1.6.0).
An older installation may not include the `machine` plugin. Check the version
and start the services, allowing installation of the recommended kernel:

```bash
container --version
container system start --enable-kernel-install
container machine --help
```

Wait for kernel installation to finish before building an image. A running
API server alone does not mean the default kernel is configured.

In an empty directory, save this as `Dockerfile`:

```dockerfile
FROM ubuntu:26.04

ENV container=container
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd systemd-sysv dbus dbus-user-session sudo ca-certificates curl \
    openssh-server iproute2 iputils-ping nftables iptables kmod \
    incus incus-client uidmap dnsmasq-base squashfs-tools btrfs-progs \
    attr acl fuse-overlayfs git python3 python3-venv pipx apparmor-utils \
    fonts-dejavu-core \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN truncate -s 0 /etc/machine-id \
    && rm -f /var/lib/dbus/machine-id \
    && systemctl set-default multi-user.target \
    && systemctl mask dev-hugepages.mount sys-fs-fuse-connections.mount \
       systemd-update-utmp.service console-getty.service
```

The fonts package supplies `/usr/share/fonts`, which JailBee 1.7.7's base
profile mounts even for a headless container. Without that source directory,
profile assignment failed before the container could start. (From 1.7.8
JailBee no longer mounts the host's fonts, so the package is optional.)

```bash
container build --progress plain --cpus 4 --memory 4G \
  -t local/jailbee-host:trial .
container machine create --name jailbee --cpus 6 --memory 12G \
  --home-mount none local/jailbee-host:trial
container machine run -n jailbee --root -i -t -- bash -l
```

`--home-mount none` keeps the Mac home out of the VM, which `container
machine` would otherwise share. That was the tester's choice, not a JailBee
requirement; omitting it was not tested.

The builder's 4 GiB and the machine's 12 GiB are separate settings. The
following sections explicitly identify the shell in which commands run.

## 2. Prepare the VM account and build the kernel — as VM root

Set `dev_user` to the Mac short login name used when the machine was created.
The machine creates that account in `/home/<name>` even with home sharing off.

```bash
dev_user=YOUR_MAC_LOGIN
dev_uid=$(id -u "$dev_user")
getent group 1500
```

Choose an unused GID if 1500 is already occupied. The test Mac's primary GID
was 20, which Ubuntu reserves for `dialout`; JailBee's golden-image
provisioning then failed at `groupadd -g 20 dev`. Give the VM account an unused
Linux group. This changes only its VM-local home. (From 1.7.8 the
build renames a system group that already holds the GID instead of failing,
so this step is optional.)

```bash
groupadd --gid 1500 jailbee-dev
usermod --gid jailbee-dev --append --groups incus-admin "$dev_user"
chgrp -R jailbee-dev "/home/$dev_user"
dev_gid=$(id -g "$dev_user")
```

Complete UID/GID delegation and keyring limits as in
[Installation](installation.md#2-delegate-one-uidgid-for-host-file-access):

```bash
for mapping in root:1000000:1000000000 "root:$dev_uid:1"; do
  grep -qxF "$mapping" /etc/subuid || printf '%s\n' "$mapping" >> /etc/subuid
done
for mapping in root:1000000:1000000000 "root:$dev_gid:1"; do
  grep -qxF "$mapping" /etc/subgid || printf '%s\n' "$mapping" >> /etc/subgid
done
cat > /etc/sysctl.d/99-jailbee-keys.conf <<'EOF'
kernel.keys.maxkeys=2000
kernel.keys.maxbytes=2000000
kernel.keys.root_maxkeys=2000
kernel.keys.root_maxbytes=2000000
EOF
sysctl -p /etc/sysctl.d/99-jailbee-keys.conf
systemctl restart incus
loginctl enable-linger "$dev_user"
```

Build the matching Linux source using the running Apple kernel's driver
configuration as the starting point. These versions describe the tested
combination; revalidate when changing the Apple kernel or Linux version.

```bash
apt-get install -y --no-install-recommends build-essential flex bison bc \
  libssl-dev libelf-dev xz-utils cpio pkg-config
curl -fL --retry 2 -o /var/tmp/linux-6.18.35.tar.xz \
  https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.35.tar.xz
mkdir -p /var/tmp/jailbee-kernel
tar -xJf /var/tmp/linux-6.18.35.tar.xz -C /var/tmp/jailbee-kernel
cd /var/tmp/jailbee-kernel/linux-6.18.35
zcat /proc/config.gz > .config
scripts/config --enable NF_CONNTRACK_BRIDGE \
  --enable SECURITY_APPARMOR --enable BTRFS_FS --enable BTRFS_FS_POSIX_ACL \
  --enable DEBUG_INFO_NONE --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
  --disable DEBUG_INFO_DWARF4 --disable DEBUG_INFO_DWARF5 --disable DEBUG_INFO_BTF \
  --set-str LOCALVERSION -jailbee \
  --set-str LSM 'landlock,lockdown,yama,integrity,apparmor,bpf'
make olddefconfig
make -j6 Image
sha256sum arch/arm64/boot/Image
exit
```

Keep the checksum to compare with the exported binary. Compilation takes
several minutes; disabling debug information avoids the additional debug
toolchain and reduces the build size.

## 3. Select the custom kernel — on the Mac

Use a persistent path, not a temporary directory that may be cleaned later:

```bash
jailbee_kernel_dir="$HOME/Library/Application Support/com.apple.container/kernels"
mkdir -p "$jailbee_kernel_dir"
container machine run -n jailbee --root -- \
  cat /var/tmp/jailbee-kernel/linux-6.18.35/arch/arm64/boot/Image \
  > "$jailbee_kernel_dir/vmlinux-6.18.35-jailbee"
shasum -a 256 "$jailbee_kernel_dir/vmlinux-6.18.35-jailbee"
```

After confirming that the checksums match:

```bash
container machine set -n jailbee \
  "kernel=$jailbee_kernel_dir/vmlinux-6.18.35-jailbee"
container machine stop jailbee
container machine run -n jailbee --root -i -t -- bash -l
```

This selects a kernel for this machine; it does not replace Apple's default
kernel. To roll back, stop the machine, run
`container machine set -n jailbee kernel=`, then boot it again. The default
kernel still lacks the features this recipe needs.

## 4. Persist securityfs and initialise Incus — as VM root

```bash
uname -r
```

Expected: `6.18.35-jailbee`. AppArmor must be active, not just compiled in.
The machine did not mount securityfs automatically. Adding it to `/etc/fstab`
failed at boot in `systemd-remount-fs.service`; use an early systemd service:

```bash
cat > /etc/systemd/system/jailbee-securityfs.service <<'EOF'
[Unit]
Description=Mount securityfs for JailBee's Incus host
DefaultDependencies=no
Before=sysinit.target apparmor.service incus.service
ConditionPathIsMountPoint=!/sys/kernel/security

[Service]
Type=oneshot
ExecStart=/bin/mount -t securityfs securityfs /sys/kernel/security
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
EOF
systemctl daemon-reload
systemctl enable --now jailbee-securityfs.service
systemctl restart apparmor
systemctl restart incus
cat /sys/kernel/security/lsm
cat /sys/module/apparmor/parameters/enabled
```

Expected: the LSM list includes `apparmor`, and its enabled parameter is `Y`.
On a fresh Incus installation:

```bash
incus admin init --auto --storage-backend dir
incus storage create jailbee-btrfs btrfs size=30GiB
exit
```

Inspect an existing installation instead of reinitialising it. The `dir`
pool remains available; use `jailbee-btrfs` for JailBee environments. See
[Storage](storage.md) for capacity and copy-on-write behaviour.

### Disk space on the Mac

`size=30GiB` is a ceiling, not an allocation. The pool is a sparse loop file
on the machine's disk, which is itself a sparse image file on the Mac, so
both grow only as containers write data. Raise the ceiling later with
`incus storage set jailbee-btrfs size=…`.

Freed space returns to the Mac only when discards travel down the whole
chain. btrfs mounts with `discard=async` by default, so a deleted file frees
the loop file's blocks within seconds. The machine's ext4 root then has to
pass the freed blocks on, either continuously or in batches:

```bash
mount -o remount,discard /          # continuous, until the next boot
systemctl enable --now fstrim.timer # weekly batch
```

Run `fstrim -av` twice to reclaim space at once: the first pass trims btrfs,
and the outer ext4 can only trim the blocks that pass released after its
journal commits. This chain was measured on 2026-10-22 in a Virtualization
framework VM attached the way Apple's `containerization` attaches disks, not
in a `container machine`: a deleted 512 MiB file left the Mac within 20
seconds with `discard` on the ext4 mount, and Virtualization framework did
punch holes in the APFS image. Whether a `container machine` root accepts
the `discard` remount has not been tested; to check, compare `du -h` of the
machine's disk image on the Mac before and after deleting a large file.

## 5. Enter as the development user and create an environment

On the Mac:

```bash
container machine run -n jailbee --root -i -t -- runuser -l "$(id -un)"
```

Use this login form for day-to-day work. A direct `container machine run`
command used only the account's configured primary group in the test; it
omitted supplementary `incus-admin` membership. `runuser -l` loads the Linux
account's updated primary and supplementary groups. Check `id` in the shell.

Inside the VM, as that user:

```bash
pipx install 'jailbee[ssh]>=1.7.8'
pipx ensurepath
export PATH="$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
mkdir -p ~/.config/jailbee
cat > ~/.config/jailbee/global.yaml <<'EOF'
defaults:
  storage_pool: jailbee-btrfs
EOF
```

Use a VM-local clone of your project, or create a disposable fixture:

```bash
mkdir ~/jailbee-smoke
cd ~/jailbee-smoke
git init -b main
git config user.name 'JailBee smoke test'
git config user.email 'jailbee-test@example.invalid'
git config remote.pushDefault origin
mkdir .jailbee
cat > .jailbee/config.yaml <<'EOF'
container_prefix: apple-smoke
golden:
  ubuntu_version: "26.04"
  stacks: {}
gpg:
  enabled: false
egress_allow:
  - example.com:443
EOF
git add .jailbee/config.yaml
git commit -m 'Initial smoke fixture'
jb init
jb setup --only timer --yes
jb base build
jb new smoke main --storage jailbee-btrfs --no-attach --no-autostart
jb exec smoke -- id
jb exec smoke -- git status --short --branch
```

This fixture deliberately has no project toolchains or agent credentials.
Follow [project configuration](project-config.md) for a real stack. A source
repository that exists only under a Mac path such as `/Volumes/workspace/...`
is not available inside a machine created with `--home-mount none`.

## 6. Test the boundaries and reboot

From the VM's development-user shell:

```bash
jb exec smoke -- curl -fsSI --connect-timeout 3 --max-time 5 https://example.com
jb exec smoke -- curl -sSI --connect-timeout 3 --max-time 5 https://1.1.1.1
curl -fsSI --connect-timeout 3 --max-time 5 https://1.1.1.1
```

The allowlisted URL must succeed. The unlisted destination must fail inside
the container and remain reachable from the VM itself. A timeout alone is
not proof of the ACL if that destination is also unreachable from the VM.

Check the AppArmor label **from the VM host**, using the container's host PID:

```bash
instance_pid=$(incus query /1.0/instances/apple-smoke-smoke/state \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["pid"])')
sudo cat "/proc/$instance_pid/attr/current"
incus config device get apple-smoke-smoke root pool
jb snapshot create smoke verified
```

The host-side label should include `incus-apple-smoke-smoke` and `(enforce)`;
the pool should be `jailbee-btrfs`. Inside a nested AppArmor namespace,
`/proc/self/attr/current` can say `unconfined` while the outer Incus profile
is enforced, so the inside view is insufficient evidence.

Stop and boot the machine from the Mac. Once its system bus is ready, run
`systemctl is-system-running --wait` inside the VM. In the test, a command
issued immediately after boot sometimes preceded the system bus; retry after
boot rather than treating that error as a failed Incus installation.

Repeat the network, host-side label and storage checks, then check:

```bash
systemctl --user is-active jailbee-net-refresh.timer
sysctl kernel.keys.maxkeys
jb ls
```

The timer should be active, the quota at least 1000, and the environment
running. Nested Docker was also tested in an unprivileged Incus probe with
`security.nesting=true`, using Ubuntu's `docker.io` package and
`docker run --rm hello-world`, including after a machine reboot.

## Remote commands, SFTP and RDP

Configure key-only access to the VM's ordinary
OpenSSH server, then use it as the jump host to JailBee's loopback SSH service.
Authorise a public key and start the JailBee service from the VM user shell:

```bash
jb remote ssh key add /path/to/mac-public-key.pub
jb remote ssh enable
```

Use the [Mac SSH configuration](macos.md#2-on-the-mac-one-ssh-config-entry),
with the VM's current IP and development user as `ProxyJump`. Get the current
IP with `container machine ls`; it changed across reboots in this test.
The key must be authorised separately by the VM's ordinary SSH server and
by JailBee. Do not expose JailBee's loopback listener just to avoid the jump.

With that SSH alias, the verified command and file paths are:

```bash
ssh jb help
ssh jb repos
ssh jb ls --repo apple-smoke
ssh -t jb shell smoke --repo apple-smoke
sftp jb
```

SFTP lists containers by their full name, for example
`/apple-smoke-smoke/`, and restricts access to their repo directories. A UTF-8
upload/download round trip passed. This transfers files; it does not provide
a live Mac bind mount. A diagnostic macOS bridge configuration reached the
VM and printed the installed version. That does not establish access to the
Mac repository: neither a version command nor `jailbee mac doctor` verifies
that the working directory contains the same files inside the VM. No bridge
project workflow was verified in this setup.

For GUI testing, add this to the project's `.jailbee/config.yaml`:

```yaml
browsers:
  firefox:
    enabled: true
    source: image
```

Linux Google Chrome's image installer rejects arm64; the Firefox installer
supports it. Add this alongside `defaults` in the VM user's `global.yaml`:

```yaml
remote:
  ssh:
    rdp: true
```

In the VM's development-user shell, rebuild the image, create a fresh
environment, and start a loopback HTTP server for the browser to reach:

```bash
jb base build
jb new gui-test main --storage jailbee-btrfs --no-attach --no-autostart
jb exec gui-test -d -- python3 -m http.server 8765 --bind 127.0.0.1
jb display up
```

The first attempt in this test could not locate `weston` and `waypipe`.
Cloud-init subsequently reported completion; refreshing package indexes and
retrying `jb display up` succeeded. (From 1.7.8 `jb display up`
waits for cloud-init itself, so this retry should not be needed.)

```bash
sudo incus exec jailbee-display -- cloud-init status --wait
sudo incus exec jailbee-display -- apt-get update
jb display up
```

Follow [Remote GUI](remote-gui.md#connect) to tunnel the display to Windows
App. The test used local port 33990 to avoid a possible 3389 collision:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:33990:127.0.0.1:13389 jb
```

Connect Windows App to `127.0.0.1:33990`. Launch Firefox through the JailBee
SSH service, not through a plain machine command:

```bash
ssh jb firefox gui-test http://127.0.0.1:8765 --repo apple-smoke
```

The URL reaches the HTTP server inside that environment. Windows App showed
the Weston desktop, and Firefox requested a page from the container's own
loopback successfully in the test.

The creation command emitted a logind runtime-directory timeout warning for
the headless environment. The subsequent explicit SSH/RDP Firefox launch
succeeded. Do not treat a background launch message alone as a working GUI:
verify the visible window, typing, and clipboard in both directions.

## Native Mac windows with Cocoa-Way

The same machine also accepted a waypipe connection from
Cocoa-Way 2.1.1 with waypipe-darwin 0.11.2 on the Mac; the display container
had waypipe 0.11.0. Firefox loaded its container-local HTTP page, and the
compositor logged creation of a rootless Mac window and presentation of its
first frame. The test form received the expected pasted and typed tokens.
The reverse-direction token also matched the Mac clipboard, confirming text
clipboard transfer in both directions in this trial.

Install Cocoa-Way and waypipe-darwin and define `cwaypipe` using the
[Mac native-window instructions](macos.md#4-on-the-mac-native-windows-optional).
Start the compositor on the Mac:

```bash
COCOA_WAY_PRESENTATION=rootless cocoa-way
```

In another Mac terminal, with the same SSH alias and the test HTTP server
still running in `gui-test`:

```bash
cwaypipe -o ControlPath=none jb firefox gui-test \
  http://127.0.0.1:8765 --repo apple-smoke --move
```

`--move` closes this test browser on its previous display and opens it on
Cocoa-Way. The SSH command stays attached; ending it closes the window.
JailBee runs the server-side waypipe in `jailbee-display`, so installing it
in the VM host or the project container is unnecessary. This path uses the
existing SSH jump host and does not require a Mac home mount or RDP client.

Verify the visible window, typing and clipboard in each direction before
using it for daily work. `cocoa-wayctl --json clipboard` reports transfer
metadata without printing clipboard contents.

### Finnish keyboard layout in Cocoa-Way 2.1.1

The first manual trial passed ASCII typing and text clipboard transfer but
used a US keyboard layout despite Finnish being selected in macOS.
[Cocoa-Way 2.1.1 constructs an explicit US XKB layout](https://github.com/J-x-Z/cocoa-way/blob/v2.1.1/src/state.rs#L319-L325),
so changing the Mac input source or setting `XKB_DEFAULT_LAYOUT` does not
replace that explicit setting.

For a temporary Finnish Mac trial, use a private copy of the XKB data. This
workaround changes the compositor's interpretation of `us` only in that copy;
it does not modify the installed data or macOS keyboard settings:

```bash
cocoa_xkb=$(mktemp -d /tmp/jailbee-xkb.XXXXXX)
cp -R /opt/homebrew/share/X11/xkb/. "$cocoa_xkb/"
cat > "$cocoa_xkb/symbols/us" <<'EOF'
default partial alphanumeric_keys
xkb_symbols "basic" {
    include "fi(mac)"
    include "level3(alt_switch)"
};
EOF
COCOA_WAY_PRESENTATION=rootless XKB_CONFIG_ROOT="$cocoa_xkb" cocoa-way
```

Close the previous test compositor before launching this one, then reconnect
the waypipe browser. The Finnish Mac keymap compiled with the expected
`ä`, `ö`, `å`, `Ä`, Option-8 `[` and Shift-Option-8 `{` symbols, and keyboard
input was confirmed manually with this override. Exhaustive coverage of every
shortcut and input source was not tested. This is a fixed XKB Finnish
Macintosh layout, not automatic synchronisation with arbitrary macOS input
sources. Quit this compositor and start without `XKB_CONFIG_ROOT` to return
to the installed behaviour.

## Scope of verification

| Path | Result |
|---|---|
| Golden image, branch clone, mapped user, git bridge | Passed |
| Strict per-container egress, positive and negative destinations | Passed |
| Host-side AppArmor enforcement after reboot | Passed |
| btrfs instance migration and snapshots, retained after reboot | Passed |
| `jailbee mac setup` end to end, both modes, upgrade and uninstall (2026-10-22, 1.6.0) | Passed on one Mac, with the machine's own `container machine` root measured: `fstrim` and a `discard` mount both shrink the Mac's disk image |
| Native Mac windows through `jailbee mac setup` | Not checked: needs a graphical login |
| Freed btrfs space returned to the Mac | Measured in a separate Virtualization framework VM, not in a `container machine` |
| Nested Docker `hello-world` after reboot | Passed |
| User refresh timer and keyring limits after reboot | Passed |
| SSH repository discovery, listing and interactive shell | Passed |
| Restricted SFTP UTF-8 round trip | Passed |
| Windows App desktop | Visually confirmed |
| Firefox page request through SSH/RDP | Passed, including typed and pasted input |
| Clipboard Mac → container | Passed |
| Clipboard container → Mac | Failed in the initial manual test; menu-copy retest pending |
| Native macOS bridge | Transport/version probe only; no project workflow verified |
| Live Mac repo mounts | Not tested: the test machine did not share the Mac home |
| Cocoa-Way/waypipe Firefox | Page, rootless window and ASCII input passed; default layout is US |
| Finnish Mac keyboard workaround | XKB symbols verified; keyboard operation confirmed manually |
| Cocoa-Way text clipboard, both directions | Passed: form token and Mac clipboard token verified |
| JetBrains and full application stacks | Not tested |

The normal Apple machine runner did not initialise supplementary groups;
the login command above avoids that issue. When automating checks, save a
script inside the VM and execute that file. Streaming a script into `bash -s`
and running `incus exec` inside it let the child consume the remaining script
from stdin, silently skipping later checks.
