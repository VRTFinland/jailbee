# Troubleshooting

## Start with `jailbee doctor`

Run `jailbee doctor` from inside your repo. It checks the host and your config
and names most problems (uid delegation, bridge reachability, keyring quota,
Incus reachability, GitHub token shape, …) with a remediation hint. Fix what it reports first —
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

If you have read it and are not going to run the action yet, mark it read:

```bash
jailbee dismiss apply        # or: jailbee dismiss base-build
jailbee dismiss              # what applies here, and what you have dismissed
jailbee dismiss --clear apply
```

The hint then stops appearing on `jailbee ls` / `new` / `shell` until a later
release adds a **new** reason for that action — upgrading alone does not bring
it back. `jailbee doctor` is unaffected: it keeps reporting the action as a
failed `upgrade actions` check and adds the version you dismissed it at, so
nothing is hidden from the one place you would go looking.

The same command covers the deprecation notices about `.gie/config.yaml`
(`legacy-config-dir`) and a legacy `chrome:` block (`legacy-chrome-block`).
Those cannot grow a new reason on their own, so they stay dismissed until you
change the config; `jailbee doctor` lists them under its `dismissed notices`
check. Warnings that answer the command you just typed — what `jailbee config
validate` reports, `jailbee base build`'s `golden.python` line, a deprecated
command alias — are deliberately not dismissible.

### Containers get no IPv4 address

A new container's `IPV4` column in `jailbee ls` / `incus list` stays empty, or
nothing inside it can reach the network.

