# JailBee

Isolated per-branch development containers, the agents working in them, and
the host-side machinery that lets those agents reach GitHub only through a
human or a granted orchestrator.

## Outbox

**Outbox**:
The per-container queue of GitHub actions an agent has proposed and nobody has
published yet.
_Avoid_: queue, pending writes

**Proposal**:
One staged manifest in the outbox: a named, ordered set of issue or PR actions
that is published or dropped as a whole.
_Avoid_: manifest (for the queued item), request

**Draft**:
A proposal still being assembled from several commands; it enters the outbox
only when submitted.
_Avoid_: batch (for the stored object)

**Journal**:
The host's record of each action's publication outcome for one proposal:
prepared, applied or uncertain.
_Avoid_: progress file, receipt log

**Sent record**:
A durable host-side entry for one action that reached GitHub, kept after its
proposal is gone.
_Avoid_: applied log, history entry

## Broker roles

**Plain container**:
A container with a broker socket but no orchestration role; it may use the
outbox but not the inbox.
_Avoid_: ordinary container, normal container

**Orchestrator**:
A container holding an orchestration grant that may create and direct child
containers.

**Child**:
A container created by an orchestrator; it may not stage outbox proposals.
_Avoid_: worker, sub-container
