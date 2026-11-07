---
name: jailbee-orchestration-child
description: Use when `JAILBEE_ROLE=child` is set or your first prompt says you are a child container of a JailBee orchestrator. Covers reporting back, asking questions and finishing cleanly. Trigger on "you are a child container", "report done to the orchestrator", "ask the orchestrator", "olet lapsikontti", "ilmoita valmis orkestraattorille".
---

# Working as a child container

**First:** If `JAILBEE_ROLE=child`, the role marker identifies you as a child,
or a staging command refuses you as a child, do not stage GitHub writes. Send
the orchestrator the exact repository, target, title, body and action content
with `jailbee inbox send message --text "..."`; use `--kind question` when you
need a result. Do not write GitHub outbox files or directories by hand.

When the inbox CLI works, route orchestration requests and blockers to your
orchestrator. If a `jailbee inbox` command is missing or reports the broker
unavailable, tell the human; never hand-roll the protocol.

## You are a worker

An orchestrator (`$JAILBEE_ORCHESTRATOR`, with `JAILBEE_ROLE=child` set) gave
you one task in your first prompt. Work on your branch and use `jailbee inbox`
for all orchestration actions. You must never read, write or move files under `~/.jailbee/orchestration/`.
Never merge into other branches, push, open PRs,
spawn, stop or destroy containers; those actions belong to the orchestrator.

An `adopted` inbox message can make you a child mid-session; from then on,
follow this skill. `JAILBEE_ROLE=child` is set only on the next agent launch,
not retroactively in your current session. Adoption does not automatically
relaunch your agent.

You may be running a non-default agent or model, chosen by your orchestrator
or the user; do not change it.

General development practice (style, tests, commits) comes from the project's
own instructions.

## Your own subagents

**If you are a subagent** dispatched by the child's agent (an implementer,
reviewer or any other helper), this skill's inbox protocol is not yours, even
though `JAILBEE_ROLE=child` is set: do not use `jailbee inbox` at all. Return
your result, question or blocker to the agent that dispatched you, through
your normal return value.

As the child's top-level agent, you are the controller of your container.
Where the project's instructions reserve work for "the controller" (long
builds or test runs, for example), that means you, never the orchestrator: do
it yourself. When you dispatch subagents, tell them not to message the
orchestrator and to report back to you instead. Only you talk to the
orchestrator.

## Stay reachable

At session start, run `jailbee inbox wait --timeout none` in the background;
with Claude, use Bash `run_in_background`. When it exits 0, run
`jailbee inbox ls`, handle and `jailbee inbox read <id>` every unread message,
including status replies, then re-arm the waiter. Exit 2 means a waiter already
runs: do not start another, and never run a foreground ordinary `wait` alongside
it. `send --wait` is safe alongside the waiter. After an accepted `done` in a
kept child, keep the waiter armed between tasks. If background commands are not
available, run `jailbee inbox wait --timeout 30m` in a loop and re-run it on
exit 1. Exit 4 means the host broker is not reachable: tell the human.

## Give up access

When access is no longer needed, use `jailbee inbox send rights
--revoke-loose --revoke-mount aws --revoke-egress pypi.org --wait` (include
only items to give up). Omit `--child` or name yourself. Never include a
`grant` block: children may only revoke their own rights. Rights a human
added are left untouched; the reply says so. Your orchestrator receives a
note describing the rights you gave up. Ask for more access with a
`question` message as before, never by granting it yourself.

## Sending a message

Four kinds: `note` (information), `question` (you need an answer), `blocked`
(you cannot continue) and `done` (finished).

