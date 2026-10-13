# Storage: use btrfs

JailBee gives every branch its own system container, and each container carries
a full OS plus your toolchain. The Incus storage backend decides what that costs
on disk. **On a host that runs more than a few containers, use a `btrfs` pool.**

## Why not the default pool

`incus admin init --auto` creates a pool with the `dir` driver on most hosts.
A `dir` pool is just a directory tree, so:

- **Every container is copied** from the golden image's root filesystem;
  the driver does not use native storage snapshots to share it.
- **Every snapshot is copied** too.
- Creating a container copies the whole tree, so `jailbee new` is slower.

Ten containers with an 8 GiB unpacked root filesystem therefore cost roughly
80 GiB before workload data and snapshots. This is not the compressed image
archive size; filesystem compression and sparse files also affect disk use.

A `btrfs` pool is copy-on-write. Within the same pool, a new container is a
snapshot of the unpacked image volume and initially shares its data extents.
Writes allocate new extents, while snapshots retain the old ones. The storage
clone is fast, but `jailbee new` still has to configure and boot the container.
Different golden-image builds and different pools do not automatically share
all their data.

| | `dir` | `btrfs` |
|---|---|---|
| New container | Full copy | CoW clone, shares blocks with the image |
| Snapshot | Full copy | Shares extents; retains old data after writes |
| Cost of N fresh containers from one image | Roughly N × unpacked image | Roughly one unpacked image plus each container's changes |
| Transparent compression | Depends on underlying filesystem | Yes (`zstd`) |
| Setup effort | Directory on an existing filesystem | Btrfs filesystem, device or loop-backed pool |
| Protection from disk failure on one disk | None | None; checksums detect corruption, but repair needs another good copy |

