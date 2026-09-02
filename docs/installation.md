# Installation

## Prerequisites

- Linux host (Ubuntu 26.04+ recommended; Wayland session for GUI passthrough)
- Incus **6.0.5-8 or newer** — ships in Ubuntu 26.04's `universe` repository.
  That build carries the nested-Docker AppArmor fix (CVE-2025-52881 fallout,
  backported to the 6.0 LTS series), so nested Docker works out of the box
  with `security.nesting=true` — no AppArmor workaround required. The 6.0 LTS
  series and the current 7.x feature releases both work: `jailbee` detects the
  client version and adapts where the CLI changed between them (`incus profile
  assign` switched from a comma-joined profile list to separate arguments in
  7.3). If the version cannot be read, `jailbee` assumes the 6.0 LTS syntax.
  `jailbee doctor` reports the version it detected.
- A way to install a Python CLI application — [`uv`](https://docs.astral.sh/uv/)
  or [`pipx`](https://pipx.pypa.io/). JailBee itself needs neither at runtime;
  pick whichever you already have:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh   # uv
  sudo apt install pipx                             # or pipx
  ```
- Host-installed Chrome at `/opt/google/chrome` and JetBrains Toolbox at
  `~/.local/share/JetBrains/Toolbox` (only needed for the GUI passthrough
  features)
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
incus admin init    # accept defaults
```

`security.nesting=true` (needed for nested Docker and for systemd services
that use user namespaces) is set automatically by `jailbee` on every container.

### 2. Delegate one UID/GID for host-file access

`jailbee` mounts a few host files (`~/.gitconfig`, `~/.gnupg`, the GPG agent
socket, …) into every container **as your real host UID** via `raw.idmap`,
while the container otherwise runs unprivileged. For that to work,
`/etc/subuid` and `/etc/subgid` each need two root-delegation lines: the
high shifted range Incus uses for the rest of the namespace, plus a
single-UID hole for your identity. These commands write both lines
idempotently (replace nothing — they read your `id -u` / `id -g`):

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

A fresh `incus admin init` usually installs the `root:1000000:1000000000`
line but never the second one, so this step is required. See
[Why the UID mapping is needed](#why-the-uid-mapping-is-needed) below for
the rationale, the security impact, and a verification recipe.

### 3. Install JailBee

```bash
uv tool install jailbee     # or: pipx install jailbee
```

This installs the `jailbee` and `jb` commands. Add the Qt dashboard with the
`gui` extra (`uv tool install 'jailbee[gui]'`), and install
`git+https://github.com/VRTFinland/jailbee` instead of the release when you
want the unreleased tip.

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
| `skills` | JailBee's [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills) in `~/.claude/skills` | Claude Code on your host does not know how to drive `jailbee` |

Every step is idempotent, so re-run `jailbee setup` after upgrading JailBee.
`--yes` installs everything without asking, `--only <step>` picks one, and
`--shell bash|zsh|fish` overrides shell detection (repeatable). zsh is the
one shell that needs a line in `~/.zshrc`; `jailbee setup` offers to add it
and prints it for you to copy if you decline.

The timer is a *user* timer, so it only runs while you have a login session
unless linger is enabled — `jailbee setup` prints the one-liner
(`sudo loginctl enable-linger $USER`) when it is not.

`jailbee doctor` reports each of these steps afterwards, so a missed one
does not stay invisible.

### 5. You're done with host setup

Confirm the CLI is on your `PATH`:

```bash
jailbee version
```

Everything else — creating the golden image and your first container — is
per repo. Continue in [Getting started](getting-started.md). The first thing
it has you run, `jailbee doctor`, flags any remaining host issues, i.e. the
conditional sections below.

---

The rest of this page is **conditional**. Apply a section only if `jailbee
doctor` or the symptom it describes says so;
[Troubleshooting](troubleshooting.md) links here by symptom.

## Host networking (only if you use a firewall)

Incus's default networking needs no changes. But if your host runs a
firewall, its two managed bridges must be allowed through:

- `incusbr0` — Incus's default. Used by the `strict` profile.
- `jailbee-loose` — created by `jailbee init`. Used by the `loose` profile so the
  per-repo allowlist ACL on `incusbr0` doesn't leak into "open egress" mode.

### firewalld

Add both bridges to the trusted zone:

```bash
sudo firewall-cmd --permanent --zone=trusted --add-interface=incusbr0
sudo firewall-cmd --permanent --zone=trusted --add-interface=jailbee-loose
sudo firewall-cmd --reload
```

### UFW

If your host runs `ufw` with the default `deny (incoming)` and `deny (routed)`
policies, containers won't get DHCP leases or reach the internet without
opening up the JailBee bridges. `jailbee doctor` flags a *missing* `jailbee-loose`
bridge — but that is an existence check only; it does not test DHCP/DNS
reachability. If a bridge exists yet containers get no IPv4 lease, suspect
the UFW rules below.

Both bridges need the same minimal opening: one `ufw route` rule plus three
`before.rules` lines, repeated per bridge.

**1. Allow forwarding from both bridges** — lets containers reach the
internet via NAT. Reply traffic returns automatically through UFW's
`ESTABLISHED,RELATED` rule, so no symmetric "out" rule is needed.

```bash
sudo ufw route allow in on incusbr0
sudo ufw route allow in on jailbee-loose
```

These are persistent (UFW saves them) and visible in `ufw status`.

**2. Edit `/etc/ufw/before.rules`** — UFW ships with a hardcoded silent
DROP for UDP 67 (DHCP) and a default-deny for UDP/TCP 53 (DNS) destined
to the host, because UFW assumes the host isn't a DHCP/DNS server.
Incus's `dnsmasq` is exactly that on each managed bridge, so we need
explicit ACCEPTs that run *before* `ufw-after-input` reaches the silent
drop.

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
```

Reload UFW: `sudo ufw reload`.

**3. Verify** — launch a fresh container on each bridge and confirm it
gets an IPv4 address within ~10 seconds:

```bash
incus launch images:ubuntu/26.04 ufw-test -c security.nesting=true
sleep 10
incus list ufw-test                       # IPv4 column should be populated
incus delete ufw-test --force

incus launch images:ubuntu/26.04 ufw-test-loose \
    -c security.nesting=true --network=jailbee-loose
sleep 10
incus list ufw-test-loose                 # IPv4 column should be populated
incus delete ufw-test-loose --force
```

If `IPV4` is empty after these steps, run `jailbee doctor` for diagnostics.

**Existing setups.** If your host already has the `incusbr0` rules from
an earlier JailBee install, you only need to add the `jailbee-loose` ones (one
`ufw route allow in` plus the three `before.rules` lines), then
`ufw reload`. `jailbee net loose <container>` will then work end-to-end.

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

The reason the DHCP/DNS rules can't be expressed via `ufw allow in`
commands: UFW puts user rules in `ufw-user-input`, which runs *after*
`ufw-before-input` jumps to it. UFW's hardcoded DHCP-suppression
DROP lives in `ufw-after-input`, hit only if nothing earlier accepted
the packet. With bridge-netfilter, the user rule's `-i <bridge>` match
sometimes sees the underlying veth instead of the bridge, so the
packet falls through to `ufw-after-input` and gets dropped silently.
Putting the ACCEPT into `ufw-before-input` sidesteps both issues.

## Kernel keyring limits (running many containers in parallel)

Running several JailBee containers in parallel exhausts the host kernel's
per-user keyring quota long before any disk fills up. The symptom is a
misleading error from runc when starting a container or launching
nested Docker inside one:

```
OCI runtime create failed: runc create failed: unable to start container
process: error during container init: unable to join session keyring:
unable to create session key: disk quota exceeded
```

"Disk quota exceeded" here means *kernel keyring quota*, not filesystem
space. Each Incus container's runc creates a session keyring under the
host uid that Incus maps container root to (default `1000000`). The
per-user default `kernel.keys.maxkeys=200` runs out after a handful of
concurrent containers — `jailbee doctor` warns when `maxkeys` is below its
recommended floor of 1000 (the values below set 2000 for headroom).

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

Step 2 above adds a second delegation line to `/etc/subuid` and
`/etc/subgid`. After the changes each file must contain (with `1000`
replaced by your `id -u` / `id -g`; on most single-user systems they're the
same):

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
and confirm `incus` was restarted.

**Why this is needed and what it actually means.** JailBee containers
deliberately combine two things that are usually mutually exclusive:

- **Unprivileged execution** — the container's UID 0 is mapped to a high
  host UID (e.g. `1000000`), so a container escape lands on a synthetic
  user with no real privileges on the host.
- **Sharing a few files with your real host UID** — your gitconfig, GPG
  socket and SSH config must work transparently inside the container,
  without rewriting ownership.

The mechanism is `raw.idmap`, which tells Incus: "shift everything to the
high range *except* UID `<UID>` — leave that one identity-mapped." The kernel
side of this is a multi-segment user namespace mapping. The userland side
is `newuidmap`, which refuses to install any segment that isn't authorised
in `/etc/subuid`. The `root:1000000:1000000000` line covers the shifted
range; the new `root:<UID>:1` line covers the carve-out. Both lines together
let `newuidmap` install the full mapping.

**Security impact: effectively none.** The new line authorises root to
delegate exactly one UID — your own. Any process you run already executes
as that UID, so this doesn't grant the system any privilege it didn't
already have. It only grants Incus the right to *project* that identity
into a container's user namespace, which is precisely what `raw.idmap`
exists to do.