Message your orchestrator only when it has to act or decide: a `question`,
`blocked`, a change of scope or plan it should know about, and `done`. **Do
not send progress notes** ("tests running", "task 3 started", "review
underway"): every message wakes the orchestrator and costs it a full turn.
The broker already reports idle and quiet children, and your final `done`
carries the summary. Answer a status request from the orchestrator with one
short note.

Use a new id every time; ids are never reused. Send messages through the helper. The wire format includes `"kind": "done"`:

```bash
jailbee inbox send message --kind question --text "Which parser do you mean?"
jailbee inbox send message --kind blocked --text "Tests need network access I do not have."
jailbee inbox send message --kind done --wait - <<'EOF'
Fixed the parser; tests pass. Everything is committed.
EOF
```

Pass long text on stdin (`-`) or with `--text`. Never write a summary or
any other scratch file inside the repo work tree: an untracked file makes
the tree dirty and your `done` is refused. Use an absolute path under
`/tmp` if you need a file. The helper prints the id (`<type>-<UTC time>-<hex>`)
used as the status reply's `ref`.

## Done means committed

Before `done`, everything is committed on a branch (not a detached HEAD) and
`git status` is clean. Otherwise the broker replies `status refused`:

- `uncommitted changes: commit everything before reporting done`
- `detached HEAD: check out your branch before reporting done`

Fix it and send a new `done` under a new id. Put a summary in the `done`
body: what changed, tests run, open points.

If your prompted agent ends without an accepted `done`, Jailbee reports it
for you: startup failure is `failed`, later death is `blocked`, including
exit 0, error statuses and signals. It includes status/signal and recent
output when available; loss of the wrapper/container may leave status
unknown. Do not send a duplicate. A refused `done` does not silence this
monitoring. Broker `stop`/`destroy` ends the run intentionally; restart
begins a fresh monitored run. Your orchestrator may use `restart --agent-only`
after accepted `done` (or after this agent exits) to replace only the agent
session in a kept RUNNING child, with a new runner and prompt. It does not
boot the container or repeat `on_start`; existing services stay running.
Children cannot request this restart themselves.

For non-elevated service preparation, run `jailbee service refresh SERVICE`.
If it exits 3 because elevation is needed, send the orchestrator a `question`
naming the services; do not attempt to grant yourself elevation.

If you need an answer, send `question`, never a progress `note`, before
waiting. A live agent awaiting input without accepted done may trigger a
broker-generated `idle` report to your orchestrator (default 15 minutes,
repeated at that interval). It includes your last accepted message and recent
output. This safety net is not a substitute for asking explicitly. `idle` is
host-only, not a kind you can send; an explicit `blocked` suppresses it until
your next accepted message.

Always send `done` with `--wait` and end the session only after exit 0
(`done` or `queued`). The reply says whether work was imported and whether
the container will be destroyed or kept. On exit 1, fix the refusal and
resend under a new id. Exit 3 means no reply: the request may or may not have
run. Inspect `jailbee inbox ls` and `jailbee inbox status` before resending with
a new ID; queued requests are dropped when their connection closes.
After accepted `done`, stop working: the container may be destroyed. Commit everything before reporting.
Use the same `--wait` rule for any message your work depends on.

## Updates from your orchestrator

An `update` message carries `imported_ref` and `imported_sha`. If it also
has `merged_head`, the broker already applied the update to your branch:
re-read the changed files and do not merge `imported_ref` again. Otherwise,
commit or stash current work, run `git merge <imported_ref>`, resolve
conflicts, commit, then continue. The message body names the source:
by default the parent's current branch, or an explicit ref, commit or host
base branch. Explicit sources arrive at
`refs/jailbee/update/<orchestrator>/<manifest-id>`; always use the message's
`imported_ref` rather than guessing a branch ref.

## Waiting for a reply

After a `question` or `blocked`, keep the background waiter armed as described
in **Stay reachable**. It prints the oldest unread message's id on exit 0;
`wait` returns at once while any message is unread, so read every message
before re-arming it. Never poll tighter than every 5 min. Treat the next message from
your orchestrator as the answer. When it is a reply, its `in_reply_to` is the
id `jailbee inbox send` printed for your message: the host gives relayed
messages new ids, but maps a reply back to the id you chose. To answer one of
your orchestrator's messages, use the id you read in your inbox.

`wait` never marks anything read. Optional `--wake-on` filters message kinds
and status states, leaving nonmatches unread; children normally omit it so
no orchestrator instruction is missed. Only one ordinary waiter may run
(another exits 2). A concurrent `send --wait` stores its first status read while
the sender's connection is live. A disconnect during handling leaves it unread.
The orchestrator's `--quiet-after` watchdog uses broker child state;
it is not needed by a child waiting for its parent.

## Checking the inbox while working

At natural checkpoints (a finished step), run `jailbee inbox ls`. The
orchestrator may refine the task. Read messages with `jailbee inbox read <id>`;
the command marks the message read.

## Status messages for you

Every message you send gets a `status` reply whose `ref` is its id, with
state `done` (delivered), `queued` or `refused`. Only `refused` needs action;
it is final, so fix the cause and send a new message under a new id.
`queued` is not a failure: your orchestrator is stopped or has older parked
items; the report is imported before delivery when it can receive it. Do not
resend it. `send --wait [--timeout DUR|none]` (default 30m) prints the matching
status and marks only that status read: 0 for done/queued/started,
1 for refused/failed, 2 for usage error, 3 for timeout. `none` waits without a
deadline and never exits 3. A first-state refusal always prints its detail to
stderr and exits 1, even without `--wait`; stdout contains only the request ID.
The refusal stays in read history, so an ordinary waiter does not see it again.

Two `refused` details for `done` are not a fault in your manifest or your
tree: `could not fetch your branch to the host: ...` and
`could not record the done report: ...`. Retry `done` once under a new id; if
it repeats, send a `blocked` message describing the detail. For any other
unknown `refused` detail do the same: retry once under a new id, then send
`blocked`.
