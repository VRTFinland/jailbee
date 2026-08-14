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

## Limitations

- Linux host only
- One IDEA at a time across containers (shared JetBrains profile). Chrome
  runs per-container with its own profile slot, seeded from the most recent
  slot — see `jailbee chrome-pool ls` / `jailbee chrome-pool prune` to inspect or clean.
- NVIDIA GPU passthrough requires extra setup (not covered by `jailbee init`)
