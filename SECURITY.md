# Security Policy

## Supported versions

`jailbee` is currently pre-1.0 (`0.x`). Only the **latest released version** is
supported with security fixes. There is no long-term-support branch at this
stage — please upgrade to the latest release before reporting an issue, and
expect fixes to land as new `0.x` releases rather than backports.

| Version | Supported          |
| ------- | ------------------- |
| 0.x (latest) | :white_check_mark: |
| 0.x (older)  | :x:                 |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately by emailing:

**tuomas.airaksinen@gisgro.com**

Include as much detail as you can:

- A description of the issue and its impact
- Steps to reproduce (a minimal `jailbee` invocation or config snippet, if
  applicable)
- The `jailbee` version, Incus version (`incus version`), and host OS

You should receive an acknowledgement within a few business days. We'll work
with you to understand and confirm the issue, and will credit you in the
fix's release notes unless you prefer to stay anonymous.

## Scope: what's sensitive here

`jailbee` orchestrates Incus system containers that get real credentials and
real network access injected into them on behalf of the host user. The
security-relevant surface is smaller than the whole codebase, but worth
calling out explicitly so reports land on the right target:

- **SSH agent / key forwarding into containers.** `jailbee` forwards or provisions
  SSH access inside containers so they can reach git remotes. A bug that
  leaked host SSH key material to a container that shouldn't have it, or
  that let one container read another container's forwarded agent socket,
  is in scope.
- **GPG forwarding/injection.** Similarly, GPG signing material made
  available inside a container for commit signing is sensitive — leaks or
  cross-container access are in scope.
- **GitHub PAT / `gh` token injection.** Containers with `github.enabled`
  get a token injected so `gh` works inside them. Overly broad token scope,
  unintended persistence, or a token becoming visible to the wrong
  container/user is in scope.
- **Network egress ACLs.** `jailbee`'s network modes (`strict`/`loose`/`offline`)
  control what a container can reach on the network. A bypass that lets a
  container reach hosts/ports it should be denied under `strict` mode is in
  scope.
- **Host filesystem exposure.** Mount-mode containers (`jailbee new --mount`)
  bind-mount host paths into a container. A bug that exposed host paths
  beyond what the config/mount options intend is in scope.

Things generally **out of scope**: issues that require an attacker to
already have root on the Incus host or unrestricted shell access inside a
container they were legitimately given (that container's owner already has
that access by design); denial-of-service via resource exhaustion on a host
the reporter fully controls; vulnerabilities in Incus itself (report those
upstream at [linuxcontainers.org](https://linuxcontainers.org/incus/)) or in
third-party dependencies (report those to the dependency's maintainers, and
feel free to also flag it to us so we can bump the version).

If you're unsure whether something is in scope, email us anyway — we'd
rather triage a borderline report than miss a real one.
