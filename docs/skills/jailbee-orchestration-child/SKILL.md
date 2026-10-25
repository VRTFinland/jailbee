---
name: jailbee-orchestration-child
description: Use when `JAILBEE_ROLE=child` is set or your first prompt says you are a child container of a JailBee orchestrator. Covers reporting back, asking questions and finishing cleanly. Trigger on "you are a child container", "report done to the orchestrator", "ask the orchestrator", "olet lapsikontti", "ilmoita valmis orkestraattorille".
---

# Working as a child container

## You are a worker

An orchestrator (`$JAILBEE_ORCHESTRATOR`, with `JAILBEE_ROLE=child` set) gave
you one task in your first prompt. Do it on your branch. Toward the host you
can do exactly one thing: message your orchestrator, by writing manifests
into `~/.jailbee/orchestration/outbox/`. Never merge into other branches,
push, open PRs, spawn, stop or destroy containers. Those manifests are
refused, and that work belongs to the orchestrator. Schemas are in
[`references/protocol.md`](references/protocol.md).

You may be running a non-default agent or model, chosen by your orchestrator
or the user; do not change it.

General development practice (style, tests, commits) comes from the project's
own instructions.

## Sending a message

Four kinds: `note` (information), `question` (you need an answer), `blocked`
(you cannot continue) and `done` (finished). Use a new id every time; ids are
never reused. Prefer the helper, which writes the manifest atomically and
prints the id:

```bash
jailbee-inbox send message --kind question --text "Which parser do you mean?"
jailbee-inbox send message --kind blocked --text "Tests need network access I do not have."
jailbee-inbox send message --kind done - <<'EOF'
Fixed the parser; tests pass. Everything is committed.
EOF
```

Pass long text on stdin (`-`) or with `--text`. Never write a summary or
any other scratch file inside the repo work tree: an untracked file makes
the tree dirty and your `done` is refused. Use an absolute path under
`/tmp` if you need a file. Note the id the helper prints
(`<type>-<UTC time>-<hex>`): it is the `ref` of the status reply.

Without the helper, write `.<id>.json.tmp` and `mv` it to `<id>.json`:

```json
{"version": 1, "id": "msg-0003", "type": "message", "kind": "question",
 "body": "Which parser do you mean?", "in_reply_to": null}
```

## Done means committed

Before `done`, everything is committed on a branch (not a detached HEAD) and
`git status` is clean. Otherwise the broker replies `status refused`:

- `uncommitted changes: commit everything before reporting done`
- `detached HEAD: check out your branch before reporting done`

Fix it and send a new `done` under a new id. Put a summary in the `done`
body: what changed, tests run, open points:

```json
{"version": 1, "id": "msg-0004", "type": "message", "kind": "done",
 "body": "Fixed the parser; tests pass. Everything is committed.",
 "in_reply_to": null}
```

If your prompted agent exits non-zero (1 to 127), Jailbee reports it to the
orchestrator as a `blocked` message for you; do not send your own duplicate.

After `done`, stop working and wait. The orchestrator may message you or
restart you later with a new prompt.

## Waiting for a reply

After a `question` or `blocked`, run:

```bash
jailbee-inbox wait --timeout 30m   # prints the path of the oldest unread message
```

It exits 0 with a path, 1 on timeout and 2 on a usage error. On timeout,
wait again. Never poll tighter than every 5 min. Treat the next message from
your orchestrator as the answer. Its `in_reply_to` is informational only: the
host gives relayed messages new ids, so it need not equal the id of your
message.

## Checking the inbox while working

At natural checkpoints (a finished step), run `jailbee-inbox ls`. The
orchestrator may refine the task. `jailbee-inbox read <id>` prints a message
and moves it to `inbox/read/`; or `mv` the file there yourself.

## Status messages for you

Every message you send gets a `status` reply whose `ref` is its id, with
state `done` (delivered), `queued` or `refused`. Only `refused` needs action;
it is final, so fix the cause and send a new message under a new id.
`queued` is not a failure: your orchestrator is not running, and the message
is delivered when it starts. Do not resend it.

Two `refused` details for `done` are not a fault in your manifest or your
tree: `could not fetch your branch to the host: ...` and
`could not record the done report: ...`. Retry `done` once under a new id; if
it repeats, send a `blocked` message describing the detail. For any other
unknown `refused` detail do the same: retry once under a new id, then send
`blocked`.
