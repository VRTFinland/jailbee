# Troubleshooting

## Start with `jailbee doctor`

Run `jailbee doctor` from inside your repo. It checks the host and your config
and names most problems (missing bridge, keyring quota, Incus reachability,
GitHub token shape, …) with a remediation hint. Fix what it reports first —
the sections below expand on the ones that need host changes.

## Common problems

### "Run `jb base build` in this repo to pick these up"

`jailbee ls`, `jailbee new` and `jailbee shell` print a short block on stderr,
and `jailbee doctor` reports the same thing as its `upgrade actions` check,
when the version of JailBee you just upgraded to changed something a golden
image or a set of Incus profiles already on your machine does not have yet.
Neither is rebuilt automatically, so the hint names what changed and the one
command that picks it up — run it in the repo it appeared in.

It is only a hint: nothing is blocked, and everything keeps working off the
old image or profiles meanwhile. But it repeats on every one of those commands
until the action has actually run to completion — a `jailbee apply` that
reported a failed restart or port forward has not, and will not clear it.

### Containers get no IPv4 address

A new container's `IPV4` column in `jailbee ls` / `incus list` stays empty, or
nothing inside it can reach the network.

**Cause:** a host firewall is blocking DHCP/DNS or forwarding on the JailBee
bridges. See [Host networking](installation.md#host-networking-only-if-you-use-a-firewall)
— add the firewalld zone entries or the UFW `route` + `before.rules` lines.
`jailbee doctor` only checks that the `jailbee-loose` bridge *exists*; it does not
test DHCP reachability, so a present-but-blocked bridge won't be flagged.

### "disk quota exceeded" when starting a container or Docker

runc fails with `unable to join session keyring: ... disk quota exceeded`.

**Cause:** the host kernel-keyring quota, not disk space — it runs out after
a handful of concurrent containers. Raise it: see
[Kernel keyring limits](installation.md#kernel-keyring-limits-running-many-containers-in-parallel).

### "newuidmap: uid range ... not allowed" when a container starts

**Cause:** the second `/etc/subuid` / `/etc/subgid` delegation line is
missing (or `incus` wasn't restarted after adding it), so `raw.idmap` can't
be installed. Re-run step 2 of the install and restart Incus: see
[Why the UID mapping is needed](installation.md#why-the-uid-mapping-is-needed).

### The IDE won't launch (`jailbee ide`)

- `jailbee ide` exits 2 with a message → `jetbrains.enabled` is `false`. Turn it
  on in `~/.config/jailbee/global.yaml` (see [`config.md`](config.md)).
- Nothing appears on screen → there's no graphical session for the
  passthrough to target (autostart's IDE launch is a no-op without one), or
  the JetBrains Toolbox path doesn't match `jetbrains.toolbox_host_path`.
- "Only one IDEA at a time" → the JetBrains profile is shared across
  containers, so a second IDEA won't open while one is running. Chrome runs
  per-container (`jailbee chrome`); inspect its profile pool with
  `jailbee pool ls chrome-profile` (`jailbee chrome-pool ls` still works too).

### Gradle (or Maven) builds hang on "Waiting to acquire ... lock"

**Cause:** two containers of the same repo built against the same
`~/.gradle` (or `~/.m2`) at once, and Gradle/Maven's own inter-process file
lock on the cache directory made the second build wait — or, past its
timeout, fail. This is what cache pooling exists to prevent: `gradle` and
`m2` are pooled by default (`pooled_caches`), which gives each container
its own private slot instead of one shared mount. If it's still happening,
`jailbee pool ls gradle` (or `m2`) tells you whether the cache is actually
pooled in this repo — it errors "No pooled cache named ..." if it isn't,
which means either a `pooled_caches: {gradle: false}` (or `m2: false`)
override, or a repo that predates this feature and hasn't run
`jailbee apply` since upgrading (a container already running when the pool
is created keeps its old shared mount until it next restarts — restart it
too). Run `jailbee apply`, then re-check `jailbee pool ls gradle` /
`jailbee pool ls m2` for a slot per running container.

See [`pooled_caches`](config.md#pooled_caches).

### `git push` / `gh` fails inside a container

**Cause:** by design, `github.com` is not in the default `strict` egress
allowlist, so day-to-day work runs offline-of-GitHub. Either bring the
commits to the host and push from there (`jailbee git checkout <name>` →
`git push`), or switch the container to loose for the write:
`jailbee net loose <name>`, push, then `jailbee net strict <name>`. See
[Security and limitations](security.md).

### GPU / NVIDIA passthrough

Not configured by `jailbee init`. NVIDIA passthrough needs extra Incus setup on
the host (drivers + `nvidia.runtime` / device wiring) that JailBee does not
manage — configure it directly on the Incus profile/instance.

## Removing JailBee

There is no `jailbee uninstall` command; teardown is manual. Some resources are
**per-repo**, others are **host-wide and shared** — remove them in that
order so you don't break other repos.

### Per-repo resources

Run from the repo. `<prefix>` is the repo's `container_prefix` (defaults to
the repo directory name; `incus profile list` shows the jailbee-owned ones):

```bash
jailbee destroy --all --force              # remove this repo's containers
jailbee net unregister                     # drop this repo from the egress-refresh timer

for p in base binds net-strict net-loose; do
    incus profile delete "<prefix>-$p"
done
incus network acl delete "<prefix>-allowlist"
incus image delete "<prefix>-base"     # the golden image (by alias)
rm -rf ~/.local/share/jailbee/shared/<prefix>
```

### Host-wide resources (only after the last JailBee repo is gone)

```bash
jailbee registry down                      # stop the shared Docker registry mirror
incus network delete jailbee-loose         # shared bridge — only if no jailbee repos remain
uv tool uninstall jailbee
```

The host tweaks from installation are harmless to leave in place; remove
them too if you want a clean slate:

```bash
sudo rm -f /etc/sysctl.d/99-jailbee-keys.conf   # the keyring-limit override
# the extra root: lines in /etc/subuid and /etc/subgid only grant
# delegation of your own UID, so they are safe to keep.
```