`zfs` and thin-provisioned `lvm` pools also support space-efficient clones and
snapshots. Regular LVM without a thin pool does not have the same behavior.
Btrfs support is in the Ubuntu kernel, making it a convenient choice without
an additional filesystem module. See the Incus
[Btrfs](https://linuxcontainers.org/incus/docs/main/reference/storage_btrfs/)
and [LVM](https://linuxcontainers.org/incus/docs/main/reference/storage_lvm/)
driver references.

## Check what you have

```bash
incus storage list      # DRIVER column: dir, btrfs, zfs, lvm
```

Two numbers that look unrelated often explain each other on a `dir` pool:
`/var/lib/incus/storage-pools/<pool>` holds the containers and snapshots, while
`/var/lib/incus/images` holds the golden image tarballs, including every dated
archive retained by `jailbee base build`. `jailbee disk-usage` reports a
"Golden images" total from Incus's image-size metadata, not a measurement of
that directory; despite the label, it includes all images visible to Incus.
Its container and snapshot rows need access to root-owned pool directories.
Permission failures appear as `n/a`; a missing measured path can instead
appear as zero. For root-only filesystem measurements, run the relevant
`du` or btrfs command with `sudo` rather than treating an inaccessible row as
zero disk use.

## Setting up a btrfs pool

This is a one-time host operation. The device recipe below assumes a dedicated
disk or partition you can erase. Run its host administration commands as root;
run JailBee commands later as your normal development user. Install
`btrfs-progs` and, for encryption, `cryptsetup` if they are not already present.

If you already have a mounted btrfs filesystem, **skip device encryption and
formatting**. Create an empty subvolume there, ensure the filesystem is mounted
before Incus starts, and continue with step 4 using that subvolume's path.

> **Warning:** `luksFormat` and `mkfs.btrfs` destroy data on the target device.
> `/dev/nvme1n1` is only an example, not a detected spare disk. Check the device
> and all its partitions with `lsblk -o NAME,SIZE,MODEL,FSTYPE,MOUNTPOINTS`,
> confirm nothing uses it, and back up anything needed before proceeding.

### 1. Encrypt the device (optional but recommended)

```bash
cryptsetup luksFormat --type luks2 /dev/nvme1n1
cryptsetup open --allow-discards /dev/nvme1n1 storage
```

`--allow-discards` permits TRIM through the encrypted mapping, at the cost of
revealing allocation patterns. It does not schedule trimming. If that leakage
matters, omit both this option and `discard` from the crypttab example below;
periodic TRIM through the same mapping is not a way around the trade-off.
Skip this step if you do not want encryption; use the raw device in step 2
and omit the crypttab entry in step 3.

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
- `noatime` avoids access-time updates. The normal `relatime` default already
  reduces their frequency; it does not write an access time on every read.

Most btrfs mount options apply to the whole filesystem, not independently to
each subvolume. On an existing shared filesystem, review the effects on other
subvolumes before changing compression or mount options.

### 3. Make the mount permanent

Find the UUIDs with `blkid /dev/nvme1n1` (LUKS) and `blkid /dev/mapper/storage`
(btrfs), then:

```text
# /etc/crypttab
storage  UUID=<luks-uuid>  none  luks,discard

# /etc/fstab
UUID=<btrfs-uuid>  /storage  btrfs  noatime,compress=zstd:1,x-systemd.device-timeout=30  0  0
```

Reload systemd's generated units with `systemctl daemon-reload`. Before
creating the Incus pool, and only while nothing uses `/storage`, test its
fstab entry: `umount /storage && mount /storage && findmnt /storage`. Do not
unmount an existing active pool to run this test. This checks the filesystem
mount, not boot-time LUKS unlocking; plan a supervised reboot before relying
on unattended startup. A broken fstab can drop the boot into emergency mode.

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
incus launch images:ubuntu/26.04 cow-test -s cow -c security.nesting=true
incus storage info cow
incus delete -f cow-test
```

## Making JailBee use it

By default, new branch containers use the **root disk pool of Incus's
`default` profile**: JailBee calls `incus init` without an explicit pool.
You can override that in two ways.

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

Only **new** branch containers are affected. Existing containers stay on their
original pool. Removing the key restores the profile fallback for future
containers; it does not move existing storage.

**Golden-image builds are separate:** `jailbee base build` launches its build
container without a storage override, so it still uses the `default` profile's
root pool. `defaults.storage_pool` does not redirect that build container.

The host-wide helper containers (`jailbee-egress-proxy`, `jailbee-litellm`, the
registry mirror, the shared display) and the LiteLLM state volume follow the
`defaults.storage_pool` of **`global.yaml` only**: they belong to no repo, so a
repo's own override does not move them. They are created on that pool when they
do not exist yet; an existing helper stays where it is until it is recreated:

| Helper | Recreate on the configured pool |
|---|---|
| egress proxy | `jailbee net egress proxy up --recreate` |
| registry mirror | `jailbee registry up --recreate` (cache and CA live on the host and survive) |
| shared display | `jailbee display up --recreate` |
| LiteLLM | `jailbee litellm up --recreate` (the state volume is kept) |

**Copy LiteLLM state before changing both pool selections.** JailBee searches
for its state volume in the configured global pool first, then the `default`
profile's root pool. It does not search every pool. If neither selected pool
contains the old volume, startup can create a new empty one even though the
old volume still exists elsewhere. Find the actual source pool, stop LiteLLM,
and copy its state as described below before recreating the helper.

To retire the old pool entirely, the `default` profile must stop pointing at it
too, otherwise Incus will not delete it:

```bash
incus profile device set default root pool=cow
incus profile show default       # root: pool: cow
```

Incus refuses this while any container still takes its root disk from the
profile (`At least one instance relies on this profile's root disk device`),
even a stopped one. Containers created with an explicit storage selection
have their own root disk override; older instances may still inherit it.
Inspect `incus profile show default` and `incus storage show <old-pool>` to
find users. Move any instances you need to keep, or deliberately destroy
only disposable ones, before changing the profile. Other profiles and
custom volumes can also keep the old pool in use.

### Starting clean (recommended)

Moving a container from `dir` to `btrfs` gives it a btrfs subvolume, but does
not retroactively share its existing data with the golden image or sibling
containers. Future snapshots can still benefit from CoW. For disposable branch
containers, recreating them from the image usually saves more space.

The example below uses `cow` as the new pool. Replace `<old-pool>` with the
actual source; do not assume it is named `default`. Schedule downtime for
shared helpers, and keep backups outside the pool you intend to remove.

1. Inspect `incus storage list`, `incus storage show <old-pool>` and
   `incus storage volume list <old-pool>`. Identify branch containers, helpers,
   custom volumes and profiles that use it, including non-JailBee resources.
   Save any uncommitted work and state that exists only inside a container.
2. If you use LiteLLM, inspect `incus config show jailbee-litellm --expanded`
   to find the state mount's pool. If it is already `cow`, skip the state copy
   and do not delete that volume in step 6. Otherwise, pause its clients and
   jobs until verification is complete. **Stop the helper before copying its state** so the database
   and other files are not changing during the copy:
   ```bash
   incus stop jailbee-litellm
   incus storage volume copy <state-pool>/jailbee-litellm-state cow/jailbee-litellm-state
   incus storage volume show cow jailbee-litellm-state
   ```
   Keep the source volume and a backup until the recreated service has been
   verified. If the destination already exists, investigate its contents;
   do not delete it just to make the copy command succeed.
3. Set `defaults.storage_pool: cow` in `global.yaml`. Use the `up --recreate`
   commands above for the helpers you use. They recreate the container, not
   arbitrary state stored in its root filesystem; back up any such state
   first. LiteLLM's `up --recreate` starts the service and can write state
   immediately, so keep clients paused. Verify that it mounts the copied
   volume from `cow` and retains its expected state before resuming clients.
   The old copy does not include writes made after the copy; rolling back to
   it discards those changes.
4. Run `jailbee destroy <name>` for each disposable branch container in its
   owning repo, so JailBee's bookkeeping is cleaned up. Move containers and
   other resources you need to keep instead of deleting them. Do not aim for
   an empty `incus list`: unrelated instances can remain. Once no instance
   inherits the old root device, switch the `default` profile as shown above.
5. Run `jailbee apply` for the affected repositories, then `jailbee doctor`
   and create a new branch container with `jailbee new <branch>`. Check its
   root device with `incus config show <container> --expanded`, and verify
   the helpers on the new pool. Keep the old pool until these checks pass.
6. Only after verification, remove old custom volumes you no longer need.
   If you migrated LiteLLM state, this includes the original volume:
   ```bash
   incus storage volume delete <state-pool> jailbee-litellm-state
   ```
   Recheck `incus storage show <old-pool>` and its volume list. Resolve any
   remaining references deliberately, then remove the retired pool:
   ```bash
   incus storage delete <old-pool>
   ```
7. Optionally prune old JailBee image archives:
   `jailbee base prune --all --days 14`. This asks to remove dated archives
   older than 14 days for registered repos, not every Incus image. Current
   live aliases are kept. Image archives are separate from the unpacked
   image volumes in each pool; Incus can unpack a retained image into the
   new pool on first use.

### Moving a container you want to keep

```bash
incus stop <container>
incus move <container> --storage cow
```

Use the full Incus instance name and take a backup first. Snapshots come along
unless explicitly excluded. Moving from `dir` does not deduplicate existing
data against images on the new pool, but subsequent writes and snapshots use
btrfs CoW. Inspect the resulting root disk, then start and verify the container.

Keep the original instance name: copying to `<container>-new` and deleting the
original is not an equivalent JailBee migration, because JailBee's records
still refer to the original name. If your Incus version rejects the move,
consult its `incus move --help` and the
[Incus volume-migration guide](https://linuxcontainers.org/incus/docs/main/howto/storage_move_volume/)
rather than substituting a differently named container. Separately attached
custom volumes are not moved merely by moving the instance's root disk.

## Verify that copy-on-write works

```bash
incus storage info cow
sudo btrfs filesystem usage /storage
sudo btrfs filesystem du -s /storage/incus-pool/containers/
```

These paths assume the subvolume-source recipe above. `filesystem usage`
reports allocation for the whole btrfs filesystem, including any non-Incus
data on it. `filesystem du` traverses the container subvolumes and reports
extent sharing; it is not a compressed physical-disk-usage total.

A fresh container from an image should have substantial shared data and a
small exclusive part. As it changes, its exclusive data grows. A large
exclusive figure does not prove it was never cloned, and sharing alone does
not identify which image or snapshot shares the extents. See the
[btrfs filesystem reference](https://btrfs.readthedocs.io/en/latest/btrfs-filesystem.html).

## Things to know

- **One disk is not a backup.** Checksums detect corruption, but repair needs
  another good copy. The usual single-device layout duplicates metadata, not
  file data; neither protects against losing the disk. Keep work backed up.
- **Image tarballs stay.** `/var/lib/incus/images` holds the golden images'
  tarballs regardless of driver. They shrink only when you delete old images
  (`jailbee base prune`).
- **`jailbee disk-usage` and btrfs.** The "Containers" and "Container
  snapshots" rows run `du -sb` on `<pool source>/containers` and
  `<pool source>/containers-snapshots`, summed across pools. These are apparent
  sizes, not exclusive CoW usage or compressed physical allocation. A device
  or loop-file source is not a directory to measure this way. Use
  `incus storage info <pool>` and the btrfs commands above for storage analysis.
- **Nested Docker storage depends on the installed Docker version.** Btrfs is
  a supported backing filesystem for `overlay2`, but fresh Docker Engine 29+
  installations default to the containerd image store. Check `docker info`
  inside the container rather than assuming a driver. See
  [Docker storage drivers](https://docs.docker.com/engine/storage/drivers/select-storage-driver/).
- **Quotas have a cost.** Btrfs qgroup accounting can slow filesystem
  operations. Incus uses it for storage limits, so do not disable it blindly
  on a managed pool; review the Incus driver documentation before changing
  quota settings.

See also [Installation](installation.md#1-install-incus-and-initialise-it) for the
initial Incus setup and [Troubleshooting](troubleshooting.md).
