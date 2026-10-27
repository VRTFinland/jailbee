# Security and limitations

## Security model

JailBee combines unprivileged, user-namespaced Incus containers, selective
host mounts and a kernel-level network allowlist. It shares the host kernel:
it is not a VM or a guarantee against hostile code. The dev user's UID/GID
is mapped to the host user's UID/GID so shared files work, and nesting is
enabled for workloads such as Docker. Host mounts, devices, sockets and
credentials deliberately widen that boundary; review the effective config
before running untrusted code. User-declared `host_mounts` default to
read-write unless `readonly: true` is set. Container snapshots help recover
container storage, not host bind mounts, separate shared volumes or external effects.

**Read-only does not mean secret.** With `gpg.enabled`, JailBee shares both
the host's `~/.gnupg` directory read-only and gpg-agent sockets. Agent
requests do not export private keys through the socket, but disk-stored key
material in the mounted directory can still be read and copied. Processes
can request signing or SSH authentication through the agent; confirmation,
PIN and touch requirements depend on the agent and key configuration.
Hardware-backed keys may avoid exposing private key material as files, but
do not make every request require a touch.

`ssh.enabled` seeds the top-level `config`, `known_hosts` and `config.d/`
into an initially empty shared `~/.ssh`. It excludes other top-level files,
not secrets recursively: `config.d/` is copied recursively and symlinks are
followed, so audit those sources for keys or other sensitive content. The
shared SSH directory is **read-write**, and disabling `ssh.enabled` skips
seeding and permission setup, not the default shared-cache mount. Plain API
tokens, OAuth credentials and key files exposed inside a container are
readable credentials, not agent-only capabilities. See
[Running an agent without prompts](#running-an-agent-without-prompts).

Host-local per-repo settings live outside the checkout at
`~/.config/jailbee/repos/<container_prefix>.yaml` (or under
`$XDG_CONFIG_HOME`). This is the supported location for per-repo secrets such
as `github.token`; JailBee creates the directory as `0700` and each local
config file as `0600` when writing through JailBee. Loading a token file
with group/other access warns rather than rejecting it; `jailbee doctor`
reports insecure permissions as a failure. Keep token files at `0600`,
including `~/.config/jailbee/global.yaml` while it contains the legacy
`github.api_tokens` map. `container.env` is not secret storage: values are
written into the Incus profile and are visible with `incus profile show`.
Arbitrary `agents.<name>.env` values are not masked by `config show` either.
Injected `GH_TOKEN` is also written into the container's
`/etc/profile.d/jailbee-github.sh`; disabling the integration does not remove
an existing copy. Revoke exposed credentials at the provider when needed.

### Remote SSH

The optional SSH server is a capability boundary inside the current user's
account, not a second kernel sandbox. The service runs as the same Unix UID
that runs the local `jailbee` CLI and has the same Incus access. With
`remote.ssh.restrict_host` at its default, JailBee restricts host-reaching
commands and arguments as described below, whatever `commands.mode` says.
It still permits container operations, ref transfers and confirmed GitHub
publication with the host's credentials. It does not remove capabilities
already exposed inside containers. The two settings are separate axes:
`commands.mode` selects callable commands; `restrict_host` adds host-access
checks to those commands. Neither is a substitute for reviewing mounts,
credentials and display access.

Every authorized client key has identical access. There are no per-key repos,
roles or command policies, and the fixed SSH username `jailbee` does not map
keys to different operating-system users. Authorize a key only when its holder
may exercise the complete configured remote surface across every registered
repo. Removing a key blocks new connections immediately but does not revoke an
already-authenticated connection.

An operator can narrow that shared repo surface at the host-controlled
repository exclusion boundary using the
`remote.ssh.excluded_repos` list in `~/.config/jailbee/global.yaml`:

```yaml
remote:
  ssh:
    restrict_host: true
    excluded_repos: [private-app]
```

Each entry is an exact registered `container_prefix`, not a path, glob, or
key-specific grant. The excluded repo is unavailable via direct SSH repo
selection and is omitted from the remote dashboard and console. Supported
aggregates such as `ls --all`, `job ls --all-repos`, `base usage --all`, and
repo summaries such as `net status` operate only on visible repos; filtering
is applied before probing, rendering, totals, or actions. The unified
`outbox` commands, `outbox ls --all-repos` included, are refused outright
while any repo is excluded (the deprecated `review ls --all-repos` is still a
filtered aggregate). Unknown and excluded repo selections give the same error. This is one
host-wide visibility boundary for all keys, not per-key authorization, and it
does not alter local CLI, TUI, or Qt behavior. Each new command channel takes
a routing/repo-scope policy snapshot; a running channel keeps that snapshot.
Mount grants are checked separately against freshly loaded host policy at
mount time, and new GUI forwarding requests re-read the `rdp` and `waypipe` flags.

With exclusions active, commands whose output or effects cannot safely be
scoped are refused instead of exposing host-wide data: `config show --layer
global`, `account ls`, `account group ls`, and `doctor`. Commands not
classified for safe scoping also fail closed. Existing command-mode and
host-restriction checks continue to apply; exclusions require
`restrict_host: true`.

This is an SSH routing and JailBee-command boundary, not filesystem or kernel
isolation. It does not revoke data already made available inside an allowed
container. For example, an allowed container may have a host directory exposed
through a configured bind mount, or may reach a service over the network or
use shared credentials. Review mounts and other host-to-container channels
separately; excluding a repo does not remove an allowed container's mounts.

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
the host's display; with `remote.ssh.rdp` on, or inside a `waypipe ssh`
session with `remote.ssh.waypipe` on, launches go to the shared RDP display or
the client's display instead). The admitted `exec --gui` route is an exception to the
named GUI-launcher restrictions, described below. Restricted child processes
carry `JAILBEE_REMOTE_SSH=1` and `LESSSECURE=1`, inherited by their children.
Every SSH session, restricted or not, also carries
`JAILBEE_SSH_SESSION=1`, which keeps the dashboard to registered repos, the
Qt dashboard refused and the post-install setup offer unmade: the one at the
other end is not at this host.

While `restrict_host` is on, the commands that manage the host itself are
refused in every mode, `full` and an allowlist naming them included:
`config edit`/`init`/`migrate`, every `remote ...` command, `setup`, `init`,
`apply`, `base build`/`prune`, `net install`/`migrate`/`refresh`/`unregister`,
`net loose` and `net egress add` (either can open the host's own and its
LAN's addresses to the container), `orchestrate grant`, `registry up`/`down`, `display up`/`down`,
`litellm
up`/`down`/`login`/`logout`/`logs`, the `account` commands that write,
`port to-container`, and the GUI launchers (`gui`, `ide`, the browsers, `apps
run`). `mount` is host-only unless `remote.ssh.mount_roots` or
`remote.ssh.mounts` unlocks it. With `remote.ssh.rdp` on, or inside a
`waypipe ssh` session with `remote.ssh.waypipe` on, `ide`, the browsers and
`apps run` are permitted and draw on the shared display or the client's display
instead; `gui` stays host-only.
With `remote.ssh.network` on, `net loose`, `new --net loose` and container-scope
`net egress add` are permitted too; without it `new` is refused only when
`--net` names `loose`, in any spelling of the case. `net strict` and container-scope
`net egress rm` only narrow a container and are always permitted; `--repo` on
`net egress add`/`rm` is refused regardless, because it writes the host-local
repo layer.
`orchestrate grant` is refused in a restricted session while
`remote.ssh.orchestration` is `off`; `revoke`, `show` and `ls`
are not affected. With `ssh` (the default), a grant may give children only what the session
could do itself: loose network and egress hosts need `remote.ssh.network`,
optional-mount kinds need `remote.ssh.mounts`, and `mount_roots` never
qualifies. With `orchestrator`, it may give only what the orchestrator
container permanently holds, as defined. `orchestrator` is a ceiling on what a grant may carry, not on what a session can first give the orchestrator itself: with `remote.ssh.network` or `mounts` on, a session could first make such rights permanent on the orchestrator (for example `mount --no-revert`, loose without a TTL, `net egress add`) and grant them on, which is within what the session could do itself. The number of children and the
operations are never bounded. A remote grant can always narrow, and elements
the stored grant already holds, including wider host-set ones, are kept. The
bound is checked when the grant is made: turning `network` or `mounts` off
later does not narrow stored grants, so revoke or edit those on the host.
The startup log names allowlisted commands still refused by these restrictions.
A regression test checks that every public leaf has a host/container
classification; restricted routing also rejects unclassified commands at runtime.

`commands.mode: full` is a high-trust setting. In restricted sessions it
grants classified public JailBee commands, and newly added or unclassified
commands fail closed. With `restrict_host: false` and no inherited restricted
session marker, `full` admits public leaves including future ones, except
reserved routes and hidden internal commands. It also grants every hidden
*alias* of a public command (`merge`,
`pull`, `push`, and a few others — see [`remote.ssh`](config.md#remotessh)),
since those are policy-checked against the public command they alias, not
their own hidden spelling. Hidden internal commands with no public twin
(`_remote-console`, the deprecated `jailbee claude ...`/`chrome-pool ...`
groups, and the rest) are never included. The default policy uses `full`,
bounded by this classifier and `restrict_host`; operators needing narrower
command access should prefer an exact-leaf allowlist such as `ls` or `git
pull`; allowing `git pull` does not grant sibling `git` commands, though it
does grant `git --help` (a public group's own help is always permitted once
some command under it is).

The SSH protocol surface is also fail-closed:

- public-key authentication is the only authentication method; password,
  keyboard-interactive, host-based and GSS authentication are disabled;
- agent forwarding, X11 forwarding, Unix-socket forwarding and remote
  listeners are disabled, and so is TCP forwarding, with two exceptions (see
  [Remote GUI](#remote-gui)): `127.0.0.1:13389` (the shared display), for any
  authorized key, only while `remote.ssh.rdp` is on; and one reverse unix
  forward per connection, for `waypipe ssh`, requested by a connection
  authenticated by an authorized key, only while `remote.ssh.waypipe` is on.
  JailBee listens for that forward at its own path under the state directory
  (the client never chooses a host path), and the listener's directory is
  mounted into `jailbee-display` only;
- SFTP and SCP are on by default only under `commands.mode: full`, where
  `shell` already reaches the same files; they are off by default under
  `allowlist` and `disabled`, and an explicit `remote.ssh.files` always wins.
  When enabled, they serve one thing only (see [File transfer](#file-transfer)) and every other
  subsystem is still refused;
- client environment requests, including `SendEnv`, are accepted by the
  protocol but ignored: the client's environment never reaches the child
  process, which is built from the service's own environment. The one
  exception is the terminal's identity — `LC_TERMINAL`,
  `LC_TERMINAL_VERSION`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION` and
  `COLORTERM`, each only when its value is a short plain identifier — which
  the dashboard needs to work around terminal-specific bugs (iTerm2's mouse);
- the interactive `console` entry point is a restricted JailBee console, not a
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
  `review apply`, `issue apply`, `outbox apply`) stays available — the key holder is the
  human the outbox is reviewed by — but never with `--yes`, so each action
  is shown and confirmed; `pr push --web` and `pr open` are refused as host browsers;
- a branch whose autostart config widens privileges (the escalation prompt
  of `jailbee new`) is refused outright, `--yes` included: over SSH the one
  answering that prompt is the remote user it exists to hold back.

A container created with `jailbee new --mount` shares the host repo's working
tree, `.git` included, so a restricted session may neither create one nor
enter one: `shell`, `tmux`, `exec` and the GUI app launchers refuse a
mount-mode container, and clone-mode containers are unaffected.

Host display access is a separate capability. The Wayland socket is
attached on a container's first GUI launch (or every boot with
`gui.wayland: always`; [`gui`](config.md#gui)). Once attached, any process
in that container can connect, including a remote shell. Named GUI launchers
are restricted as described above, but **`exec -d --gui` is admitted**. Without
a remote-GUI session marker it selects the host display and can attach its
socket if the SSH service's environment provides a usable display; with the
marker it selects the shared remote display. Do not treat the launcher
restrictions as a guarantee that remote commands cannot open host windows.

JailBee shares no X11 socket. Wayland normally isolates clients' surfaces,
but the compositor and its supported protocols remain part of the trust
boundary; socket access is not a guarantee against display-server bugs or
additional compositor capabilities. Host session D-Bus and PulseAudio
sockets are opt-in ([`gui`](config.md#gui)). With `remote.ssh.rdp` on,
boot-time setup can also attach the shared display directory read-only to
dev containers, allowing their processes to draw on the shared RDP display
(see [Remote GUI](#remote-gui)).

A server imports its routing and session marking when it starts. Its version
watcher detects installed-version changes, refuses new command sessions and
exits after draining existing work; the systemd service restarts it. A
foreground `serve` process exits with code 75 rather than restarting itself.
This detects version changes, not every editable-source modification (see
[Installation](installation.md#optional-ssh-service)).

`remote.ssh.restrict_host: false` (or `jb remote ssh serve
--no-restrict-host` for one run) disables the host-restriction command and
argument checks and stops adding `JAILBEE_REMOTE_SSH` and `LESSSECURE` to
child processes. Treat admitted commands as host-user capabilities. It does
not disable the command policy, reserved/internal-route checks or the general
`JAILBEE_SSH_SESSION` marker: the dashboard still lists registered repos only,
Qt is refused and the setup offer stays suppressed. The startup log announces
`host restrictions: OFF`. A server started from inside a restricted session
inherits its restricted marker and stays restricted whatever the setting says.

The user journal records source address, authorized-key fingerprint, bounded
route/repository/command identifiers, decision and exit status. It does not
record key material, complete argv, environment values, terminal contents or
user input. See [Installation](installation.md#optional-ssh-service) for key
rotation, service status, journal inspection and recovery operations.

#### File transfer

`remote.ssh.files: true` lets an authorized key use `sftp` and `scp` against a
container's repository directory, in both directions. It is a single switch for
reading and writing together. Unset, it follows `commands.mode`: on under
`full`, where `shell` already reaches the same files, and off under
`allowlist` and `disabled`, so a narrowed command policy does not silently
grant access to every container's repo. An explicit value always wins.
Enabling it requires
`jb remote ssh restart` if the server started without file-transfer support.
Disabling it refuses new SFTP channels without a restart; existing channels
retain their scope. Close those channels or restart the service to end them.

The server is not a stock SFTP server, which would expose the host's
filesystem. It presents a virtual tree: `/` lists the running containers of
non-excluded repositories, and `/<full-container-name>/...` is that
container's repository directory. File operations run inside the container,
not directly against the host filesystem. The path checks reject targets
resolving outside the repository:

- every operation is one `incus exec` of a fixed script; names and offsets
  reach it as arguments, never as script text;
- the target is resolved inside the container and refused when it leaves the
  repository; `lstat` can show an escaping symlink and unlink can remove the
  link itself without removing its target;
- links cannot be created, file names must be UTF-8, the top of the tree is
  read-only, and files cannot be moved between containers;
- files are written as the user who owns the repository (never root), a file
  cannot grow past 2 GiB, and at most eight `incus exec` calls run at once
  across all sessions;
- containers that are stopped, belong to an `excluded_repos` repo, or are not
  JailBee-managed are not listed and cannot be entered; neither are containers
  created with `--mount`, since their repository directory is the host's own
  checkout;
- every operation that touches a container, and every one that is refused,
  writes exactly one audit line with the key fingerprint, container, path
  (and the new path of a rename), operation and outcome; path canonicalisation,
  `close` and `fsync` touch nothing and are not audited.

This is not an inode-level or race-free boundary against a process already
running inside the container: path resolution and the operation are separate,
so concurrent symlink changes can race the check. Existing hardlinks and
mounts inside the repository remain reachable, including host data exposed
by those mounts. Submodules are part of the tree too. Keep secrets out of
that reachable tree; Rsync is not supported.

### Remote mounts

Over a restricted SSH session (`restrict_host: true`), `jailbee mount` is
refused unless the resolved host path of the mount matches exactly one entry
in `remote.ssh.mount_roots` (a host-global list in `~/.config/jailbee/global.yaml`).
A match qualifies only when `os.path.realpath(mount.host)` equals the root's
`path` exactly; no subdirectory, no prefix match. The device source passed to
Incus is the root's own spelling, not the repo config's. A root entry may
declare `readonly: true` (the default), in which case the mount is attached
read-only even if the `optional_mounts` entry says otherwise. The effective
readonly flag is `mount.readonly or root.readonly` for a newly added device.
An already attached mount gets only its deadline updated: its existing source
and read-only setting are not reconciled. Detach and reattach to apply a
changed grant. A non-empty `mount_roots` requires `restrict_host: true`; a
`repos` list (when present) only scopes which registered repos may attach that root.

`remote.ssh.mounts: true` grants every `optional_mounts` entry as defined,
with no path check and no readonly override; the repo config alone decides
the host path. It does not require `restrict_host`.

A root is refused at `jailbee mount` time (for the repo whose container is
being mounted) when a *different* read-write `host_mounts` or `optional_mounts`
entry resolves to the root or an ancestor of it. `jailbee doctor` warns when
any registered repo has a writable mount covering a root, and when a listed
root directory does not exist on disk.
Removing a root does not detach a mount already attached.

The decision rests on the resolved host path against a host-global list;
neither a repo's `.jailbee/config.yaml` nor a branch autostart config can
set `mount_roots`. Exact-match only is deliberate: no path inside a root is
ever resolved, so a writable root cannot be used to plant a symlink that
would reach outside the declared directory.

### Remote GUI

With `remote.ssh.rdp: true`, GUI apps launched over SSH draw on a shared RDP
display (see [Remote GUI over SSH](remote-gui.md)); `remote.ssh.waypipe`
(on by default) separately permits native windows over `waypipe ssh`, described
at the end of this section. The RDP exposure:

- The weston compositor listens on the display container's loopback only, and
  an Incus proxy device publishes it on the host's loopback
  (`127.0.0.1:13389`) only. The bridge address refuses connections from the
  host and from sibling containers.
- The RDP connection is TLS with a self-signed certificate and no Network
  Level Authentication; weston checks no login, so any local user of the host
  can connect to `127.0.0.1:13389`. JailBee assumes a single-user workstation.
- Over SSH, any authorized key may forward to that one destination while
  `remote.ssh.rdp` is on; the flag is re-read for every forward request, so
  turning it off refuses new tunnels at once (open ones run until they close).
  The forward is not tied to a key that has launched an app: every authorized
  key runs under the same server-wide policy and could launch one, so such a
  binding would protect nothing.
- All containers share one screen and the RDP clipboard: one container can
  draw over another's windows and read what is on the clipboard.
- Boot-time attachment of the shared display directory requires
  `remote.ssh.rdp` and an existing display directory. The client mount is
  read-only. Turning the flag off does not detach existing mounts or revoke
  old sessions and tunnels. Every client shares the host user's idmap, so a
  writable mount would let a compromised container
  delete or replace weston's socket with its own and capture every other
  container's windows, keystrokes and clipboard; read-only means a container
  cannot replace the socket through this mount. A read-only bind still
  permits socket connections, so any process in a container can open a window on the shared
  display. That is the exposure below, and it is why containers are not
  isolated from each other on this screen.
- The display container uses the dev user's idmap, like the client containers,
  so the shared socket is owned by the same host user.
- With SSH repository exclusions active, the GUI launchers (`ide`, the
  browsers, `apps run`) are refused over SSH regardless of this setting.
  `exec -d --gui` is not: it is admitted as a command scoped to one container
  of a repo the session may see. Repository exclusions do not isolate the
  display: it is one screen shared by participating containers, including
  excluded repos, and any session allowed to forward to it sees all of it.
  Command allowlists and repo exclusions do not narrow this display access.

With `remote.ssh.waypipe` on (the default), each `waypipe ssh` session (see
[Native windows with waypipe](remote-gui.md#native-windows-with-waypipe)) runs
one waypipe server per container inside `jailbee-display`. Each server's socket
lives in its container's own directory under `state/waypipe-servers/`, which is
mounted read-only into that container only, on its first waypipe launch. A
container therefore cannot open a window through another container's server,
and none of these sockets is in the shared display directory that `rdp` mounts.
Sessions of one container share that directory. No two containers share a
server process. Untrusted Wayland traffic is parsed by those servers and the
laptop's waypipe client processes the forwarded protocol. The display container
is an intermediary, not a guarantee that hostile display traffic cannot affect
the client.

### Running an agent without prompts

Unattended agents are a supported use case, but a container does not make
permission prompts or an agent's own sandbox redundant. Disabling them
removes defense in depth: a mistaken or injected command can affect every
mounted path, shared login and reachable service available to the process.

The bundled Claude preset's headless PR command uses
`--dangerously-skip-permissions`. Setting that flag in
`agents.claude.command` also opts the agent's autostart invocation out of
its permission prompts; it is not needed for ordinary container use. An
agent's own sandbox is a separate control, and disabling it is a separate
trust decision. Check the installed agent's supported options before changing
either.

Size the exposure *before* an unattended run, rather than assuming container
isolation covers every action:

**Reachable by an unattended agent.** The container's own clone of the repo,
or the host working tree itself in `--mount` mode. By default, an existing
repo `.local/` directory is also shared read-write. Everything in
`host_mounts` — read-only entries can be read and used, and the host
gpg-agent socket lets the agent request signatures without receiving the
private key through the socket. This does not protect key files exposed by
a mount. The shared state layer, which is *shared*: `<shared_dir>/claude`
holds Claude's settings and the shared `~/.ssh` holds whatever you put there,
and damage to either is not contained to one container. Claude's login lives
in that directory too, unless the repo is in a credential group — then it is
the group's directory, mounted at `~/.claude-creds` and shared with every
other repo in the group. Pooled caches have per-container slots, but some
cache subtrees use hardlinks; they are not isolation from hostile peers.
Read-write shared directories can also carry modified scripts or settings
back to host tools. Network destinations allowed in `strict` are reachable,
and `loose` widens that access. Either mode can permit a push to `origin`
when transport and credentials allow it. With `github.enabled`, the
container also holds its own `GH_TOKEN` — see the limitation below.

**GitHub token scope is a documentation contract, not an enforced one.**
Configure the fine-grained PAT (`github.token`, injected as `GH_TOKEN`)
with Contents, Issues, Pull requests and Metadata set to Read. This is a
recommendation, not a permission JailBee assigns. With no other write
credential exposed, the intended publication path is the host's independently
authenticated `gh`: `jailbee pr push` for PRs and the reviewed host-side outbox
(`jailbee issue apply` / `jailbee review apply` / `jailbee outbox apply`) for
queued issues and reviews. **jailbee cannot verify a fine-grained
PAT's effective permissions** — `jailbee doctor` reminds you of the intended
scope but never probes GitHub to check it. A PAT you (or an org policy)
scope wider than read-only lets an agent (or anything running as it) write
to GitHub directly with the container's own `gh`, bypassing the outbox
review gate entirely — the same trust boundary the read-only recommendation
in [`config.md`](config.md#github) and [`git-bridge.md`](git-bridge.md#github-cli-gh-inside-containers)
exists to hold.

An issue manifest may target a repository outside this project
(`github:<owner>/<name>`); the host marks it as foreign in every plan and
dashboard and publishes to it only after a separate confirmation or an
explicit `--foreign`, since the write uses the host's own GitHub identity.
The same holds for a numbered PR manifest published through `jailbee outbox
apply` (or an Outbox entry in a dashboard) that names another repository's
PR; `jailbee review apply` and `jailbee pr push` still refuse it.

**Outside the filesystem boundary.** Host paths that have not been mounted
into the container, including unattached `optional_mounts`, are not exposed
through its filesystem. This is not a separate-kernel guarantee, and shared
mounts and credential groups cross container and repo boundaries deliberately.
Forwarding an SSH or GPG agent socket does not copy its private keys into the
container, but lets processes request authentication or signatures. A private
key placed in a shared directory or mounted explicitly is a readable file,
not protected by socket forwarding.

**Network limits.** In `strict` mode, direct NIC traffic is restricted by
IP-and-port ACL rules, not an exclusive hostname identity: different hosts
can share an allowed IP. Wildcard names are checked by the HTTP(S)
[egress proxy](#egress-proxy), not by the NIC ACL. Host sockets and declared
[port forwards](#port-forwards) are separate access paths outside that ACL.

**Shared across repos, by design.** Credential groups and LiteLLM are
additional cross-repo channels, alongside any shared paths or displays. A
credential group shares the configured agent's login across its members
(Claude is currently the supported account-pool adapter; see
[`credentials`](config.md#credentials)). A process in a member container
can read, use or modify that shared credential.

When LiteLLM is configured and provisioned, its service ACL exposes the
host-wide proxy ports to strict containers across repos. Dev-container
settings include keys for accounts used by served profiles
(`/etc/jailbee/litellm-<account>.key`). These are shared account credentials,
not per-repo authorization boundaries: a holder can spend the account's
subscriptions and API keys through the proxy. Provider tokens are not part
of that dev-container payload, but remain accessible to host administrators;
API secrets also remain in host-side `secrets.env`. See
[LiteLLM security and limitations](litellm.md#security-and-limitations).

The practical shape, then: stay in `strict`, bind read-only what the build
genuinely needs, leave sensitive `optional_mounts` detached, and take a
snapshot (`jailbee snapshot create`) before a long unattended run.

### Lending a container a credential

Some work needs a secret for a while: an API key for one tool, cloud
credentials for one deploy. Writing it into the container leaves it there for
as long as the container lives, readable by every agent run that follows.
Declare it as an [`optional_mounts`](config.md#optional_mounts) entry instead
to control when that host path is attached. Automatic detachment is
best-effort cleanup, not credential revocation.

```yaml
optional_mounts:
  openai:
    host: ~/.config/openai          # contents become readable while mounted
    container: /home/dev/.config/openai
    readonly: true                  # the default, stated for the reader
    description: "OpenAI API key"
    auto_unmount_after: 1h
  aws:
    host: ~/.aws
    container: /home/dev/.aws
    auto_unmount_after: 15m
```

```bash
jailbee mount openai feat-login --for 1h  # record an auto-unmount deadline
jailbee mount aws feat-login --for 30m    # override this mount's default
jailbee unmount aws feat-login           # detach early
```

The **MOUNT** column in `jailbee ls` and the dashboards shows that a container
has one attached and its recorded deadline (`● 12m`, or `● ∞` for one
with no deadline); the details panel names which, and the dashboard's
container menu has Mounts…. Without `--for`, an interactive call asks for
the duration using the configured default; non-interactive calls use that
default. A disabled `mount_auto_revert` disables the configured default,
though an explicit `--for` can still record a deadline.

Detachment requires a registered repo and a working
`jailbee-net-refresh.timer`; it occurs on a successful refresh after expiry,
not at an exact instant. Autostart can defer it, and failed removals are
retried. Verify the mount is gone when the boundary matters. An
[autostart](project-config.md#5-define-autostart-steps) stage can request
`mounts: [aws]` and attempts to remove mounts it added when the stage finishes.
This is stage-scoped cleanup, not a guarantee after a crash or for the lifetime
of a background process; copies made by commands remain accessible.

- **Mount the directory, not the file.** A bind mount of a single file pins
  that file's inode, so a host editor or tool that replaces it atomically —
  most of them — leaves the container reading the old one.
- **Read-only** prevents writes through that mount, not reads, copies or
  connections to sockets inside it. A separate writable mount of the same
  data still permits changes. Re-running `jailbee mount` on an attached mount
  changes only its deadline, not its source or read-only flag.
- **Where to declare it:** in `~/.config/jailbee/global.yaml` for a
  credential that is yours on every repo; in the repo's own config when a tool
  in the repo reads it from a fixed path, as `website/video/render.py` does
  with `~/.config/openai/api-key` in JailBee's own repository. The secret
  itself never goes in either file.

What this buys is a window, not a broker. While the mount is attached,
anything running in the container can read and copy the secret. Strict
network rules constrain destinations, not what data may be sent to an
allowed endpoint; shared sockets and port forwards provide other paths.
Detaching removes the mount, not copies or access already obtained with the
credential. Attach it for the task that needs it, keep `auto_unmount_after`
short, and use provider-side expiry or revocation for a real credential
lifetime limit.

### Git remote & push

After `jailbee new` clones the source repo into the container, it rewrites
`origin` from the RO mount path (`/mnt/host-source`) to the host's selected
upstream URL when one exists, and configures branch tracking. Without an
upstream URL, the source mount remains the remote. Network access,
authentication and repository permissions still determine whether remote
operations succeed. The `--shared` clone semantics survive — objects continue
to read from the mount through `.git/objects/info/alternates`. This mount also
makes the host source tree readable; the clone is not the only repository
data exposed.

The container's remote is always named `origin`, whatever the host calls its
own upstream (see [Which remote is the
upstream?](config.md#which-remote-is-the-upstream)) — the clone is jailbee's
own, so its naming is jailbee's invariant rather than something inherited.

**Strict restricts destinations, not GitHub operations.** GitHub integration
adds `api.github.com:443`; Claude's plugin support also adds GitHub HTTPS
destinations, including `github.com:443`. User allowlist entries can add
other transports. An allowed HTTPS connection can carry a read, a push or an
API write: a write-capable credential can therefore bypass the intended
host-side review workflow even in strict mode. The recommended read-only PAT
is an operator-configured permission boundary, not something JailBee verifies
(see [GitHub CLI inside
containers](git-bridge.md#github-cli-gh-inside-containers)).

Prefer publishing from the host after review (`jailbee pr push <name>`, or
`jailbee git checkout <name>` followed by `git push`). Switching to loose
changes reachability, not credential permissions, and is not required merely
because an operation writes to GitHub.

### Port forwards

A port forward (`host_ports` in config, or an ad hoc `jailbee port
to-container`/`jailbee port to-host`) is a deliberate hole through the
boundary `net strict` otherwise enforces. It works because the traffic
**never traverses the bridge the network ACL is attached to**: each forward
is one Incus `proxy` device, and Incus's forkproxy connects directly into
(or out of) the container's network namespace rather than sending packets
over the NIC. The ACL — applied in `strict` mode only — is deny-by-default
on both egress and ingress (see `src/jailbee/network.py`), so neither
direction of a forward is filtered by it. On the optional `jailbee-work`
network, loose mode attaches an ACL too (`<prefix>-work-loose`, scoped to the
container's own address); a forward bypasses it in the same way.

Both directions matter, and each bypasses a different half of that
default-deny: a `to-container` forward (the `host_ports` case, e.g. the adb
recipe in [project-config.md](project-config.md#talking-to-android-devices-over-adb))
lets the container reach a host service that the egress deny would
otherwise have blocked. A `to-host` forward lets something on the host
reach into the container — traffic the ingress deny would otherwise have
blocked. A `to-host` listener defaults to `127.0.0.1`; choosing a wildcard
or non-loopback `--host-address` can expose the container service to other
machines, subject to host routing and firewall rules. The forward adds no
application authentication. It does **not** itself give the container new
outbound reach: a client initiates the connection to the published service.

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
stage the flag carries the supervisor's pid rather than a bare `"1"`.
If no live process remains behind that pid, the flag no longer blocks an
existing TTL. That is not automatic crash recovery: the stage's network
switch does not itself create a loose deadline. Reversion needs a recorded
deadline and a working refresh service for a registered, loadable repo. A
legacy `"1"` flag is still treated as held. A killed supervisor can therefore
leave a container loose; check its actual mode after an interrupted run.

A detached stage makes a **best-effort mode check** before restoring the
entry mode. It skips restoration if the observed mode differs from the one
the stage set. This is not atomic compare-and-swap: same-mode operator changes
and changes racing the check are not protected. `jailbee net` warns about
an active autostart but proceeds. Foreground stages restore the entry mode
unconditionally, so a change from another shell during the stage can be
overwritten. Avoid concurrent network changes when the resulting boundary
matters, and verify it afterward.

## Egress overrides

`jailbee net egress add` widens a container's, or a repo's, strict-mode
allowlist **without requiring code review**. Unlike tracked `egress_allow`
in `.jailbee/config.yaml`, an override is not automatically visible in the
checkout or its review/CI workflow. It lives in the container's own
`user.jailbee.egress_extra` label (container scope, the default) or in
host-local state (`--repo`, applying to every container of the repo on
this machine).

The mitigation is visibility, not a prompt: `jailbee net egress ls` shows
every applicable entry and where it came from (`config`, `repo-override`,
`container`), and `jailbee net status` lists the overrides for **the repo
whose checkout you run it from**. These are local inspection commands, not
tracked records; remote SSH callers can also inspect them when their command
and repo policy permits. `jailbee net status` is repo-scoped, not a host-wide
audit: to see another repo's overrides, select that repo or run from its
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

**Host control is not exposed by default.** Standard dev containers are not
given access to the host's Incus API. Installing or running JailBee inside a
container does not grant that access. This boundary assumes custom mounts,
remote API credentials or other host-control channels have not exposed it;
never mount the host's Incus socket into an untrusted container. Container
code can also influence an operator or agent using host tools, so review
requests to widen access rather than treating them as trusted instructions.

The `branch_config` escalation gate is separate: it checks privilege-widening
*committed* autostart settings during `jailbee new`. It does not authorize
later host-side `jailbee net egress` changes.

## Egress proxy

Wildcard `egress_allow` entries (`*.example.com`) are enforced by Squid in a
`jailbee-egress-proxy` container rather than by the NIC ACL. See
[`egress_allow`](config.md#wildcards-go-through-the-egress-proxy) for the
user-facing behaviour. The security properties:

- **The proxy is trusted infrastructure.** It sits on `jailbee-loose`, which has
  no egress ACL, so its own upstream traffic is unrestricted. Anyone who can
  run code inside it, or change its Squid configuration, has that upstream
  access. Standard dev containers have no host Incus access and the strict
  service rule exposes only the proxy's port 3128, not its management plane.
- **Identity is the source IP.** Squid decides per client address. The proxy has
  one NIC on each client bridge (`incusbr0`, and `jailbee-work` when it
  exists) so it sees each container's own address; a single leg on another
  network would show every client as the router's masqueraded gateway address
  and make the per-container rules meaningless. The client NICs carry an
  address and no route, so upstream traffic can only leave through `eth0`.
- **Legacy network: repo scope only.** Container-scope wildcards are refused
  there, since a legacy container's address is a DHCP lease that can change.
  Repo-scope rules are rebuilt from the live addresses on each sync, and the
  window between a lease change and the next sync is a limitation of that
  generation. The work network reserves a fixed address per container.
  The legacy network has a second, sharper limit: its NICs carry no
  `security.ipv4_filtering`, and port 3128 is reachable from every strict
  container (see the services rule below). A process with root in one strict
  container can therefore assign itself another container's address and
  inherit that repo's whole Squid allowlist, wildcards and hostname entries
  alike. The address can be a stopped container's (it stays in the fragment
  until a successful refresh removes it) or a live one's, if the impostor wins
  the ARP race. Repos that use wildcards should run `jailbee net migrate`: the work
  network filters spoofed source addresses at the NIC.
- **Always-on on the work network.** With
  [`egress_proxy_always`](config.md#egress_proxy_always) (default), every
  work-network container is a proxy client. Strict sources get their repo's and
  their own entries as rules; loose sources get an unconditional allow,
  matching the loose NIC rule. A source is in exactly one of the two at a time,
  and a successful mode sync moves it. If a loose-to-strict switch's proxy-rule
  sync fails, stale loose access can remain until a later successful sync.
  The refresh timer normally retries every 60 seconds; that is an attempt
  interval, not a bound on exposure. Verify proxy state as well as NIC mode.
- **Blast radius.** A stopped or broken proxy can break HTTP(S) traffic
  directed through it across all always-on containers, not just wildcard
  destinations. `jailbee doctor` reports proxy problems (`jailbee net status`
  also reports a stopped proxy or configured wildcards). `jailbee apply`
  attempts repair, and container start/restart attempts to start a stopped
  proxy, but proxy synchronization failures warn rather than abort startup.
  Existing proxy variables are retained on failure; an initial setup failure
  can leave a new container without them. The strict NIC ACL still applies.
- **Direct traffic remains ACL-filtered in strict mode.** Proxy environment
  variables are advice, normally configured for always-on containers even
  without wildcards. A tool that ignores them connects directly and is
  rejected unless its destination is allowed by the ACL. Loose mode does not
  provide that default-deny boundary.
- **The services rule is host-wide.** Reaching the proxy needs a rule in the
  host-global `jailbee-services` ACL, so every strict container on the host can
  open a connection to port 3128. Squid then denies any source that no repo
  fragment lists (`http_access deny all`), so an unknown client gets a 403.
- **No TLS interception.** HTTPS is tunnelled with `CONNECT`; the match is on
  the requested host name and port. Squid keeps no cache and logs requests to
  `/var/log/squid/access.log` in the proxy container.

## Registry mirror

When a repo uses Docker, its containers pull images through the host-global
`jailbee-registry-mirror` (rpardini/docker-registry-proxy, an nginx-based
MITM caching proxy). The security properties:

- **The mirror is trusted infrastructure.** It sits on `jailbee-loose`, which
  has no egress ACL, so its own upstream traffic is unrestricted. Every strict
  container that uses it has an ACL rule to its port, so what the mirror is
  willing to relay is part of the strict boundary.
- **It relays registries and nothing else.** jailbee replaces rpardini's
  `nginx.conf` with a locked-down copy. Stock rpardini tunnels a `CONNECT` to
  any host and port, and its caching layer fetches whatever `Host` header a
  client sends — either was a way past strict mode. The jailbee copy refuses a
  `CONNECT` to any host outside rpardini's intercept list (its defaults plus
  `extra_registries`), serves only those hosts in the caching layer, and
  allows only `GET` and `HEAD`. There are no blind tunnels at all, not even to
  the CDNs registries redirect blob downloads to: the mirror follows those
  redirects itself. A tunnel to a shared CDN address would otherwise reach any
  other tenant of that CDN by SNI.
- **`jailbee doctor` checks it.** The `registry lockdown` row probes a running
  mirror from the host with a `CONNECT` to `example.com`, a foreign `Host`
  header and a `PATCH`, and turns red if any gets through — which is what a
  mirror provisioned before the lockdown looks like until the next
  `jailbee apply`, `jailbee new` or `jailbee registry up` syncs it.
- **Residual channel: registries are multi-tenant by path.** Code in a
  container can still `GET` an attacker's own repository on an allowed
  registry and carry data in the path (a tag name, say). Where the registry
  shows its owner access logs — cloud registries with audit logging do, Docker
  Hub does not — the attacker can read it back. Neither the mirror nor any
  network filter can tell that request from a legitimate pull; only a
  per-image allowlist would.
- **Alternatives considered.** An egress ACL on the mirror's own NIC would be
  IP-based against rotating, shared CDN addresses, and routing the mirror's
  upstream through the egress proxy needs transparent TLS interception. Both
  would guard only against a bug in the mirror's own config, which the doctor
  probe already detects.

## Limitations

### Work-network generation

The optional `jailbee-work` network is IPv4-only. Loose mode on this bridge
does not provide IPv6 egress, which may be narrower than loose mode on the
legacy unrestricted bridge on hosts where IPv6 previously worked. Returning
to strict removes the source-scoped loose exception and blocks new forbidden
connections, but does not forcibly terminate an already-established
loose-created flow (for example, a long-lived TCP session). Do not treat
strict as immediate session revocation; stop the workload or container when
immediate termination is required. See [work-network activation](installation.md#optional-work-network-activation-and-rollback).

- Incus requires a Linux runtime; [macOS support](macos.md) delegates to a
  Linux VM rather than running native macOS containers.
- Default JetBrains mounts share IDE state across containers; avoid concurrent
  IDEA instances using that shared profile. Chrome and Firefox use separate
  pool slots. Gradle and Maven are pooled by default but can be configured as
  shared caches; hardlinked pool content is not a security boundary. See
  `jailbee pool ls` / `jailbee pool prune` to inspect or clean
  (`jailbee chrome-pool ls/prune` is a deprecated alias scoped to Chrome).
- NVIDIA GPU passthrough requires extra setup (not covered by `jailbee init`)
