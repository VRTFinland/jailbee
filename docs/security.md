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
into a container — those are ordinary files. That is why running agentic tools with their in-process sandbox
disabled (e.g. Claude Code's `dangerouslyDisableSandbox`) is reasonable
*inside* a `jailbee` container in `strict` network mode, provided optional
mounts (e.g. `.aws`) stay detached and sensitive data isn't copied in.

### Git remote & push

After `jailbee new` clones the source repo into the container, `origin` is
rewritten from the RO mount path (`/mnt/host-source`) to the host
repo's real upstream URL, and branch tracking is set explicitly so
`git push`/`fetch`/`gh` work without `-u` once the network ACL allows
them. The `--shared` clone semantics survive — objects continue to read
from the mount through `.git/objects/info/alternates`.

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