**Cause:** a host firewall is blocking DHCP/DNS or forwarding on the JailBee
bridges. See [Host networking](installation.md#host-networking-only-if-you-use-a-firewall)
— add the firewalld zone entries or the UFW `route` + `before.rules` lines.

Run `jailbee doctor` with the container still running: the `network <bridge>
reachability` check tests the three openings in order and names the missing
rule. The three symptoms are distinct, so the check can tell them apart:

| Symptom | Missing opening |
| --- | --- |
| no IPv4, but IPv6 works | `--dport 67` (DHCP) in `before.rules` |
| IPv4 fine, every name lookup hangs | `--dport 53` (DNS) in `before.rules` |
| IPv4 and DNS fine, nothing reaches out | `ufw route allow in on <bridge>` |

With no container running on a bridge there is no symptom to read, and the
check stays silent — it never launches one of its own to find out.

### "disk quota exceeded" when starting a container or Docker

runc fails with `unable to join session keyring: ... disk quota exceeded`.

**Cause:** the host kernel-keyring quota, not disk space — it runs out after
a handful of concurrent containers. Raise it: see
[Kernel keyring limits](installation.md#kernel-keyring-limits-running-many-containers-in-parallel).

### "newuidmap: uid range ... not allowed" when a container starts

**Cause:** the second `/etc/subuid` / `/etc/subgid` delegation line is
missing (or `incus` wasn't restarted after adding it), so `raw.idmap` can't
be installed. The container is created and stays `STOPPED`; the message
appears only in `incus info --show-log <name>`.

`jailbee doctor`'s `uid delegation` check names the missing line directly.
Re-run step 2 of the install and restart Incus: see
[Why the UID mapping is needed](installation.md#why-the-uid-mapping-is-needed).

### A GUI app (IDE, browser, `apps:` entry) won't launch

- `jailbee ide` / `jailbee chrome` / `jailbee firefox` exits 2 with a
  message → the matching master switch (`jetbrains.enabled`,
  `browsers.chrome.enabled`, `browsers.firefox.enabled`) is `false`. Turn it
  on in `~/.config/jailbee/global.yaml` (see [`config.md`](config.md)).
- Nothing appears on screen → there's no graphical session for the
  passthrough to target (autostart's launch is a no-op without one), or —
  for the IDE — the JetBrains Toolbox path doesn't match
  `jetbrains.toolbox_host_path`. Every app's stdout/stderr lands in
  `/tmp/jailbee-app-<name>.log` inside the container (`/tmp/jailbee-exec-*.log`
  for a `jailbee exec -d` command) — check it before assuming the launch
  itself failed.
- `jailbee apps ls <container>` reports a builtin or `apps:` entry as
  `missing` → the binary genuinely isn't in that image (common on one built
  before a browser was enabled or before `source` changed to `image`). Run
  `jailbee base build` (for `source: image`) or `jailbee apply` (for
  `source: host`) and check again.
- "Only one IDEA at a time" → the JetBrains profile is shared across
  containers, so a second IDEA won't open while one is running. Chrome and
  Firefox both run **per-container** instead (`jailbee chrome` /
  `jailbee firefox`); inspect their profile pools with `jailbee pool ls
  chrome-profile` / `jailbee pool ls firefox-profile` (`jailbee chrome-pool
  ls` still works too, as a deprecated alias for the Chrome one).
- "Firefox is already running, but is not responding" from inside the
  container → this is exactly what Firefox's own profile pool exists to
  prevent (a stale lock file from an unclean exit, seeded into a fresh
  container). If it still happens, check the pool with `jailbee pool ls
  firefox-profile` — a slot stuck mid-release, or a repo whose
  `pooled_caches` overrides `firefox-profile: false` (a `ConfigError` at
  load time, so this shouldn't reach a real config) points at the cause.

### Gradle (or Maven) builds hang on "Waiting to acquire ... lock"

**Cause:** two containers of the same repo built against the same
`~/.gradle` (or `~/.m2`) at once, and Gradle/Maven's own inter-process file
lock on the cache directory made the second build wait — or, past its
timeout, fail. This is what cache pooling exists to prevent: `gradle` and
`m2` are pooled by default (`pooled_caches`), which gives each container
its own private slot instead of one shared mount. If it's still happening,
`jailbee pool ls gradle` (or `m2`) tells you whether the cache is actually
pooled in this repo — it errors "No pooled cache named ..." if it isn't,
which means a `pooled_caches: {gradle: false}` (or `m2: false`) override.
A pooled cache attaches when a container next boots, so a container that
was already running when the pool was created needs a restart before it
uses its own slot. Run `jailbee apply`, restart the affected containers,
then re-check `jailbee pool ls gradle` / `jailbee pool ls m2` for a slot
per running container.

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

### Autostart stages never finished

A `jailbee new`/`start`/`restart` returned control (or the job seems to sit
forever), but you're not sure the container's autostart actually finished —
or it clearly didn't.

1. **`jailbee ls`'s JOB column.** `autostart:<stage>` means a detached
   supervisor is on that stage right now. Once the supervisor has died, the
   `autostart:` prefix drops and it reads just `<stage> (worker gone)` —
   the bare stage name it was on when it died, not `autostart:<stage>
   (worker gone)`.
2. **`jailbee autostart status <name>`.** One row per step, grouped by
   stage. A step shown as `running` under a live worker is genuinely in
   flight; the same state under a dead one is rendered `interrupted` — it
   was cut off and will never report a result, since nothing routes an
   aborted step through the normal finish path.
3. **`jailbee job log <name> [--follow]`.** The supervisor's own output —
   there is no separate `jailbee autostart log`.
4. **`(worker gone)`** always means the supervisor process is dead, however
   the run ended. `jailbee job clear <name>` acknowledges the record
   without touching the container, which is left exactly as the run left
   it (network mode, mounts, whatever steps did finish).

`jailbee autostart cancel <name>` stops a run that's still alive rather than
waiting it out: SIGTERM unwinds the stage in flight (interrupts the running
step, detaches the stage's mounts, restores the network) before marking the
job failed. It refuses once the worker is already gone — `jailbee job
clear` is the tool for that case, not `cancel`. See
[Detaching a run](config.md#detaching-a-run) and
[Security](security.md#autostart-and-the-network-exposure-window).

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
