# Orchestration protocol: manifest and inbox schema

This file is the authoritative schema for the orchestration outbox and inbox.
The broker on the host validates every manifest against it; a manifest that
does not match is answered with a `refused` status. All names are **short
names** (the branch-level name, never one prefixed with the repo).

## Directories

| Path | Direction | Purpose |
| --- | --- | --- |
| `~/.jailbee/orchestration/outbox/` | container to host | manifests you write |
| `~/.jailbee/orchestration/inbox/` | host to container | messages and status replies |
| `~/.jailbee/orchestration/inbox/read/` | local | where you move handled messages |
| `~/.jailbee/orchestration/grant.json` | host to container | your grant (read-only) |

## File rules

- A manifest is `outbox/<id>.json`. The id matches `^[a-z0-9][a-z0-9-]{0,63}$`
  and must equal the file stem and the `"id"` field. Use a new id every time;
  ids are never reused, even after a `refused`.
- Long text (a prompt or message body) may live in a body file next to the
  manifest, named `^[a-z0-9][a-z0-9._-]{0,127}[.]md$`, referenced by
  `prompt_file` / `body_file`. Give exactly one of the inline field
  (`prompt` / `body`) or the file field. Text is at most 65536 bytes.
- Write atomically: write `.<id>.json.tmp` (and the body file first), then
  `mv` it to `<id>.json`. Hidden files are ignored by the broker.
- The broker deletes the manifest and its body file after acting on it.
- `jailbee-inbox send <type> ...` does all of this and prints the id.
  Unknown keys in a manifest are refused.

## Envelope

Every manifest has `"version"`, `"id"` and `"type"`. Version is `1`, except
spawn also supports version `2`. The type is one of
`spawn`, `message`, `import`, `update`, `restart`, `stop`, `destroy`, `rights`.

## Orchestrator manifests

### `spawn`

`spawn` and `restart` accept optional `autostart_profile: "NAME"`
(helper: `--autostart-profile NAME`), independent of the runner's `profile`.
The broker validates against the host repo's `autostart.profiles`; an unknown
name is refused with ``autostart profile `NAME` is not defined``. Spawn uses
the named profile for creation. Restart stores it before booting and runs
missing one-shot setup before the profile's `on_start` stages. Omission keeps
the normal default on spawn and the child's stored selection on restart.

Spawn requests support version 2; legacy version 1 remains valid for fork/new
without `ref` or `pr`. Other messages, inbox, grant and finish envelopes
remain version 1.

Create a child container. `kind` is `fork` (the child starts from your
committed HEAD; a dirty tree is refused) or `new` (a fresh checkout from
`base`, which is only valid with `kind: "new"`). `branch` is the child's
branch. `rights` must be within `grantable` (see below). `note` is optional.
`on_done` is optional: `"destroy"` (default) or `"keep"`. If the grant lacks
`destroy`, omission defaults to `keep` and explicit `destroy` is refused.
CLI: `--on-done destroy|keep`. Version 2 also permits `kind: "fork"`
with `ref` (a full `refs/` name or 40-hex commit), or `kind: "pr"` with
a positive integer `pr`. `base` is only for `new`; `ref` is only for
`fork`. Use `refs/heads/<name>` for a local branch, not a bare name.
Explicit-ref forks do not inherit PR bindings. A PR child's own outbox is
lost when destroyed; use `--on-done keep` to preserve it.

<!-- kind: outbox -->
```json
{"version": 1, "id": "spawn-fix-12", "type": "spawn", "kind": "fork",
 "branch": "fix/issue-12", "prompt_file": "spawn-fix-12.md",
 "rights": {"mounts": [], "loose": false, "egress": []}, "note": "GitHub #12"}
```

<!-- kind: outbox -->
```json
{"version": 1, "id": "spawn-docs-1", "type": "spawn", "kind": "new",
 "base": "main", "branch": "docs/intro", "prompt": "Write the intro page.",
 "rights": {"mounts": [], "loose": false, "egress": []}}
```

Optional `runner` (spawn and restart): `{"agent": "claude" | "claude-jb",
"profile": <LiteLLM profile, claude-jb only>, "model": "fable" | "opus" |
"sonnet" | "haiku"}`; only `agent` is required, unknown keys are refused.
On spawn, omitting it uses the effective default runner in `grant.json`, or the
repo's default agent if none is configured. Choose a cheaper or stronger tier
when appropriate for the task, following the user's model guidance; when in
doubt use the default. Keep the default agent and profile unless guidance says
otherwise. On restart, omission keeps the child's runner; `null` selects the
current effective default, or clears the runner when no default is configured.

