# Troubleshooting

## Start with `jailbee doctor`

Run `jailbee doctor` from your repo on the host. It checks selected host and
configuration conditions (UID delegation, bridge reachability, keyring quota,
Incus reachability, GitHub token format, etc.) with remediation hints. Some
checks are skipped or inconclusive when evidence is unavailable; a passing
check is not a complete health guarantee. Start with its reported failures
and use the sections below to investigate further.

## Common problems

### "Run `jb base build` in this repo (or `jb upgrade` for every repo) to pick these up"

`jailbee ls`, `jailbee new` and `jailbee shell` print a short block on stderr,
and `jailbee doctor` reports the same thing as its `upgrade actions` check,
when the release notes call for a newer golden image or updated host
configuration than this repo's recorded build/apply version. This is based
on recorded actions, not an inspection of every installed file.
Run the named command in that repo, or use `jailbee upgrade` to run the
pending actions across registered repos (`--dry-run` previews them).
`upgrade` does not restart existing containers, their Docker daemons or
LiteLLM; restart-dependent changes remain pending. Rebuilding the golden
image affects future containers, not software already installed in existing
ones.

The hint does not block commands, but leaving it unresolved can leave fixes
or features unavailable. It repeats until the action is recorded or the hint
is dismissed. An `apply` that completes its configuration-writing steps
**does clear the upgrade hint even if a later container restart or port
forward fails**. Check the command's errors and `jailbee doctor` rather than
using disappearance of the hint as proof that everything is healthy.

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
(`legacy-config-dir`), a legacy `chrome:` block (`legacy-chrome-block`), a
`claude_credentials:` block (`legacy-credentials-block`), per-repo entries
still kept in `global.yaml` (`legacy-per-repo-map`) and the old
`agents.claude.ai_pr_*` keys (`legacy-pr-keys`), and the old remote SSH
`shell` spelling of `console` (`legacy-remote-ssh-shell`).
Deprecation dismissals are keyed by notice and config-file path, not file
contents: editing the same file does not reset them. Fixing the deprecated
setting makes the notice inapplicable; use `jailbee dismiss --clear <key>`
to re-enable a dismissed notice. `jailbee doctor` lists applicable dismissed
notices under its `dismissed notices` check. Warnings that answer the command you just typed — what `jailbee config
validate` reports, `jailbee base build`'s `golden.python` line, a deprecated
command alias — are deliberately not dismissible.

### Containers get no IPv4 address

A new container's `IPV4` column in `jailbee ls` / `incus list` stays empty, or
nothing inside it can reach the network.

