# Security and limitations

## Security model

Layering several mechanisms together — unprivileged, user-namespaced
containers, read-only binds for secrets (GnuPG, SSH, and gitconfig are
mounted read-only; note that user-declared `host_mounts` are read-write
unless you set `readonly: true`), a kernel-level egress allowlist, and
snapshot/rollback — gives stronger isolation than an in-process sandbox on
the host.

**Keys stay on the host.** Where a secret has an agent, JailBee shares the
agent's *socket* rather than the key: with `gpg.enabled`, the host
gpg-agent's socket is attached to the container and `SSH_AUTH_SOCK` points
at its SSH socket, so signing and SSH authentication are performed by the
host agent. `ssh.enabled` seeds only `config`, `known_hosts` and
`config.d/` into the shared `~/.ssh` — **private keys, `authorized_keys`
and sockets are never seeded**. A process in the container can therefore
ask the agent to sign for as long as it runs, but cannot take the key with
it; on a smartcard-backed key each signature can still require a physical
touch. This is the same "use it, don't hold it" property a credential proxy
provides, and it does not extend to plain API tokens mounted or written
into a container — those are ordinary files. That is why running an agent
with its own guardrails off is reasonable *inside* a `jailbee` container —
see [Running an agent without prompts](#running-an-agent-without-prompts).

### Remote SSH

The optional SSH server is a capability boundary inside the current user's
account, not a second kernel sandbox. The service runs as the same Unix UID
that runs the local `jailbee` CLI and has the same Incus access. With
`remote.ssh.restrict_host` at its default, a remote session reaches the
containers and the host repo's refs, and every host-reaching path JailBee
knows of is closed (listed below), whatever `commands.mode` says. What an
enabled command does to Incus containers is otherwise its normal behavior.
The two settings are separate axes: `commands.mode` picks which commands a
remote caller may run, `restrict_host` whether any of them may reach past
the containers and the git bridge into the host.

Every authorized client key has identical access. There are no per-key repos,
roles or command policies, and the fixed SSH username `jailbee` does not map
keys to different operating-system users. Authorize a key only when its holder
may exercise the complete configured remote surface across every registered
repo. Removing a key blocks new connections immediately but does not revoke an
already-authenticated connection.

The default listener is IPv4 loopback only (`127.0.0.1:8022`). It is not a
network authentication boundary and is not remote-device deployment guidance:
changing `listen`, exposing the port through a tunnel, VPN, firewall rule or
port forward, and securing that route are the operator's responsibility.
JailBee does not configure TLS, a firewall, a VPN or NAT for this service.

Entry points are deliberately narrow, but they are not read-only. In
particular, the remote dashboard shows registered repos only and exposes its
container and git-bridge actions: creating, starting, stopping or destroying
containers and moving commits can therefore affect host state. It withholds
what would reach the host beyond that — the config editor (a config decides
host mounts and this very policy), the diff pager (a pager can start a
shell), "Open PR" and GUI app launches (a browser or window would open on
the host's display). Every process the service starts is marked as remote
(`JAILBEE_REMOTE_SSH=1`, inherited by everything it starts in turn) and runs
with `LESSSECURE=1`. Every SSH session, restricted or not, also carries
`JAILBEE_SSH_SESSION=1`, which keeps the dashboard to registered repos, the
Qt dashboard refused and the post-install setup offer unmade: the one at the
other end is not at this host.

While `restrict_host` is on, the commands that manage the host itself are
refused in every mode, `full` and an allowlist naming them included:
`config edit`/`init`, every `remote ...` command, `setup`, `init`, `apply`,
`base build`/`prune`, `net install`/`refresh`/`unregister`, `net egress
add`/`rm` (which accept the host's own and its LAN's addresses), `registry
up`/`down`, the `account` commands that write, `mount`, `port to-container`,
and the GUI launchers (`gui`, `ide`, the browsers, `apps run`). The startup
log names any allowlisted command that stays refused this way, and every
public command is classified one way or the other by the test suite, so a
new one cannot land unclassified.

`commands.mode: full` is a high-trust setting: it grants every current
public JailBee command and automatically grants public commands added by
future versions — the container-side ones while `restrict_host` is on,
all of them once it is off. It also grants every hidden *alias* of a public command (`merge`,
`pull`, `push`, and a few others — see [`remote.ssh`](config.md#remotessh)),
since those are policy-checked against the public command they alias, not
their own hidden spelling. Hidden internal commands with no public twin
(`_remote-console`, the deprecated `jailbee claude ...`/`chrome-pool ...`
groups, and the rest) are never included, but that exclusion does not make
`full` a safe default. Prefer an exact-leaf allowlist such as `ls` or `git
pull`; allowing `git pull` does not grant sibling `git` commands, though it
does grant `git --help` (a public group's own help is always permitted once
some command under it is).

The SSH protocol surface is also fail-closed:

- public-key authentication is the only authentication method; password,
  keyboard-interactive, host-based and GSS authentication are disabled;
- SFTP, SCP, agent forwarding, X11 forwarding, TCP and Unix-socket forwarding,
  and remote listeners are disabled;
- client environment requests, including `SendEnv`, are accepted by the
  protocol but ignored: the client's environment never reaches the child
  process, which is built from the service's own environment;
- the interactive `shell` entry point is a restricted JailBee console, not a
  POSIX shell, and implements no pipes, redirection, expansion or executable
  lookup;
- one-shot commands are parsed into an argv without invoking a shell, and
  `--repo` resolves an exact registered prefix rather than a client-supplied
  filesystem path; and
- in every command mode, `full` included, a remote command may not set a
  path-typed option or argument — `--config` would read any host file (its
  parse errors echo the contents) or make any host directory a repo whose
  config decides host mounts — nor `jailbee new --mount`, whose read-write
  bind of the host repo includes `.git`, where a planted hook runs on the
  host. The argv is parsed by the command's own parser to decide this, so
  short-option clusters and `--opt=value` forms are covered.

- the git bridge updates refs only. `git checkout`, `branch` on the host,
  `git pull` into the host's checked-out branch or with `--checkout`, and a
  `git fetch` that would move the checked-out branch are refused before
  anything is fetched; a submodule new in the container is not cloned into
  the host tree (skipped with a warning, the rest still transfers).
  Whatever lands in the checked-out tree — a repo config that decides host
  mounts, a build script — is what the host's own tools read next. Pull
  into another host branch (`--into`) or fetch into one (`--as`), and check
  it out on the host;
- publishing to GitHub with the host's own `gh` (`pr`, `submodule pr`,
  `review apply`, `issue apply`) stays available — the key holder is the
  human the outbox is reviewed by — but never with `--yes`, so each action
  is shown and confirmed; `--web`/`--open` are refused as host browsers;
- a branch whose autostart config widens privileges (the escalation prompt
  of `jailbee new`) is refused outright, `--yes` included: over SSH the one
  answering that prompt is the remote user it exists to hold back.

A container created with `jailbee new --mount` shares the host repo's working
tree, `.git` included, so a restricted session may neither create one nor
enter one: `shell`, `tmux`, `exec` and the GUI app launchers refuse a
mount-mode container, and clone-mode containers are unaffected.

One host resource stays reachable from inside a container on purpose: the
Wayland display socket, attached whenever the host session is Wayland. Any
process in the container — a remote session's shell included — can open a
window on the host's screen with it, and JailBee's own GUI launchers are
withheld remotely only because a window there helps no remote user. A
Wayland client draws its own surfaces and cannot read or drive other
windows, and no X11 socket is shared. The host's session D-Bus and
PulseAudio sockets, which do reach further, are opt-in
([`gui`](config.md#gui)).

A server imports its routing and session marking when it starts, so one left
running across an upgrade would enforce the old version's rules. It
therefore restarts itself when the installed version changes, before
serving another session (see [Installation](installation.md#optional-ssh-service)).

`remote.ssh.restrict_host: false` (or `jb remote ssh serve
--no-restrict-host` for one run) lifts every host restriction above and in
the dashboard paragraph at once: no argument check, no session marker, no
`LESSSECURE`, and an allowed command then reaches the host exactly as it
does locally. It is one switch on purpose — a partly lifted boundary is
harder to reason about than either state — and the startup log announces
`host restrictions: OFF`. A server started from inside a restricted
session inherits its marker and stays restricted whatever the setting says.

The user journal records source address, authorized-key fingerprint, bounded
route/repository/command identifiers, decision and exit status. It does not
record key material, complete argv, environment values, terminal contents or
user input. See [Installation](installation.md#optional-ssh-service) for key
rotation, service status, journal inspection and recovery operations.

### Running an agent without prompts

A coding agent asks before it acts because on your own host a wrong command
is unbounded. Inside a container it is bounded by the container, so the
prompts cost more than they buy, and turning them off is the intended mode
rather than a corner you cut:

- **Permission prompts** — `claude --dangerously-skip-permissions` (or
  `--permission-mode bypassPermissions`). JailBee itself runs Claude this
  way for `jailbee pr`. Put it in `claude.command` and every container's
  autostart window comes up in that mode.
- **The agent's in-process sandbox** — a separate switch on the same axis
  (Claude Code's `dangerouslyDisableSandbox` opts a single bash command out
  of its own sandbox). Redundant inside a container that is already the
  boundary.

The point of the trade is that you size the blast radius *before* the run,
with the container's config, instead of adjudicating it prompt by prompt.
So it is worth knowing exactly what you sized:

**Reachable by an unattended agent.** The container's own clone of the repo.
Everything in `host_mounts` — read-only entries can be read and used, and a
read-only `~/.gnupg` plus the host gpg-agent socket means the agent can ask
for signatures for as long as the container runs, even though it can never
take the key. The shared state layer, which is *shared*: `<shared_dir>/claude`
holds Claude's own credentials and the shared `~/.ssh` holds whatever you put
there, and damage to either is not contained to one container. In `loose`
mode, the network — including a push to `origin`. With `github.enabled`, the
container's own `GH_TOKEN` — see the limitation below.

**GitHub token scope is a documentation contract, not an enforced one.**
The recommended fine-grained PAT (`github.api_tokens`, injected as
`GH_TOKEN`) is read-only by design — Contents, Issues, Pull requests, and
Metadata all set to Read — so that every GitHub write an in-container agent
proposes must go through the host-side outbox (`jailbee issue apply` /
`jailbee review apply`), reviewed and applied with the host's *own*,
independently authenticated `gh`. **jailbee cannot verify a fine-grained
PAT's effective permissions** — `jailbee doctor` reminds you of the intended
scope but never probes GitHub to check it. A PAT you (or an org policy)
scope wider than read-only lets an agent (or anything running as it) write
to GitHub directly with the container's own `gh`, bypassing the outbox
review gate entirely — the same trust boundary the read-only recommendation
in [`config.md`](config.md#github) and [`git-bridge.md`](git-bridge.md#github-cli-gh-inside-containers)
exists to hold.

**Out of reach.** Your host's filesystem and dotfiles. Other repos'
containers and their shared dirs. `optional_mounts` you haven't attached with
`jailbee mount`. Private keys, which never leave the host agent. In `strict`
mode, every host not on the allowlist.

The practical shape, then: stay in `strict`, bind read-only what the build
genuinely needs, leave sensitive `optional_mounts` detached, and take a
snapshot (`jailbee snapshot create`) before a long unattended run.

### Git remote & push

After `jailbee new` clones the source repo into the container, the clone's
`origin` is rewritten from the RO mount path (`/mnt/host-source`) to the host
repo's real upstream URL, and branch tracking is set explicitly so
`git push`/`fetch`/`gh` work without `-u` once the network ACL allows
them. The `--shared` clone semantics survive — objects continue to read
from the mount through `.git/objects/info/alternates`.

The container's remote is always named `origin`, whatever the host calls its
own upstream (see [Which remote is the
upstream?](config.md#which-remote-is-the-upstream)) — the clone is jailbee's
own, so its naming is jailbee's invariant rather than something inherited.

**By design, `github.com` is NOT in the default strict-mode
`egress_allow`.** Day-to-day strict-mode work runs offline-of-GitHub;
when you need to push or use `gh`, temporarily switch to loose with
`jailbee net loose <name>`, perform the write op, then go back to strict
with `jailbee net strict <name>`. This keeps unattended agent runs from
producing surprise pushes.

### Port forwards

A port forward (`host_ports` in config, or an ad hoc `jailbee port
to-container`/`jailbee port to-host`) is a deliberate hole through the
boundary `net strict` otherwise enforces. It works because the traffic
**never traverses the bridge the network ACL is attached to**: each forward
is one Incus `proxy` device, and Incus's forkproxy connects directly into
(or out of) the container's network namespace rather than sending packets
over the NIC. The ACL — applied in `strict` mode only — is deny-by-default
on both egress and ingress (see `src/jailbee/network.py`), so neither
direction of a forward is filtered by it.

Both directions matter, and each bypasses a different half of that
default-deny: a `to-container` forward (the `host_ports` case, e.g. the adb
recipe in [project-config.md](project-config.md#talking-to-android-devices-over-adb))
lets the container reach a host service that the egress deny would
otherwise have blocked. A `to-host` forward lets something on the host
reach into the container — traffic the ingress deny would otherwise have
blocked. The `to-host` direction does **not** give the container any new
outbound reach: the host is the one initiating the connection, into a port
the container is already listening on.

It is opt-in either way — a forward exists only because it is declared in
`host_ports` or because someone ran `jailbee port`. `jailbee net status`
lists every active forward alongside the strict-mode summary, so the real
boundary — ACL plus whatever forwards are open — can be read off one
command, and `jailbee doctor` separately reports `host_ports` entries that
are declared in config but missing from a running container.

### Autostart and the network exposure window

An `autostart` stage that sets `network: loose` widens the container's
egress for as long as *the whole stage* runs — every chain, every step in
it — not just for one step's duration the way the deprecated step-level
`network` did. A stage with several parallel chains genuinely needs this:
nothing may flip the profile out from under a chain that's still running,
so the switch and its restore bracket the stage as a unit. See [Stages and
chains](config.md#stages-and-chains).

`user.jailbee.autostart_in_progress` is what keeps `jailbee-net-refresh`'s
TTL revert from racing a stage's own network swap (see
[`loose_auto_revert`](config.md#loose_auto_revert)). For a **detached**
stage the flag carries the supervisor's own pid rather than a bare `"1"`,
so a supervisor that dies mid-run without reaching its own cleanup —
killed, OOM, a host reboot — stops pinning the container loose:
`jailbee-net-refresh` sees no live process behind the pid and reverts on
schedule instead of leaving the container exposed indefinitely. A `"1"`
written by an older `jailbee` (before the pid was tracked) still reads as
held, so an in-flight run started before an upgrade isn't reverted out from
under itself.

Restoring the entry network mode after a detached stage is
**compare-and-swap**: the supervisor re-reads the container's current mode
first and only restores if it's still the mode *this stage* set. Run
`jailbee net strict`/`jailbee net loose` by hand while a detached stage is
mid-run and your choice stands — the stage's own restore, whenever it
finally happens, silently no-ops instead of clobbering it (`jailbee net`
itself only warns and proceeds; it does not refuse). The foreground path
has no such race to guard against — nothing can run concurrently with a
stage the CLI is blocking on — so it keeps the old unconditional restore.

## Egress overrides

`jailbee net egress add` widens a container's, or a repo's, strict-mode
allowlist **without passing code review**. That is the point of the feature
and also its risk: unlike `egress_allow` in `.jailbee/config.yaml`, an
override is never seen by a teammate, a reviewer, or CI — it lives in the
container's own `user.jailbee.egress_extra` label (container scope, the
default) or in host-local state (`--repo`, applying to every container of
the repo on this machine).

The mitigation is visibility, not a prompt: `jailbee net egress ls` shows
every applicable entry and where it came from (`config`, `repo-override`,
`container`), and `jailbee net status` lists the overrides for **the repo
whose checkout you run it from** — both host-local sections that never leave
the machine, so they can only be read by someone who already has a shell
there. `jailbee net status` (like its other sections) is cwd-scoped, not a
host-wide audit: to see another repo's overrides, run it from that repo's
checkout. `--repo` is the wider of the two scopes; the flag itself is the
confirmation that the change is repo-wide rather than one container.

Overrides are **additive only**. Neither scope can revoke what
`config.yaml` grants — `jailbee net egress rm` refuses an entry that exists
only in the config file, pointing at it instead — so a repo cannot be
quietly narrowed on one developer's machine. `jailbee net egress export`
prints the whole `egress_allow:` key with host-local overrides folded in,
for pasting over the config to promote a durable one into git; the
overrides it just promoted can then be dropped with `jailbee net egress
rm` now that `config.yaml` covers them.

**No container can grant itself egress.** A container holds no `jailbee`
binary and no access to the host's Incus socket, so code inside it —
including an agent, and including the untrusted head checked out by
`jailbee new --pr` for review — cannot reach these commands. That is the
first question a security reviewer should ask, and the answer is
structural, not policy: there is nothing to invoke. This is unrelated to
the `branch_config` escalation gate, which weighs what a branch's
*committed* autostart configuration is allowed to grant itself on
`jailbee new`; `jailbee net egress` is the operator, at a host shell,
typing a command.

## Limitations

- Linux host only
- One IDEA at a time across containers (shared JetBrains profile). Chrome,
  Firefox, Gradle and Maven run per-container from their own pool slot
  instead, seeded from the most recent slot — see `jailbee pool ls` /
  `jailbee pool prune` to inspect or clean (`jailbee chrome-pool ls/prune`
  is a deprecated alias scoped to Chrome).
- NVIDIA GPU passthrough requires extra setup (not covered by `jailbee init`)
