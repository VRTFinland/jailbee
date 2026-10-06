# Storage: use btrfs

JailBee gives every branch its own system container, and each container carries
a full OS plus your toolchain. The Incus storage backend decides what that costs
on disk. **On a host that runs more than a few containers, use a `btrfs` pool.**

## Why not the default pool

`incus admin init --auto` creates a pool with the `dir` driver on most hosts.
A `dir` pool is just a directory tree, so:

- **Every container is a full copy** of the golden image's root filesystem.
  Containers share nothing with the image or with each other.
- **Every snapshot is a full copy** too.
- Creating a container copies the whole tree, so `jailbee new` is slower.

Ten containers of an 8 GiB image therefore cost about 80 GiB, and the cost
grows with every branch you keep around.

A `btrfs` pool is copy-on-write. A new container is a clone of the image that
shares every block until the container writes to it, so only what the container
changes costs space. Snapshots work the same way, and cloning is near-instant.

| | `dir` | `btrfs` |
|---|---|---|
| New container | Full copy | CoW clone, shares blocks with the image |
| Snapshot | Full copy | Changed blocks only |
| Cost of N idle containers | N × image | About 1 × image |
| Transparent compression | No | Yes (`zstd`) |
| Setup effort | None (default) | A dedicated filesystem |
| Redundancy on one disk | None | None (checksums detect, cannot repair) |

`zfs` and `lvm` also work and give the same CoW behaviour, but `btrfs` is in the
Ubuntu kernel and needs no extra module, which makes it the simplest choice.

## Check what you have

```bash
incus storage list      # DRIVER column: dir, btrfs, zfs, lvm
```

Two numbers that look unrelated often explain each other on a `dir` pool:
`/var/lib/incus/storage-pools/<pool>` holds the containers and snapshots, while
`/var/lib/incus/images` holds the golden image tarballs, including every dated
archive that `jailbee base build` left behind. `jailbee disk-usage` reports
both; run it as root (`sudo jailbee disk-usage`), because the pool directories
are root-only and show `n/a` otherwise.

## Setting up a btrfs pool

This is a one-time host operation. It assumes a dedicated disk or partition
that you can wipe (or a path on a filesystem that is already btrfs). Commands
need root.

> **Warning:** `luksFormat` and `mkfs.btrfs` destroy everything on the device.
> Check the device name with `lsblk -o NAME,SIZE,MODEL,MOUNTPOINT` before you
> run them.

### 1. Encrypt the device (optional but recommended)

```bash
cryptsetup luksFormat --type luks2 /dev/nvme1n1
cryptsetup open --allow-discards /dev/nvme1n1 storage
```

`--allow-discards` lets TRIM reach the SSD, at the cost of revealing which
blocks are in use. Drop it if that matters to you and rely on another trim
policy. Skip this step entirely if you do not want encryption; then use the
raw device in the next step.

### 2. Create the filesystem and a subvolume

```bash
mkfs.btrfs -L incus /dev/mapper/storage
mkdir -p /storage
mount -o noatime,compress=zstd:1 /dev/mapper/storage /storage
btrfs subvolume create /storage/incus-pool
```

The mount options are recommendations, not requirements:

- `compress=zstd:1` is the one that matters. It shrinks container content
  noticeably for little CPU cost, and it only applies to data written *after*
  the option is set, so set it before you create containers.
- `noatime` avoids a write on every read, which is slightly more worthwhile on
  a copy-on-write filesystem. `defaults` works too.

### 3. Make the mount permanent

Find the UUIDs with `blkid /dev/nvme1n1` (LUKS) and `blkid /dev/mapper/storage`
(btrfs), then:

```text
# /etc/crypttab
storage  UUID=<luks-uuid>  none  luks,discard

# /etc/fstab
UUID=<btrfs-uuid>  /storage  btrfs  noatime,compress=zstd:1,x-systemd.device-timeout=30  0  0
```

Test the fstab line before rebooting: `umount /storage && mount -a && findmnt
/storage`. A broken fstab can drop the boot into emergency mode.

Incus must not start before the filesystem is there, or the pool fails to come
up and no container starts:

```bash
systemctl edit incus.service
```

```ini
[Unit]
RequiresMountsFor=/storage
```

An encrypted device asks for its passphrase at boot, or needs a keyfile on an
encrypted root. Plan for that on a headless host.

### 4. Create the Incus pool

```bash
incus storage create cow btrfs source=/storage/incus-pool
```

Pointing `source` at a subvolume of an already-mounted filesystem means Incus
does not format anything and the mount options above apply. Giving it a raw
device instead makes Incus format that device itself, and then compression is
configured through the pool's `btrfs.mount_options` instead of fstab.

