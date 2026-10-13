# Installation

## Prerequisites

- Linux host (Ubuntu 26.04+ recommended; Wayland session for GUI passthrough)
- Incus **6.0.5-8 or newer** — ships in Ubuntu 26.04's `universe` repository.
  The [package changelog](https://changelogs.ubuntu.com/changelogs/pool/universe/i/incus/incus_6.0.5-8/changelog)
  records the nested-Docker fix in `6.0.5-6`, included in this build. Use
  `security.nesting=true` rather than disabling AppArmor as a workaround. The 6.0 LTS
  series and the current 7.x feature releases both work: `jailbee` detects the
  client version and adapts where the CLI changed between them (`incus profile
  assign` switched from a comma-joined profile list to separate arguments in
  7.3). If the version cannot be read, `jailbee` assumes the 6.0 LTS syntax.
  `jailbee doctor` reports the version it detected.
- Python **3.12 or newer**. `uv` can provision a compatible interpreter;
  `pipx` and manually managed virtualenvs need one available.
- A way to install a Python CLI application — [`uv`](https://docs.astral.sh/uv/)
  or [`pipx`](https://pipx.pypa.io/). JailBee itself needs neither at runtime;
  pick whichever you already have:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh   # uv
  sudo apt install pipx                             # or pipx
  ```
- Optional GUI integrations: a host browser installation for host-sourced
  browsers (Chrome defaults to `/opt/google/chrome`), and JetBrains Toolbox
  for JetBrains IDEs (default `~/.local/share/JetBrains/Toolbox`). These paths
  are configurable; image-sourced browsers need no host browser installation.
- Your project's repo cloned somewhere (e.g. `~/dev/SampleApp`)

## Quick install

On a fresh Ubuntu 26.04 host with Incus's default networking, these five
steps are all you need — they are per-host and one-time. Per-repo setup
(building the image, creating containers) then continues in
[Getting started](getting-started.md).

> On a default host this is the whole install. If your host runs a firewall
> or you plan to run many containers at once, the **conditional** sections
> further down apply — and `jailbee doctor` (which Getting started runs first)
> tells you which.

### 1. Install Incus and initialise it

```bash
sudo apt install incus
sudo adduser $USER incus-admin
# log out and back in
incus admin init --auto
```

`--auto` is Incus's non-interactive mode. On a typical fresh host it creates
a local storage pool and the `incusbr0` bridge JailBee expects by default.
The choices depend on the host filesystem and existing networks: the
storage backend can be `btrfs` on a btrfs host, and an existing `incusbr0`
can cause Incus to choose another bridge name. Inspect an existing Incus
setup instead of re-running initialization blindly. Drop the flag if you
want to pick the storage backend (`btrfs`, `lvm`, `zfs`) or subnet yourself.
A `dir` pool stores a full copy per container and per snapshot; for more
than a few containers, [use a btrfs pool](storage.md).

`security.nesting=true` (needed for nested Docker and for systemd services
that use user namespaces) is set automatically by `jailbee` on every container.

### 2. Delegate one UID/GID for host-file access

`jailbee` maps the configured development UID/GID to your real host identity
via `raw.idmap`, so configured host-file mounts and enabled agent sockets
work without changing ownership. Development containers otherwise run
unprivileged; the files and sockets shared depend on your configuration.
For the default mapping, root needs delegated ranges in `/etc/subuid` and
`/etc/subgid` covering both Incus's shifted IDs and your host UID/GID.

The commands below add the conventional shifted range and a single-ID
delegation for your identity. Existing wider root delegations may already
cover these IDs; check them first rather than adding overlapping ranges.
On a standard fresh setup, these commands add missing exact lines without
replacing existing entries (they read your `id -u` / `id -g`):

```bash
grep -qxF "root:1000000:1000000000" /etc/subuid \
    || echo "root:1000000:1000000000" | sudo tee -a /etc/subuid
grep -qxF "root:$(id -u):1" /etc/subuid \
    || echo "root:$(id -u):1" | sudo tee -a /etc/subuid

grep -qxF "root:1000000:1000000000" /etc/subgid \
    || echo "root:1000000:1000000000" | sudo tee -a /etc/subgid
grep -qxF "root:$(id -g):1" /etc/subgid \
    || echo "root:$(id -g):1" | sudo tee -a /etc/subgid

sudo systemctl restart incus
```

An Incus installation may already have the shifted range delegated, but
that alone does not cover your host identity. Check both files. See
[Why the UID mapping is needed](#why-the-uid-mapping-is-needed) below for
the rationale, the security impact, and a verification recipe.

### 3. Install JailBee

```bash
uv tool install jailbee     # or: pipx install jailbee
```

This installs the `jailbee` and `jb` commands. For the optional Qt dashboard
(`jailbee gui`, and `jailbee dashboard --gui`), add the `gui` extra instead:

```bash
uv tool install 'jailbee[gui]'      # or: pipx install 'jailbee[gui]'
```

It is the one install step `jailbee setup` cannot do for you: the extra goes
into jailbee's own environment, so installing it means reinstalling the tool
that is running — and only you know whether uv, pipx or a virtualenv put it
there. `jailbee doctor` reports whether it is present, and `jailbee gui`
prints the command when it is not. To add extras to an existing install,
rerun `uv tool install` with the extras, or use `pipx install --force`.
Include every extra you want to retain, for example:

```bash
uv tool install 'jailbee[gui,ssh]'   # or: pipx install --force 'jailbee[gui,ssh]'
```

The authenticated SSH service is a separate optional extra and is never
enabled by `jailbee setup`:

```bash
uv tool install 'jailbee[ssh]'      # or: pipx install 'jailbee[ssh]'
```

Extras can be combined as `jailbee[gui,ssh]`. Installing the dependency does
not install or start a listener; [Optional SSH service](#optional-ssh-service)
below is a second, explicit opt-in.

For routine version upgrades, use `uv tool upgrade jailbee` or
`pipx upgrade jailbee`, then rerun `jailbee setup`.

To install unreleased development code, use an explicit branch rather than
relying on the repository's default branch:

```bash
uv tool install 'git+https://github.com/VRTFinland/jailbee@dev'
# or: pipx install --force 'git+https://github.com/VRTFinland/jailbee@dev'
```

JailBee is an ordinary PyPI package and needs neither uv nor pipx at
runtime — both are here only because Ubuntu 24.04+ marks its system Python
as externally managed (PEP 668), so a bare `pip install jailbee` is refused.
A virtualenv you manage yourself works exactly as well:

```bash
python3 -m venv ~/.venvs/jailbee
~/.venvs/jailbee/bin/pip install jailbee
ln -s ~/.venvs/jailbee/bin/jailbee ~/.local/bin/jailbee   # and jb, if you want it
```

Reaching for `pip install --break-system-packages` instead is not
recommended: it installs into the Python that `apt` owns.

### 4. Set up your shell and the refresh timer

```bash
jailbee setup
```

Installing the package puts `jailbee` and `jb` on your `PATH` and nothing
else. Three things live outside the package, and `jailbee setup` is what
installs them — interactively, one question per step:

| Step | What it is | Without it |
| --- | --- | --- |
| `completions` | Completion scripts for **both** `jailbee` and `jb`, for your shell | No TAB completion of commands, container names or branches |
| `timer` | The `jailbee-net-refresh` **user systemd timer** | Strict-mode allowlists go stale as the IPs behind GitHub et al. change, and `jailbee net loose --for 2h` never reverts |
| `skills` | JailBee's agent skills in each skill-capable agent's own directory (`~/.claude/skills`, `~/.codex/skills`, …) for the agents found on your host. **Opt-in** — set `install_host_skills: true` in `~/.config/jailbee/global.yaml` first | Agents you run on the host itself don't know how to drive `jailbee` (the *containers*' skills are installed without this step) |

Every step is idempotent, so re-run `jailbee setup` after upgrading JailBee.
`--yes` runs selected, applicable steps without asking; it still respects
the host-skills opt-in. `--only <step>` picks one step, and
`--shell bash|zsh|fish` overrides shell detection (repeatable). zsh needs a
line in `~/.zshrc`; interactive setup offers to add it. With `--yes`, or if
you decline the edit, setup prints the line for you to add yourself.

The timer is a *user* timer, so it only runs while you have a login session
unless linger is enabled — `jailbee setup` prints the one-liner
(`sudo loginctl enable-linger $USER`) when it is not.

`jailbee setup --status` reports the three without installing anything, and
`jailbee doctor` reports them afterwards — plus the optional `gui` extra from
step 3. Before setup has been run or its first-run hint shown, interactive
`jailbee ls` and `jailbee dashboard` offer setup once if steps are missing;
non-interactive commands print a hint instead. Running setup silences future
offers even if you decline steps, so use `--status` or `doctor` to check
what remains.

### 5. You're done with host setup

Confirm the CLI is on your `PATH`:

```bash
jailbee version
```

Everything else — creating the golden image and your first container — is
per repo. Continue in [Getting started](getting-started.md). The first thing
it has you run, `jailbee doctor`, flags any remaining host issues, i.e. the
conditional sections below.

## Optional SSH service

The SSH server is opt-in host tooling for reaching JailBee itself. It is not a
host shell, and the default `127.0.0.1:8022` listener is reachable only from
this machine. Read [Remote SSH](security.md#remote-ssh) before changing the
listen address or granting a key.

Install the extra as shown above, add one client public key, then explicitly
enable the user service:

```bash
uv tool install 'jailbee[ssh]'   # retain any other extras, e.g. 'jailbee[gui,ssh]'
jb remote ssh key add
jb remote ssh enable
jb remote ssh status
```

With no argument, `key add` prompts and reads one pasted public-key line (for
example the contents of `~/.ssh/id_ed25519.pub`). A file path also works —
`jb remote ssh key add ~/.ssh/id_ed25519.pub` — and so does piping one in:
`cat ~/.ssh/id_ed25519.pub | jb remote ssh key add -`.

`key add` prints the full SHA256 fingerprint, algorithm and comment. Use `jb
remote ssh key ls` to copy that fingerprint later and `jb remote ssh key rm
SHA256:...` to revoke it. Keys are read for every new connection, so key
changes need no restart. An established connection remains authenticated;
close it when revoking access.

By default, client authorization is stored in
`~/.config/jailbee/ssh/authorized_keys`; the persistent server identity is
`~/.local/share/jailbee/ssh/host_key`. The former honors `XDG_CONFIG_HOME`,
the latter `XDG_DATA_HOME`. Their directories are mode 0700 and
the files mode 0600. Enabling or serving creates missing files but never
replaces an existing host key. `key add` accepts one plain OpenSSH public-key
line; key options and certificates are not supported.

`enable` creates private key directories, preserves any existing Ed25519 host
key, installs `~/.config/systemd/user/jailbee-ssh.service`, and runs
`systemctl --user enable --now jailbee-ssh.service`. The default configuration
enables the dashboard, interactive console and one-shot JailBee commands,
with `commands.mode: full` and host restrictions enabled. This grants broad
access to JailBee-managed containers, including actions that modify them. Review
[`remote.ssh`](config.md#remotessh) before granting access; configure it in
`~/.config/jailbee/global.yaml` (or `$XDG_CONFIG_HOME/jailbee/global.yaml`).

Inspect the service and its bounded audit log with:

```bash
jb remote ssh status
systemctl --user status jailbee-ssh.service
journalctl --user -u jailbee-ssh.service
journalctl --user -u jailbee-ssh.service -f
```

The unit otherwise follows the login session. To keep it running while this
user is logged out, enable systemd linger explicitly (requires root):

```bash
sudo loginctl enable-linger "$USER"
```

After changing `listen` or `port`, run `jb remote ssh restart`; a running
listener cannot move itself.

After a version upgrade, the service restarts itself: it compares the
installed version with the one it started from before each new command or
shell session and every 30 seconds. On a difference it stops accepting
connections (a session that detects the change is told to reconnect), lets
live sessions finish for up to 10 minutes before hanging up the rest, and exits with
status 75, which the unit's `Restart=on-failure` turns into a restart on the
new version (background jobs survive, as with any restart). The journal records both versions. A
service started by a JailBee from before this check cannot notice an
upgrade, and keeps enforcing its old rules while every session runs the new
CLI — `jb ls`, `jb new`, `jb shell` and `jb remote ssh status` say so and
name `jb remote ssh restart`, which this one time is needed by hand. The
startup log includes a `host restrictions:` line in servers that support
restricted mode; it reports `on` or `OFF` according to the effective policy.
Entry-point and command-policy edits apply to new SSH sessions without a
restart; existing console and dashboard sessions retain their session
policy, so reconnect to apply changes. Foreground override flags continue
to take precedence over configuration reloads. If access fails, keep the listener on
loopback, run `jb remote ssh status`, validate `global.yaml`, and inspect the
journal. `jb remote ssh serve` starts the same listener in the foreground for
diagnostics, so first stop the unit or choose an unused port; on startup it
prints the listening address, the host key's SHA256 fingerprint and a connect
example. Running beside the service on another port also accepts one-off
overrides for experimentation, without touching `global.yaml`, e.g.
`jb remote ssh serve --port 18022 --console --commands allowlist --allow ls
--allow new` — see [`remote.ssh`](config.md#remotessh) for what each flag
overrides and its validation rules. `jb remote ssh disable` stops and
disables the unit but deliberately preserves configuration, authorized keys
and the host key for recovery or later re-enablement.

---

The rest of this page is **conditional**. Apply a section only if `jailbee
doctor` or the symptom it describes says so;
[Troubleshooting](troubleshooting.md) links here by symptom.

## Host networking (only if you use a firewall)

Incus's default networking needs no changes. But if your host runs a
firewall, allow the managed bridges used on this host:

- `incusbr0` — Incus's default. Used by the legacy `strict` profile.
- `jailbee-loose` — created by `jailbee init`. Used by the legacy `loose`
  profile so the per-repo allowlist ACL on `incusbr0` doesn't leak into
  "open egress" mode.
- `jailbee-work` — created only after the explicit `jb net migrate` opt-in;
  used by new work-generation containers in both modes.

### firewalld

Add the bridges you use to the trusted zone (`jailbee-work` only after opt-in):

```bash
sudo firewall-cmd --permanent --zone=trusted --add-interface=incusbr0
sudo firewall-cmd --permanent --zone=trusted --add-interface=jailbee-loose
sudo firewall-cmd --permanent --zone=trusted --add-interface=jailbee-work
sudo firewall-cmd --reload
```

The `trusted` zone accepts all connections from these interfaces, including
connections to host services; it is broader than the DHCP/DNS-only UFW input
rules below. Use a more restrictive zone or policy if containers must not
reach other host services. See the
[Incus firewall guide](https://linuxcontainers.org/incus/docs/main/howto/network_bridge_firewalld/).

### UFW

If your host runs `ufw` with the default `deny (incoming)` and `deny (routed)`
policies, containers won't get DHCP leases or reach the internet without
opening up the JailBee bridges. `jailbee doctor` diagnoses this per bridge
once at least one container is running on it — `network incusbr0
reachability` / `network jailbee-loose reachability` (and `network
jailbee-work reachability` after `jailbee net migrate`) report which of the
three openings below is missing, naming the rule to add. With nothing
running on a bridge there is no symptom to read, and the check stays
silent (the `jailbee-work` one says **not verified**) — so on a fresh host,
apply the rules below rather than waiting for `doctor` to ask for them.

For IPv4, each active bridge needs one `ufw route` rule plus three
`before.rules` lines, repeated per bridge. Only add rules for bridges you
use; omit `jailbee-work` unless you opt in. These input rules do not configure
IPv6; if you use IPv6 on a legacy bridge, also check its DHCPv6, DNS and
forwarding rules using the Incus firewall guide linked above. The work
bridge is IPv4-only.

**1. Allow forwarding from each active bridge** — lets containers reach the
internet via NAT. Reply traffic returns automatically through UFW's
`ESTABLISHED,RELATED` rule, so no symmetric "out" rule is needed.

```bash
sudo ufw route allow in on incusbr0
sudo ufw route allow in on jailbee-loose
sudo ufw route allow in on jailbee-work
```

These are persistent (UFW saves them) and visible in `ufw status`.

**2. Edit `/etc/ufw/before.rules`** — allow DHCP and DNS requests to
Incus's `dnsmasq` on each managed bridge before UFW's later input rules
can drop them. These rules open only UDP 67 and UDP/TCP 53 on the named
interfaces, not every service on the host.

Add the following lines inside the `*filter` section of
`/etc/ufw/before.rules`, anywhere before the `COMMIT` line:

```
# allow Incus dnsmasq on incusbr0 (DHCP + DNS)
-A ufw-before-input -i incusbr0 -p udp --dport 67 -j ACCEPT
-A ufw-before-input -i incusbr0 -p udp --dport 53 -j ACCEPT
-A ufw-before-input -i incusbr0 -p tcp --dport 53 -j ACCEPT

# allow Incus dnsmasq on jailbee-loose (DHCP + DNS for unrestricted loose bridge)
-A ufw-before-input -i jailbee-loose -p udp --dport 67 -j ACCEPT
-A ufw-before-input -i jailbee-loose -p udp --dport 53 -j ACCEPT
-A ufw-before-input -i jailbee-loose -p tcp --dport 53 -j ACCEPT

# allow Incus dnsmasq on jailbee-work (DHCP + DNS for work-generation bridge)
-A ufw-before-input -i jailbee-work -p udp --dport 67 -j ACCEPT
-A ufw-before-input -i jailbee-work -p udp --dport 53 -j ACCEPT
-A ufw-before-input -i jailbee-work -p tcp --dport 53 -j ACCEPT
```

Reload UFW: `sudo ufw reload`.

**3. Verify** — for the legacy bridges, launch a temporary container on
each bridge you use and confirm it gets an IPv4 address. The examples wait
10 seconds; first boot can take longer. Use unused container names:

```bash
incus launch images:ubuntu/26.04 ufw-test \
    -c security.nesting=true --network=incusbr0
sleep 10
incus list ufw-test                       # IPv4 column should be populated
incus delete ufw-test --force

incus launch images:ubuntu/26.04 ufw-test-loose \
    -c security.nesting=true --network=jailbee-loose
sleep 10
incus list ufw-test-loose                 # IPv4 column should be populated
incus delete ufw-test-loose --force
```

An IPv4 address verifies DHCP, not DNS or internet access. Run `jailbee
doctor` while a JailBee container is running on each bridge to check those
stages too. If `IPV4` remains empty, inspect the firewall and bridge setup.
For `jailbee-work`, create a JailBee container after activation rather than
using a bare `incus launch`: work containers also need JailBee's per-NIC
network policy.

**Existing setups.** Add rules only for bridges your firewall does not already
cover. A legacy-only host does not need `jailbee-work`; after opting in, add
its route rule and three input rules, then `ufw reload` (or add it to
firewalld's trusted zone above). `jb doctor` probes a running work container
and reports the failed DHCP, DNS or allowed-egress stage with the relevant
bridge-specific rule. A present bridge with no running work container is
reported as **not verified**, not healthy.

### Optional work-network activation and rollback

Package upgrade does not move any container or change the default. Existing
containers continue on their legacy bridge and profile. To use the shared,
stable-address work network for future containers, first install the one-time
firewall rules above when applicable, then explicitly opt in:

```bash
jb net migrate
```

The command asks for confirmation, then prepares the `jailbee-work` bridge
and baseline ACL and changes the host-wide default for future containers.
It does not verify firewall reachability; confirm only after applying host
firewall setup, then check a running work container with `jb doctor`.
Non-interactive use requires `jb net migrate --yes`, which acknowledges
the unverified firewall setup rather than testing it. Existing legacy
containers are not moved. To undo activation:

```bash
jb net migrate --undo
```

Undo changes only the default for future containers. It does not move existing
work containers or remove their bridge/ACLs; those resources remain maintained
while any work-generation instance uses them. This migration is optional for
existing environments; `jb apply` maintains both generations and does not
silently migrate containers.

**Why this is the minimal set.** A naive setup might add e.g.
`ufw allow in on incusbr0` and `ufw route allow out on incusbr0` as
well, but both broaden the attack surface unnecessarily:

- `ufw allow in on <bridge>` would let containers reach **any** TCP/UDP
  port on the host (e.g. a host-running database, SSH, etc.). The
  before.rules entries above expose only the two services Incus's
  `dnsmasq` actually serves: DHCP and DNS.
- `ufw route allow out on <bridge>` would let the host's LAN initiate
  inbound connections to containers. We want strictly egress + replies,
  not ingress, so we omit it. UFW's built-in
  `ESTABLISHED,RELATED` ACCEPT in `ufw-before-forward` already handles
  reply packets from outbound connections.

The `before.rules` entries are one way to install the input allowances;
they are not the only way. The Incus firewall guide also documents ordinary
`ufw allow in on <bridge>` commands scoped to DHCP and DNS ports. If those
already work on your host, do not duplicate them. Moving a rule to an
earlier chain does not fix a rule that names the wrong bridge: after a
bridge rename, update the interface names in the firewall rules too.

## Kernel keyring limits (running many containers in parallel)

Running nested Docker workloads across several JailBee containers can
exhaust the host kernel's per-user keyring quota even with plenty of disk
space. The symptom is a misleading error from runc when starting a Docker
container:

```
OCI runtime create failed: runc create failed: unable to start container
process: error during container init: unable to join session keyring:
unable to create session key: disk quota exceeded
```

"Disk quota exceeded" here means *kernel keyring quota*, not filesystem
space. Nested Docker's runc creates session keyrings under the host UID
that Incus maps container root to (normally `1000000`). Those workloads
share the per-user quota; how soon `kernel.keys.maxkeys=200` runs out
depends on the workload. `jailbee doctor` warns when `maxkeys` is below
1000 or UID `1000000` is using at least 75% of its quota. The values below
set 2000 for headroom.

Raise the limits persistently:

```bash
sudo tee /etc/sysctl.d/99-jailbee-keys.conf >/dev/null <<'EOF'
kernel.keys.maxkeys=2000
kernel.keys.maxbytes=2000000
kernel.keys.root_maxkeys=2000
kernel.keys.root_maxbytes=2000000
EOF
sudo sysctl --system
cat /proc/sys/kernel/keys/maxkeys    # should print 2000
```

To inspect current usage at any time:

```bash
cat /proc/key-users                  # column 4 is qnkeys/maxkeys per uid
```

## Why the UID mapping is needed

Step 2 above delegates a shifted range and your host identity in
`/etc/subuid` and `/etc/subgid`. A conventional setup looks like this (with
`1000` replaced by your `id -u` / `id -g`; on most single-user systems they
are the same). Wider existing root delegations may already provide the
required coverage; these exact lines are not mandatory:

```
root:1000000:1000000000
root:1000:1
```

**Verify** — launch a fresh container with `raw.idmap` and confirm it
starts:

```bash
incus launch images:ubuntu/26.04 idmap-test \
    -c security.nesting=true \
    -c raw.idmap="uid $(id -u) $(id -u)
gid $(id -g) $(id -g)"
sleep 5
incus list idmap-test       # STATUS column should be RUNNING
incus delete idmap-test --force
```

If the launch fails with `newuidmap: uid range [<UID>-<UID+1>) ... not allowed`,
the delegation lines didn't take effect — check `/etc/subuid` / `/etc/subgid`
and confirm `incus` was restarted. `jailbee doctor`'s `uid delegation` check
checks ID coverage and shifted-range size in both files, and reports
missing coverage or unreadable files.

**Why this is needed and what it actually means.** Development containers
combine unprivileged root with deliberate identity sharing:

- **Unprivileged root** — the development container's UID 0 is mapped to a
  high host UID (e.g. `1000000`), not host root. This limits its privileges;
  it is not a guarantee against container escapes.
- **A shared development identity** — the development user is mapped to
  your real host UID/GID so configured mounts and enabled agent sockets
  work without rewriting ownership. Optional SSH integration uses a shared
  directory seeded with selected host configuration files, not a mount of
  your entire host `~/.ssh`.

The registry mirror separately maps its container root to your host
UID/GID for its cache mounts; the development-container root mapping
above does not describe that service container.

The mechanism is `raw.idmap`, which tells Incus: "shift everything to the
high range *except* UID `<UID>` — leave that one identity-mapped." The kernel
side of this is a multi-segment user namespace mapping. The userland side
is `newuidmap`, which refuses to install any segment that isn't authorised
in `/etc/subuid`. The `root:1000000:1000000000` line covers the shifted
range; the new `root:<UID>:1` line covers the carve-out. Both lines together
let `newuidmap` install the full mapping.

**Security impact: deliberate identity sharing.** The delegation does not
itself mount files, but it enables container processes to use your host
identity for resources shared with them. Shared writable files can be
modified and forwarded agent sockets can exercise the agent's authority.
Treat those mounts and sockets as part of the security boundary, and share
only what the workload needs. See [Security model](security.md).
