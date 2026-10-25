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

Every manifest has `"version": 1`, `"id"` and `"type"`. The type is one of
`spawn`, `message`, `import`, `restart`, `stop`, `destroy`.

## Orchestrator manifests

### `spawn`

Create a child container. `kind` is `fork` (the child starts from your
committed HEAD; a dirty tree is refused) or `new` (a fresh checkout from
`base`, which is only valid with `kind: "new"`). `branch` is the child's
branch. `rights` must be within `grantable` (see below). `note` is optional.

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
Omit it unless the user asked for a specific agent, profile or model (or their
instructions name one); omitting starts the repo's default agent.

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
the default). Details: `agent `claude` is not enabled in this repo`; `agent
`claude` has no prompt_args, so it cannot take a prompt`;
`agents.claude.command must start with `claude` or `claude-jb` to take a
runner (it is `...`)`; `claude-jb needs LiteLLM enabled in this repo`;
`LiteLLM profile `P` is not served (configured: ...; run `jailbee litellm
up`)`; `LiteLLM profile `P` has no `M` tier`.

CLI: `jailbee-inbox send spawn --branch fix/issue-12 [--new [--base main]]
[--mount M]... [--loose] [--egress H]... [--note T] [--agent A [--profile P]]
[--model M] -` with the prompt on
stdin (or `--text T`; `--file` only with an absolute path outside the repo).
`jailbee-inbox runners [--json]` lists the runners you may name and what
omitting one starts: `default: omit runner (<agent>)`, then one line per
`claude` and per `claude-jb -P <profile>` with its models.
Never leave prompt files in the repo work tree: untracked files make a
`fork` spawn and an `import` refused.

The short name is derived from the branch: lowercased, characters outside
`[a-z0-9-]` become `-`, runs of `-` collapse. A taken name is refused
("already exists").

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

### `restart`

Start a stopped child again with a new prompt (`prompt` or `prompt_file`).
Status: `done` ("child `x` restarted") when it runs at once; `queued` when
no slot is free, then `started` when it is later started.

`runner` is optional. Absent keeps the child's previous runner; an explicit
`"runner": null` clears it (the default agent again); an object replaces it.
The refusals are the spawn ones above.

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

CLI: `jailbee-inbox send restart --child C [--agent A [--profile P]]
[--model M] | --default-runner` with the prompt on stdin or `--text`.

### `stop`

Stop a running child, freeing its slot.

<!-- kind: outbox -->
```json
{"version": 1, "id": "stop-fix-12", "type": "stop", "child": "fix-issue-12"}
```

### `destroy`

Delete a child (stopping it first is the recommended order). Refused if it
has commits you have not imported, unless `force` is true: its current HEAD
must equal the last import. For a stopped child the HEAD recorded when the
broker stopped it counts; one stopped any other way is refused until
restarted.

<!-- kind: outbox -->
```json
{"version": 1, "id": "destroy-fix-12", "type": "destroy",
 "child": "fix-issue-12", "force": false}
```

### Grant operation per type

| Type | Needs grant operation |
| --- | --- |
| `spawn` | `spawn` (always granted) |
| `message` | `message` (always granted) |
| `import` | `import` |
| `restart` | `restart` |
| `stop` | `stop` |
| `destroy` | `destroy` |

A type whose operation is missing from `grant.json` is refused. The broker
writes `grant.json` after its first round and the file may remain after a
revoke.

## Child manifests

A child may write only `message` manifests, with no `to` (they go to its
orchestrator).

<!-- kind: child-outbox -->
```json
{"version": 1, "id": "msg-0003", "type": "message", "kind": "done",
 "body": "Fixed the parser; tests pass. Everything is committed.",
 "in_reply_to": null}
```

A `kind: "done"` message also makes the host record the child's HEAD, which
is what a later `import` and the destroy guard use.

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

A `status` reply answers one manifest; `ref` is the manifest id and `state`
is one of four values.

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
 "operations": ["import", "message", "restart", "spawn", "stop"],
 "grantable": {"mounts": [], "loose": false, "egress": []}}
```

- `max_running_children`: how many children may run at once. A spawn beyond
  it is `queued` until a slot frees.
- `operations`: the manifest types you may use. `spawn` and `message` are
  always present.
- `grantable`: the most you may hand to a child.

The repo may cap the grant further; what you read is what applies.

## Rights rule

A spawn's `rights` must be a subset of `grantable`: every mount by name is
in `grantable.mounts`, `loose` is true only if `grantable.loose` is, and each
egress host is covered by an entry in `grantable.egress`. Anything else is
`refused`.