Test it on its own, without touching the default profile:

```bash
incus launch <image> cow-test -s cow
incus storage info cow
incus delete -f cow-test
```

## Making JailBee use it

By default JailBee does not pick a pool itself: it creates containers with
`incus init` and no pool, so a container lands on the **root disk pool of
Incus's `default` profile**. You can steer that in two ways.

**Per host, in `global.yaml`** (switchable at any time, and the way back):

```yaml
# ~/.config/jailbee/global.yaml
defaults:
  storage_pool: cow
```

**Per container**, for one-off tests or to land a single container on the old
pool:

```bash
jailbee new feat-x --storage cow
```

`--storage` beats `defaults.storage_pool`, which beats the profile. A pool name
that does not exist stops `jailbee new` with exit 2 and lists the pools that do.
Pool names are per-host, so keep `storage_pool` out of a repo's committed
`.jailbee/config.yaml`; a repo's local layer (`repos/<prefix>.yaml`) is fine.

Only **new** containers are affected. A container keeps the pool it was created
on whatever you set later, so you can switch to `cow`, create a few containers,
and switch back by removing the key; nothing moves and nothing breaks.

JailBee's helper containers (`jailbee-egress-proxy`, `jailbee-litellm`, the
registry mirror, the shared display) and the LiteLLM state volume are not
covered by this setting: they follow the `default` profile's root disk. To
retire the old pool entirely, point the profile at the new one too:

```bash
incus profile device set default root pool=cow
incus profile show default       # root: pool: cow
```

Do that when no container is running. Containers without their own root disk
inherit it from the profile, and changing the pool under a running container is
not something to attempt.

### Starting clean (recommended)

Existing containers do not become CoW by being moved: `incus move --storage`
copies each one into an independent subvolume, so they share nothing with the
image and the space saving does not appear. Only containers created *after* the
switch are clones. For disposable branch containers it is simplest to start over.

1. Make sure nothing you need exists only inside a container: push or copy the
   work out first.
2. If you use LiteLLM, copy its state volume to the new pool, since the old
   pool takes it along:
   ```bash
   incus storage volume copy default/jailbee-litellm-state cow/jailbee-litellm-state
   ```
3. Stop and remove the containers. Use `jailbee destroy` for branch containers
   so JailBee's own bookkeeping is cleaned up, then `incus delete --force` for
   the helper containers (`jailbee-egress-proxy`, `jailbee-litellm`).
   `incus list` should then be empty.
4. Switch the profile (above) and, if you set one, `defaults.storage_pool`.
5. Remove the old pool, which frees its space:
   ```bash
   incus storage volume delete default jailbee-litellm-state   # if you copied it
   incus storage delete default
   ```
6. Prune dated image archives: `jailbee base prune --all --days 14`. Golden
   images live outside any pool, so the current ones keep working; Incus unpacks
   each into the new pool the first time it is used.
7. Re-apply and verify: `jailbee apply`, `jailbee doctor`, then `jailbee new`.

### Moving a container you want to keep

```bash
incus stop <container>
incus move <container> --storage cow
```

Snapshots come along. The container keeps working but is a full, unshared copy,
so only do this for containers that hold state you cannot recreate. If your
Incus version rejects `--storage`, `incus copy <container> <container>-new -s
cow` followed by deleting the original does the same job.

## Verify that copy-on-write works

```bash
incus storage info cow
btrfs filesystem du -s /storage/incus-pool/containers/*
```

For a container created from an image, `btrfs filesystem du` reports most of its
size as *shared* and only a small *exclusive* part. If the exclusive part is as
large as the container, it is not a clone.

## Things to know

- **No redundancy.** On one disk btrfs detects corruption through checksums but
  cannot repair data (metadata is duplicated by default). Keep work pushed or
  backed up.
- **Image tarballs stay.** `/var/lib/incus/images` holds the golden images'
  tarballs regardless of driver. They shrink only when you delete old images
  (`jailbee base prune`).
- **`jailbee disk-usage` and btrfs.** The "Containers" and "Container
  snapshots" rows measure `<pool source>/containers` and
  `<pool source>/containers-snapshots`. On a btrfs pool whose source is a
  subvolume the layout differs, so treat those rows as unreliable and use
  `incus storage info <pool>` and `btrfs filesystem du` instead.
- **Nested Docker works.** Docker inside a container uses `overlay2`, which runs
  fine on a btrfs-backed container.
- **Avoid btrfs quotas** unless you need them; they slow filesystem operations
  down.

See also [Installation](installation.md#1-install-incus-and-initialise-it) for the
initial Incus setup and [Troubleshooting](troubleshooting.md).