A host firewall blocking DHCP/DNS or forwarding is a common cause, not a
confirmed diagnosis. A newly started container may also still be waiting for
a lease. See [Host networking](installation.md#host-networking-only-if-you-use-a-firewall)
for the firewalld or UFW rules and their exposure implications.

Run `jailbee doctor` with the container still running. The `network <bridge>
reachability` check looks for an IPv4 address, attempts TCP to the bridge
gateway on port 53 and, when a suitable applied-ACL destination exists,
probes one external IPv4/port pair. It does **not** test the DHCP exchange,
UDP DNS or actual name resolution. A timeout can also mean the destination
is unreachable; a skipped or "not verified" probe is inconclusive.

| Symptom | Possible firewall opening to check (UFW) |
| --- | --- |
| no IPv4, but IPv6 works | UDP port 67 (DHCP) in `before.rules` |
| IPv4 works, name lookups fail | UDP and TCP port 53 (DNS) in `before.rules` |
| IPv4 and DNS work, external connections fail | `ufw route allow in on <bridge>` |

These symptoms guide investigation; they do not uniquely identify a missing
rule. Check Incus networking and the destination service too.

With no container running on a bridge there is no symptom to read, and the
check stays silent — it never launches one of its own to find out. The
`jailbee-work` check (after `jailbee net migrate`) says "not verified" in that
case instead.

### "disk quota exceeded" when starting a container or Docker

runc fails with `unable to join session keyring: ... disk quota exceeded`.

For this specific session-keyring error, investigate host kernel-keyring
limits rather than filesystem capacity. The threshold depends on quota and
usage, not a fixed container count. Doctor checks `maxkeys` and mapped-root
UID `1000000` usage when the relevant proc files are available; this is not
a complete check of every keyring limit. See
[Kernel keyring limits](installation.md#kernel-keyring-limits-running-many-containers-in-parallel).
A generic "disk quota exceeded" error elsewhere may still concern storage.

### "newuidmap: uid range ... not allowed" when a container starts

Check root's delegations in `/etc/subuid` and `/etc/subgid`: they must cover
the configured host UID/GID as well as a sufficiently large shifted range
(at least 65,536 IDs). A separate single-ID line is one way to delegate the
host ID; an existing range that covers it also works.

`jailbee doctor`'s `uid delegation` check inspects these files, not whether
the running Incus daemon has picked up changes. Inspect a failed start with
`incus info --show-log <full-container-name>` and follow the installation
instructions, including restarting Incus after changing delegation: see
[Why the UID mapping is needed](installation.md#why-the-uid-mapping-is-needed).

### A GUI app (IDE, browser, `apps:` entry) won't launch

- **Integration disabled:** if the message explicitly says so, enable the
  matching switch (`jetbrains.enabled`, `browsers.chrome.enabled` or
  `browsers.firefox.enabled`) in `~/.config/jailbee/global.yaml` (see
  [`config.md`](config.md)). Exit status 2 alone does not identify the
  cause; an enabled IDE can still have no matching Toolbox launcher.
- **No window:** read the CLI error first. Display preparation or IDE
  launcher resolution can fail before a process starts; check
  `jetbrains.toolbox_host_path` for Toolbox. Autostart warns and skips GUI
  launches when no host graphical session is available. Detached registry
  apps write stdout/stderr to `/tmp/jailbee-app-<name>.log` inside the
  container; each launch replaces that log. `jailbee exec -d` uses
  `/tmp/jailbee-exec-*.log`. A log from an earlier launch is not evidence
  that the latest attempt started.
- **`jailbee apps ls <container>` reports `missing`:** the configured
  command was not found or executable for the container user, or no IDE
  launcher was resolved. Check paths, host mounts and permissions. For an
  image-sourced browser, run `jailbee base build` and create a new container
  from it: rebuilding does not install software into existing containers.
  For host-sourced apps, correct the host path, run `jailbee apply` and
  accept or perform any required restart. Install custom `apps:` commands
  separately; declaring them does not install them.
- **No sound, notifications or file dialog:** check whether the optional
  host socket passthrough is enabled. `gui.audio: true` / `gui.dbus: true`
  opt into PulseAudio / session D-Bus sharing (see [`gui`](config.md#gui));
  sockets attach on the container's next start when available. These
  settings do not diagnose every application-side problem.
- **SSH launch refused:** GUI launches through JailBee's SSH server need
  `remote.ssh.rdp: true` in an ordinary SSH session, which prepares the shared
  RDP display and waits for an RDP client. A `waypipe ssh` session uses
  waypipe instead, which is on by default (`remote.ssh.waypipe`) and does not
  need that display. See [Remote GUI over SSH](remote-gui.md).
- **Concurrent IDE instances conflict:** JetBrains configuration and data
  directories are shared by default between containers of the same repo,
  so concurrent IDEs may contend on shared state. Chrome and Firefox use
  separate per-container profile slots when launched through JailBee.
  Inspect them with `jailbee pool ls chrome-profile` /
  `jailbee pool ls firefox-profile`.
- **Firefox reports it is already running:** the profile pool excludes
  stale `.parentlock` and `lock` files when seeding and removes them when
  releasing a slot. If the error persists, inspect Firefox's log, running
  processes and the profile actually in use. `jailbee pool ls firefox-profile`
  shows assignments, not a lock diagnosis. Do not remove locks while Firefox
  is running. Disabling this pool with `pooled_caches: {firefox-profile: false}`
  is rejected when loading config.

### Gradle (or Maven) builds hang on "Waiting to acquire ... lock"

Concurrent builds sharing `~/.gradle` or `~/.m2` can contend on cache locks,
but a lock-wait message alone does not establish that another container is
the cause. Gradle and Maven caches are pooled by default **when configured**,
either explicitly through `shared_caches` or through `golden.stacks.java`.
Each container gets a separate slot for mutable cache state and locks;
immutable artifacts may be hardlinked, so this is not a security boundary.

`jailbee pool ls gradle` / `jailbee pool ls m2` shows pool assignments.
"No pooled cache named ..." can mean the cache is absent or pooling is
disabled, not just a `pooled_caches: {gradle: false}` override. Inspect the
effective configuration. After enabling pooling, run `jailbee apply` and
restart containers that were already running: these slots attach at startup.
Re-check the assignments afterward. Pooling prevents sharing those lock
files between containers; it cannot rule out contention between processes
within one container or every build-tool lock problem.

See [`pooled_caches`](config.md#pooled_caches).

### `git push` / `gh` fails inside a container

Check network access and authentication separately. Strict mode allows
configured destinations, not read-only operations: Claude plugin support
adds `github.com:443`, and `github.enabled` adds `api.github.com:443`.
HTTPS can carry writes as well as reads. JailBee passes the configured PAT
to `gh` without verifying its permissions; use a token with only the read
permissions the container needs. This token injection does not itself
configure Git authentication.

Prefer publishing on the host: run `jailbee pr <name>`, or bring the commits
into a host branch with `jailbee git checkout <name>` and then use the host's
`git push`. Switching a container to loose only widens network access; it
does not grant write permission or repair missing credentials.

If `gh` reads fail, check `github.enabled`, the effective `github.token`,
token expiry/repository access and the allowed destinations. Doctor checks
token presence, file permissions and format when the integration is enabled,
not effective GitHub permissions or API access. See
[Git remote & push](security.md#git-remote--push).

### GPU / NVIDIA passthrough

The generated base profile passes through existing `/dev/dri/renderD*`
nodes. JailBee does not automatically configure NVIDIA drivers,
`nvidia.runtime` or `/dev/nvidia*` devices. Supported explicit device
mappings can be declared through [`host_devices`](config.md#host_devices);
additional NVIDIA setup needs separately managed Incus configuration.
Do not rely on manual edits to JailBee-generated profiles: `jailbee apply`
overwrites them.

### Autostart stages never finished

A `jailbee new`/`start`/`restart` returned control (or the job seems to sit
forever), but you're not sure the container's autostart actually finished —
or it clearly didn't.

1. **`jailbee ls`'s JOB column.** `autostart:<stage>` identifies a live
   detached supervisor's current stage. If the worker disappears without
   recording a terminal state, it reads `<stage> (dead)`. A recorded failure
   reads `failed` instead; a successful run removes its job record.
2. **`jailbee autostart status [<name>]`.** Shows recorded steps grouped by
   stage, not every configured step. An unfinished entry reads `running`
   while the worker is live and `interrupted` once the run is over. These
   labels come from the progress log and worker liveness, not a probe of
   the command itself: a surviving command may still be running in tmux.
   This command covers detached autostart jobs, not a create/boot job that
   has not reached the detach boundary.
3. **`jailbee job log [<name>] [--follow]`.** Shows the worker's output —
   there is no separate `jailbee autostart log`. Use `jailbee tmux <name>`
   to inspect individual step windows.
4. **`jailbee job clear <name>`.** Drops a failed or stale job record,
   without stopping processes or repairing the container's network, mounts
   or partially completed work. Inspect those before retrying.

`jailbee autostart cancel [<name>]` sends SIGTERM to a live detached worker;
it returns after sending the signal, not after verifying cleanup. The worker
attempts to interrupt in-flight steps, detach the stage's temporary mounts
and restore the previous network mode before recording failure. Interruption
is best-effort: a command that ignores C-c can keep running. Network restore
is skipped if the mode no longer matches the one the stage set, and cleanup
errors are logged. Check status, logs and remaining processes afterward.
Cancellation does not roll back files changed by completed or interrupted
steps, or erase secrets copied from temporary mounts. Once a run is failed
or its worker is gone, use `jailbee job clear`, not `cancel`. See
[Detaching a run](config.md#detaching-a-run) and
[Security](security.md#autostart-and-the-network-exposure-window).

## Removing JailBee

There is no `jailbee uninstall` command; teardown is manual. Some resources are
**per-repo**, others are **host-wide and shared** — remove them in that
order so you don't break other repos.

### Per-repo resources

Run JailBee commands from the repo on the host. Before deleting anything,
preserve uncommitted/unpublished work, issue/PR outboxes, and any shared
settings, profiles or credentials you need. Container snapshots are not a
backup of bind-mounted host data.

Inspect `jailbee config show` for the effective `container_prefix`,
`golden.alias` and `shared_dir`; `jailbee config show --layer local` prints
the host-local config path. Do not assume the repo directory name or default
paths, especially with custom config, scratch repos or XDG overrides.

```bash
jailbee destroy --all --no-background  # retain confirmation and work-loss checks
jailbee ls                           # verify destruction finished successfully
```

Stop here if any container remains or destruction failed. Do not substitute
`--force` unless you intend to bypass the confirmation and work-loss checks.
Once destruction succeeds, remove its registration from the shared registry
used by refresh and repo discovery (this does not delete resources):

```bash
jailbee net unregister
incus profile list
incus image list
```

Delete only this repo's unused profiles that actually exist:
`<prefix>-base`, `<prefix>-binds`, `<prefix>-net-strict`,
`<prefix>-net-loose` and, after work-network migration,
`<prefix>-net-work`, `<prefix>-net-work-strict`, `<prefix>-net-work-loose`.
For each, use `incus profile delete "<actual-profile-name>"`.
On a btrfs pool the golden is an instance, so use `incus delete "<actual-golden-alias>--golden"` instead.
Delete the image with `incus image delete "<actual-golden-alias>"` only if
no remaining repo needs it; a custom or scratch image alias can be shared.
Deleting by alias deletes the image, not just that alias.

After backing up and verifying that no other repo, profile or container uses
them, remove the repo's actual `shared_dir` and host-local config file.
Typical defaults are `~/.local/share/jailbee/shared/<prefix>` and
`~/.config/jailbee/repos/<prefix>.yaml`, but these are not universal. Shared
state can contain logins and IDE/browser settings, not just disposable caches.

Then remove the repo's ACLs. Incus refuses to delete a referenced ACL, so
inspect actual names and references first:

```bash
incus network acl list
incus network get incusbr0 security.acls       # use the configured strict bridge
incus network get jailbee-work security.acls   # only if the work bridge exists
```

For each relevant bridge, use `incus network set <bridge> security.acls
"<remaining-comma-separated-list>"` to remove **only** this repo's entries,
keeping other repos and shared baseline/service ACLs. Use an empty string
only if there truly are no remaining entries.

The repo ACLs normally include `<prefix>-allowlist` and
`<prefix>-container-extras`; work-network migration adds
`<prefix>-work-loose` and `<prefix>-work-container-extras`. Extras ACL names
can be truncated and include a digest, so do not identify ownership solely
by a full-prefix match. Delete each verified, unused ACL with
`incus network acl delete "<actual-acl-name>"`. Successful destruction
normally removes per-container extras ACLs; inspect any cleanup warnings
for leftovers.

### Host-wide resources (only after the last JailBee repo is gone)

Close all dashboards and allow the shared state service to exit (normally
30 seconds without clients). Stop the SSH service and refresh jobs before
removing shared resources. Run only the commands for services present on
this host, and investigate failures before proceeding:

```bash
jailbee remote ssh disable
systemctl --user disable --now jailbee-net-refresh.timer
systemctl --user stop jailbee-net-refresh.service
jailbee display down
jailbee registry down
jailbee egress proxy down
```

`display down`, `registry down` and `egress proxy down` stop their service
containers; they do not delete them. After checking that they are stopped
and no longer needed, delete the containers before their profiles:

```bash
incus delete jailbee-display
incus delete jailbee-registry-mirror
incus delete jailbee-egress-proxy
incus profile delete jailbee-display-profile
incus profile delete jailbee-registry-mirror-profile
incus profile delete jailbee-egress-proxy-profile
```

For LiteLLM, **`--purge` deletes its persistent state volume, including
logins**, as well as the proxy container. Back up anything needed first:

```bash
jailbee litellm down --purge
incus profile delete jailbee-litellm-profile
incus network acl delete jailbee-litellm-egress
```

Inspect the bridges' `security.acls` lists as above and remove
`jailbee-services`, preserving unrelated entries, before deleting that ACL.
Remove the shared bridges only after **all** referencing containers and
profiles are gone and no other workload needs them:

```bash
incus network acl delete jailbee-services
incus network delete jailbee-work              # if present and unused
incus network acl delete jailbee-work-baseline
incus network delete jailbee-loose             # if present and unused
incus network acl delete jailbee-loose-baseline
```

Leave the pre-existing strict bridge (usually `incusbr0`) and storage pools
in place unless you separately establish that nothing else uses them.

For a clean slate, remove the disabled user-unit files that JailBee installed
under `~/.config/systemd/user/`: `jailbee-ssh.service`,
`jailbee-net-refresh.service` and `jailbee-net-refresh.timer`, then run
`systemctl --user daemon-reload`. SSH disable preserves its keys. Registry
removal also preserves the configured `docker_registry_mirror.data_dir`,
including cache and CA material.

Review JailBee's config/data/state directories (respecting XDG overrides)
and custom paths before deleting them. They can contain host-wide account
holders and parked logins, SSH keys, logs and the shared database; removing
one repo's `shared_dir` does not remove all of these. Keep the global config
until the service commands above have finished. Finally uninstall using the
tool that installed JailBee, for example `uv tool uninstall jailbee` or
`pipx uninstall jailbee`.

Installation-time host changes are not automatically undone. Review firewall
rules, `/etc/sysctl.d/99-jailbee-keys.conf`, and `/etc/subuid` / `/etc/subgid`
separately. Root's delegations can include both a single host UID/GID and a
larger subordinate-ID range; do not remove them while other Incus containers
need them. Deleting a sysctl file alone does not reset the running kernel's
values. Do not remove unrelated firewall rules or Incus settings as part of
JailBee cleanup.
