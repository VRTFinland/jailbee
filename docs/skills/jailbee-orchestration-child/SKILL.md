---
name: jailbee-orchestration-child
description: Use when `JAILBEE_ROLE=child` is set or your first prompt says you are a child container of a JailBee orchestrator. Covers reporting back, asking questions and finishing cleanly. Trigger on "you are a child container", "report done to the orchestrator", "ask the orchestrator", "olet lapsikontti", "ilmoita valmis orkestraattorille".
---

# Working as a child container

## You are a worker

An orchestrator (`$JAILBEE_ORCHESTRATOR`, with `JAILBEE_ROLE=child` set) gave
you one task in your first prompt. Do it on your branch. Toward the host you
can message your orchestrator or give up your own orchestration rights, by writing manifests
into `~/.jailbee/orchestration/outbox/`. Never merge into other branches,
push, open PRs, spawn, stop or destroy containers. Those manifests are
refused, and that work belongs to the orchestrator. Schemas are in
[`references/protocol.md`](references/protocol.md).

You may be running a non-default agent or model, chosen by your orchestrator
or the user; do not change it.

General development practice (style, tests, commits) comes from the project's
own instructions.

## Give up access

When access is no longer needed, use `jailbee-inbox send rights
--revoke-loose --revoke-mount aws --revoke-egress pypi.org --wait` (include
only items to give up). Omit `--child` or name yourself. Never include a
`grant` block: children may only revoke their own rights. Rights a human
added are left untouched; the reply says so. Your orchestrator receives a
note describing the rights you gave up. Ask for more access with a
`question` message as before, never by granting it yourself.

## Sending a message

Four kinds: `note` (information), `question` (you need an answer), `blocked`
(you cannot continue) and `done` (finished). Use a new id every time; ids are
never reused. Prefer the helper, which writes the manifest atomically and
prints the id:

```bash
jailbee-inbox send message --kind question --text "Which parser do you mean?"
jailbee-inbox send message --kind blocked --text "Tests need network access I do not have."
jailbee-inbox send message --kind done --wait - <<'EOF'
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

Always send `done` with `--wait` and end the session only after exit 0
(`done` or `queued`). The reply says whether work was imported and whether
the container will be destroyed or kept. On exit 1, fix the refusal and
resend under a new id. Exit 3 is a timeout, not a refusal: wait for the
original status; do not duplicate the report. After accepted `done`, stop
working: the container may be destroyed. Commit everything before reporting.
Use the same `--wait` rule for any message your work depends on.

## Updates from your orchestrator

An `update` message carries `imported_ref` and `imported_sha`. Commit or
stash current work, run `git merge <imported_ref>`, resolve conflicts,
commit, then continue. The broker only transports objects and refs; it
never merges into your working tree. The message body names the branch.

## Waiting for a reply

After a `question` or `blocked`, run:

```bash
jailbee-inbox wait --timeout 30m   # prints the path of the oldest unread message
```

It exits 0 with a path, 1 on timeout and 2 on a usage error. On timeout,
wait again. After `wait` returns, run `jailbee-inbox ls` and handle **every**
unread message: replies to several manifests arrive as a burst. It returns at
once while any message is unread, so `jailbee-inbox read <id>` each one before
waiting again. Never poll tighter than every 5 min. Treat the next message from
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
`queued` is not a failure: your orchestrator is stopped or has older parked
items; the report is imported before delivery when it can receive it. Do not
resend it. `send --wait [--timeout DUR]` (default 30m) prints the matching
status and moves only that status to `inbox/read/`: 0 for done/queued/started,
1 for refused/failed, 2 for usage error, 3 for timeout.

Two `refused` details for `done` are not a fault in your manifest or your
tree: `could not fetch your branch to the host: ...` and
`could not record the done report: ...`. Retry `done` once under a new id; if
it repeats, send a `blocked` message describing the detail. For any other
unknown `refused` detail do the same: retry once under a new id, then send
`blocked`.