<!-- kind: outbox -->
```json
{"version": 1, "id": "spawn-fix-13", "type": "spawn", "kind": "fork",
 "branch": "fix/issue-13", "prompt": "Fix issue 13.",
 "rights": {"mounts": [], "loose": false, "egress": []},
 "runner": {"agent": "claude-jb", "profile": "grok", "model": "sonnet"}}
```

<!-- kind: outbox -->
```json
{"version": 1, "id": "spawn-fix-14", "type": "spawn", "kind": "fork",
 "branch": "fix/issue-14", "prompt": "Fix issue 14.",
 "rights": {"mounts": [], "loose": false, "egress": []},
 "runner": {"agent": "claude", "model": "sonnet"}}
```

A spawn the broker cannot run with that runner is `refused` (never run with
the default). Details:

- ``agent `claude` is not enabled in this repo``
- ``agent `claude` has no prompt_args, so it cannot take a prompt``
- ``agents.claude.command must start with `claude` or `claude-jb` to take a runner (it is `...`)``
- ``claude-jb needs LiteLLM enabled in this repo``
- ``LiteLLM profile `P` is not served (configured: ...; run `jailbee litellm up`)``
- ``LiteLLM profile `P` has no `M` tier``

CLI: `jailbee-inbox send spawn [--branch fix/issue-12]
[--new [--base main] | --ref REF | --pr N] [--mount M]... [--loose] [--egress H]... [--note T] [--agent A [--profile P] [--model M]] -` with the prompt on
stdin (or `--text T`; `--file` only with an absolute path outside the repo).
`--profile` and `--model` need `--agent`, and `--profile` needs
`--agent claude-jb`. `jailbee-inbox runners [--json]` lists the runners you may name and what
omitting one starts: `default: omit runner (<agent>)`, then one line per
`claude` and per `claude-jb -P <profile>` with its models. The default agent is named only when
known; otherwise it reads `unknown` and you simply omit `runner`.
Never leave prompt files in the repo work tree: untracked files make a
`fork` spawn refused (plain import does not require a clean target).

The short name is derived from the branch: lowercased, characters outside
`[a-z0-9-]` become `-`, runs of `-` collapse. A taken name is refused
("already exists").

### One PR per child

