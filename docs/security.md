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
mode, the network — including a push to `origin`.

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
- One IDEA at a time across containers (shared JetBrains profile). Chrome
  runs per-container with its own profile slot, seeded from the most recent
  slot — see `jailbee chrome-pool ls` / `jailbee chrome-pool prune` to inspect or clean.
- NVIDIA GPU passthrough requires extra setup (not covered by `jailbee init`)
