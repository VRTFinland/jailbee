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

## Dashboards

**Terminal dashboard**:
The full-screen dashboard that runs in a terminal, locally or over remote SSH.
_Avoid_: TUI dashboard, TUI (for the dashboard)

**Web dashboard**:
The browser frontend of the dashboard, served from the host, reachable from any
device that can log in.
_Avoid_: web UI, web app, web GUI

**Qt dashboard**:
The native desktop dashboard; deprecated in favour of the web dashboard.
_Avoid_: GUI dashboard

**Theme**:
The colours and typography a dashboard frontend is drawn with.

**Display profile**:
How the web dashboard behaves for one kind of screen — normal or e-ink —
independently of its theme.
_Avoid_: e-ink mode, e-ink theme (for the behaviour)

**Prompt channel**:
The private connection over which a command started by a dashboard puts its
questions to that dashboard and receives the answers.
_Avoid_: askpass, prompt hook

## Agent sessions

**Agent session**:
One run of an agent in a container, identified by the agent's own session id.
_Avoid_: conversation, chat

**Transcript**:
The agent's own record of one agent session: the prompts, replies, tool calls
and their results.
_Avoid_: log, chat log, history

**Transcript source**:
The per-agent reader that finds and interprets one agent's transcripts.
_Avoid_: transcript adapter, parser

**Session attribution**:
The recorded link between an agent session and the container it ran in.

## Remote access

**Remote transport**:
A way of reaching the host's JailBee from another machine: remote SSH or the
web dashboard.
_Avoid_: remote mode, channel

**Remote policy**:
What one remote transport may see and do: commands, repository visibility,
mounts, orchestration, network unlock, files and display.
_Avoid_: SSH policy, permissions
