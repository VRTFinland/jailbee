# Child protocol: manifest and inbox schema

This is the child's subset of the orchestration protocol. The broker on the
host validates every manifest; one that does not match is answered with a
`refused` status.

## Directories

| Path | Direction | Purpose |
| --- | --- | --- |
| `~/.jailbee/orchestration/outbox/` | container to host | messages you write |
| `~/.jailbee/orchestration/inbox/` | host to container | messages and status replies |
| `~/.jailbee/orchestration/inbox/read/` | local | where you move handled messages |

## File rules

- A manifest is `outbox/<id>.json`. The id matches `^[a-z0-9][a-z0-9-]{0,63}$`
  and must equal the file stem and the `"id"` field. Use a new id every time;
  ids are never reused, even after a `refused`.
- Long text may live in a body file next to the manifest, named
  `^[a-z0-9][a-z0-9._-]{0,127}[.]md$`, referenced by `body_file`. Give exactly
  one of `body` or `body_file`. Text is at most 65536 bytes.
- Write atomically: write `.<id>.json.tmp` (and the body file first), then
  `mv` it to `<id>.json`. Hidden files are ignored by the broker.
- The broker deletes the manifest and its body file after acting on it.
- `jailbee-inbox send message ...` does all of this and prints the id.
  Unknown keys in a manifest are refused.

## Envelope

Every manifest has `"version": 1`, `"id"` and `"type"`. A child may use only
the type `message`; any other type is refused ("children may only send
messages").

## Child `message`

No `to`: the message goes to your orchestrator. `kind` is optional (`note`,
`question`, `done`, `blocked`); `in_reply_to` is `null` for
a message you originate. The host re-ids relayed messages, so never rely on
it to match replies.

<!-- kind: child-outbox -->
```json
{"version": 1, "id": "msg-0003", "type": "message", "kind": "done",
 "body": "Fixed the parser; tests pass. Everything is committed.",
 "in_reply_to": null}
```

CLI: `jailbee-inbox send message [--kind K] [--reply-to ID] --wait --text T`
(or `-` for stdin; `--file` only with an absolute path outside the repo).
Never leave summary files in the repo work tree: they make your `done`
refused. Note the id the helper prints (`<type>-<UTC time>-<hex>`).

A `kind: "done"` message is checked on the host before it is relayed. It is
answered `refused` unless the work tree is clean and HEAD is on a branch:

- `uncommitted changes: commit everything before reporting done`
- `detached HEAD: check out your branch before reporting done`

Fix the cause and send a new `done` under a new id. Always use `--wait` for
done, and for any message your work depends on. Exit 0 means accepted
(done/queued/started), 1 refused/failed, 2 usage error, 3 timeout; the default
`--timeout` is 30m. It prints and marks only the matching status read. End
the session only after done returns 0. A timeout needs the original reply,
not another report. The container may be destroyed after done, so commit first.

## Inbox

Files are named `<UTC yyyymmddThhmmssZ>-<seq>-<id>.json`, so a plain sort is
emission order. Host-generated ids look like `h-<8 hex>`. Move a handled
message to `inbox/read/` (or use `jailbee-inbox read <id>`).

A `message` from your orchestrator (`from` is its short name):

<!-- kind: inbox -->
```json
{"version": 1, "id": "h-3fa9c012", "type": "message", "from": "main-orch",
 "kind": "note", "body": "Use the existing parser.", "in_reply_to": "h-77aa01bc"}
```

An `update` message carries optional `imported_ref` and `imported_sha`
(full commit ID), plus a body naming the parent's branch. Commit or stash,
`git merge <imported_ref>`, resolve conflicts, commit, then continue. The
broker never merges into your worktree. Other import fields on messages are
`import_error`, `on_done` (`destroy` or `keep`) and `on_done_reason` (why a
successfully imported child is kept).

Done status details say where the work was imported and whether the child
will be destroyed or kept. Import errors keep the child and are delivered
to the orchestrator for retry. A queued finish waits for parent delivery
before import/cleanup; a vanished parent keeps the child and reports an error.

A `status` reply answers one of your manifests; `ref` is the manifest id and
`state` is `done` (delivered), `queued` (your orchestrator is not running; the
message is delivered when it starts) or `refused`. Only `refused` is final and
needs action. The two `done`-check refusals above are joined by
`could not fetch your branch to the host: ...` and
`could not record the done report: ...`, which are not your manifest's fault:
retry `done` once under a new id, and send `blocked` if it repeats.

<!-- kind: inbox -->
```json
{"version": 1, "id": "h-0a1b2c40", "type": "status", "ref": "msg-0003",
 "state": "refused",
 "detail": "uncommitted changes: commit everything before reporting done"}
```