Spawn `--new --base main` from the upstream base, not the orchestrator's
HEAD. On done, stage a source manifest with `source` = `imported_ref`
and `source_sha` = `imported_sha`, without merging. For rework use `--ref`
or `--pr N`, then import and stage `pr: N` with the new source. See the
[source examples](../../jailbee-pr-review/references/manifest-schema.md#source-publication-examples).

### `message`

Send a message to a child. `to` is the child's short name; `body` or
`body_file`; `kind` is optional (`note`, `question`, `done`, `blocked`);
`in_reply_to` is the `h-<hex>` id of the inbox message you answer (the
host re-ids every relayed message, so use the id you read in the inbox).
A message to a stopped child is answered `queued` and delivered when it
runs again.

<!-- kind: outbox -->
```json
{"version": 1, "id": "reply-0004", "type": "message", "to": "fix-issue-12",
 "body": "Use the existing parser, do not add a new one.", "kind": "note",
 "in_reply_to": "h-3fa9c012"}
```

CLI: `jailbee-inbox send message --to C [--kind K] [--reply-to ID] --text T`.

### `import`

Fetch a child's committed work into your repo as
`refs/jailbee/from/<child>/<branch>`. Nothing is merged.

<!-- kind: outbox -->
```json
{"version": 1, "id": "import-fix-12", "type": "import", "child": "fix-issue-12"}
```

### `update`

Transport the orchestrator's current branch into a running child at
`refs/jailbee/from/<orchestrator>/<branch>`, without touching its index or
existing worktree. The child receives an `update` message carrying
`imported_ref` and `imported_sha`, and merges when convenient. The `import`
grant operation permits update; no new grant is needed. Detached parent
HEAD and stopped child are refused.

<!-- kind: outbox -->
```json
{"version": 1, "id": "update-fix-12", "type": "update", "child": "fix-issue-12"}
```

CLI: `jailbee-inbox send update --child C --wait`.

### `restart`

Start a stopped child again with a new prompt (`prompt` or `prompt_file`).
Status: `done` ("child `x` restarted") for a synchronous restart; repositories
with autostart profiles return `started` after handing boot and setup to a
background worker (see the log in the detail). `queued` means no slot is free,
then `started` when launched later. A later restart/setup failure is recorded in the background job and log
and also arrives as a second status, `failed`, for the same ref (``restart of child
`<child>` failed: <reason>``).

`runner` is optional. Absent keeps the child's previous runner; an explicit
`"runner": null` selects the current effective default (clearing the label if
none is configured); an object replaces it. Queued requests retain the runner
resolved when accepted.
The refusals are the spawn ones above. `on_done: "destroy" | "keep"`
changes the policy; absent or null keeps it. Explicit destroy requires the
`destroy` operation. Restart clears any pending cleanup and stop-time dirty
marker.

<!-- kind: outbox -->
```json
{"version": 1, "id": "restart-fix-12-b", "type": "restart",
 "child": "fix-issue-12", "prompt_file": "restart-fix-12-b.md"}
```

<!-- kind: outbox -->
```json
{"version": 1, "id": "restart-fix-12-c", "type": "restart",
 "child": "fix-issue-12", "prompt": "Try again.", "runner": null}
```

CLI: `jailbee-inbox send restart --child C [--agent A [--profile P] [--model M] | --default-runner]` with the prompt on stdin or `--text`.

### `stop`

Stop a running child, freeing its slot.

<!-- kind: outbox -->
```json
{"version": 1, "id": "stop-fix-12", "type": "stop", "child": "fix-issue-12"}
```

### `destroy`

Delete a running or stopped child; a done `keep` child needs no prior stop.
Refused for uncommitted changes, unreadable status or commits never imported,
unless `force` is true: its current HEAD must equal the last import. For a stopped child the HEAD recorded when the
broker stopped it counts; one stopped any other way is refused until
restarted.

<!-- kind: outbox -->
```json
{"version": 1, "id": "destroy-fix-12", "type": "destroy",
 "child": "fix-issue-12", "force": false}
```

### `rights`

Grant or revoke an existing child's rights. Orchestrators must name their own
child. Children must omit `child` or name themselves, and omit `grant` entirely;
otherwise: `children may only revoke their own rights`.

<!-- kind: outbox -->
```json
{"version": 1, "type": "rights", "id": "rights-1", "child": "fix-12",
 "grant": {"loose": true, "mounts": ["aws"], "egress": ["pypi.org"], "for": "5m"},
 "revoke": {"loose": false, "mounts": [], "egress": ["old.example.com"]}}
```

Both blocks are optional; at least one item overall is required. An item
cannot be in both blocks. `grant.for` is mandatory: a TTL (max 24h) or `never`,
covering all granted items. Grant items pass the same current grant/repo cap
checks as spawn. Effective loose permission also permits egress covered by
the repo cap, or any entry when no repo egress cap is set. Missing operation:
``operation `rights` is not in this container's grant``. Revoke-only does not
need the operation, but the orchestrator must still hold a grant.

Revoke only orchestration-owned rights (spawn, permanent or timed grants).
Hand-added rights are left alone, with `<item> was not given by orchestration;
left as is`. Timed grants never shorten a permanent right or later expiry;
`never` clears expiry. Revoke before grant; failure rolls back best effort and
replies `failed`; validation refusal changes nothing. Success replies `done`
with details. Timed rights expire on the host's 60-second timer. Every applied
manifest is journalled. A child's revoke sends its parent a `note`, e.g.
`fix-12 gave up: loose`. Stopped children take network changes on start and
mounts as devices. Wildcard egress requires the work network.

CLI: `jailbee-inbox send rights --child C [--loose] [--mount K]...
[--egress H]... --for T [--revoke-loose] [--revoke-mount K]...
[--revoke-egress H]... [--wait]`. Revoke-only omits `--for`.

### Grant operation per type

| Type | Needs grant operation |
| --- | --- |
| `spawn` | `spawn` (always granted) |
| `message` | `message` (always granted) |
| `import` | `import` |
| `update` | `import` |
| `restart` | `restart` |
| `stop` | `stop` |
| `destroy` | `destroy` |
| `rights` | `rights` when granting; revoke-only needs an active grant, no operation |

A type whose operation is missing from `grant.json` is refused. The broker
writes `grant.json` after its first round and the file may remain after a
revoke.

## Child manifests

A child may write `message` manifests, with no `to` (they go to its
orchestrator), and revoke-only `rights` for itself.

<!-- kind: child-outbox -->
```json
{"version": 1, "id": "msg-0003", "type": "message", "kind": "done",
 "body": "Fixed the parser; tests pass. Everything is committed.",
 "in_reply_to": null}
```

A `kind: "done"` message records HEAD, imports before delivery, then schedules
cleanup according to `on_done`. A stopped or busy orchestrator gets an
ordered pending finish; a vanished orchestrator refuses the report and
keeps the child. Import failure still delivers done with `import_error` and
keeps the child. Report with `send message --kind done --wait`.

## Inbox

Files are named `<UTC yyyymmddThhmmssZ>-<seq>-<id>.json`, so a plain sort is
emission order. Host-generated ids look like `h-<8 hex>`. Move a handled
message to `inbox/read/` (or use `jailbee-inbox read <id>`).

A `message` from a child:

<!-- kind: inbox -->
```json
{"version": 1, "id": "h-3fa9c012", "type": "message", "from": "fix-issue-12",
 "kind": "question", "body": "Which parser do you mean?", "in_reply_to": null}
```

Messages may carry optional `imported_ref`, `imported_sha`, `import_error`,
`on_done` (`destroy` or `keep`) and `on_done_reason`. Successful done reports
carry the ref and full SHA; failed imports carry the error and `on_done: keep`.
`import_error` is only ever a real import failure: when the import succeeded
but the child is kept anyway (the grant lost `destroy`, or the destroy marker
could not be set), the report has `on_done: keep` and `on_done_reason` says
why, and no re-import is needed. Update
messages carry the parent's ref and SHA. `ls` marks imported/failed reports;
`read` shows all fields. Merge `imported_ref` in the merged-fork workflow, or stage a source manifest without merging for one PR per child; retry `import`
after fixing `import_error`.

`send --wait [--timeout DUR]` consumes only the status for that manifest:
exit 0 on done/queued/started, 1 on refused/failed, 2 on usage errors, 3 on
timeout (default 30m). Recommend it for spawn, update and destroy.

A `status` reply answers one manifest; `ref` is the manifest id and `state`
is one of five values: `queued`, `started`, `done`, `failed`, `refused`.

<!-- kind: inbox -->
```json
{"version": 1, "id": "h-0a1b2c3d", "type": "status", "ref": "spawn-fix-12",
 "state": "queued", "detail": "queued at position 1"}
```

<!-- kind: inbox -->
```json
{"version": 1, "id": "h-0a1b2c3e", "type": "status", "ref": "spawn-fix-12",
 "state": "started", "detail": "creation of child `fix-issue-12` started"}
```

<!-- kind: inbox -->
```json
{"version": 1, "id": "h-0a1b2c3f", "type": "status", "ref": "import-fix-12",
 "state": "done",
 "detail": "imported as refs/jailbee/from/fix-issue-12/fix/issue-12 at 1a2b3c4d5e6f"}
```

A spawn's final status is `done` or `failed`:

<!-- kind: inbox -->
```json
{"version": 1, "id": "h-0a1b2c40", "type": "status", "ref": "spawn-fix-12",
 "state": "done", "detail": "child `fix-issue-12` is ready"}
```

<!-- kind: inbox -->
```json
{"version": 1, "id": "h-0a1b2c41", "type": "status", "ref": "spawn-fix-12",
 "state": "failed",
 "detail": "creation of child `fix-issue-12` failed: the worker stopped during deps — the child was removed"}
```

`failed` is final and also reports runtime rights application failures; a restart reports `done` or
`started`, then `failed` for the same ref if its boot or setup fails later. Other details: ``creation of child `<child>`
stopped unexpectedly during <phase>``, ``creation of child `<child>` ended
without a container``; a missing error reads `see the worker log`.

A `queued` status applies to `restart` as well as `spawn`, and to a
`message` for a stopped child. Other `done` details: `delivered to <child>`,
`child \`<child>\` stopped`, `child \`<child>\` destroyed`. `refused` is
final. The `ref` of a status is the id of your manifest; note the id a
helper prints (`<type>-<UTC time>-<hex>`). Asking about an unknown child is
refused ("`x` is not a child of this orchestrator"); after a revoked grant,
"orchestration grant revoked".

## `grant.json`

<!-- kind: grant -->
```json
{"version": 1, "max_running_children": 2,
 "operations": ["destroy", "import", "message", "restart", "rights", "spawn", "stop"],
 "grantable": {"mounts": [], "loose": false, "egress": []},
 "default_runner": {"agent": "claude", "model": "sonnet"},
 "default_runner_source": "repo"}
```

- `default_runner`: the effective runner for an omitted spawn runner, or null
  when no runner default is configured. It is a preference, not a right.
- `default_runner_source`: `container`, `local`, `repo`, or `global`, or null when none
  is configured.
- `max_running_children`: how many children may run at once. A spawn beyond
  it is `queued` until a slot frees. During creation, a child counts from
  the `started` reply until it is running and its create has finished, or
  its create has failed.
- `operations`: the manifest types you may use. `spawn` and `message` are
  always present.
- `grantable`: the most you may hand to a child.

The repo may cap the grant further; what you read is what applies.

## Rights rule

A spawn's `rights` must be a subset of `grantable`: every mount by name is
in `grantable.mounts`, `loose` is true only if `grantable.loose` is, and each
egress host is covered by an entry in `grantable.egress`, or effective loose
permission allows it within the repo egress cap (if set). This rule applies
to spawn and runtime grants. Anything else is
`refused`.
