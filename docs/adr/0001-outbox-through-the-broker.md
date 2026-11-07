# Outbox through the broker

Pending GitHub proposals used to be files inside each container, read and
changed by the host through `incus exec`. That cost several round trips per
container and made `jb outbox` take seconds. We moved them to the host broker:
every container (not only orchestrators and children) has a broker socket,
container commands stage proposals over it, and all outbox state lives in the
host-side `broker.db`, next to the orchestration inbox. Host commands read and
write that store directly. See #89.

## Considered options

- **Keep files, batch the reads** (one exec per container for both kinds and
  identity). Cheaper to build, but keeps two transports and leaves PR publish
  progress inside the container, where the agent can edit it.
- **Container-side spool when the broker is down.** Rejected: it is the file
  transport again. Staging exits 4 when the broker is unreachable, like
  `jailbee inbox`.

## Consequences

- The broker is a core component: `jb setup` enables it and `jb doctor` treats
  a stopped broker as a failure.
- Proposals are bound to the Incus instance uuid, so a fork or snapshot
  restore no longer carries pending proposals.
- No migration: proposals staged under the file layout are neither imported
  nor read. Publish or drop them before upgrading.
