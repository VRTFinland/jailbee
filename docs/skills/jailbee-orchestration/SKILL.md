---
name: jailbee-orchestration
description: Use when this container holds a JailBee orchestration grant (`~/.jailbee/orchestration/grant.json` exists) and you want to split work across child containers — spawning children, briefing them, answering their questions, importing and merging their commits, and cleaning them up. Trigger on "orchestrate", "spawn a child container", "fan out these issues", "dispatch to containers", "merge the children", "orkestroi", "jaa tehtävät kontteihin", "käynnistä lapsikontti".
---

# Orchestrating child containers

## What you are

You are an orchestrator inside a sandbox. You act only by writing manifests
into `~/.jailbee/orchestration/outbox/`; the host broker validates each one
against your grant and answers in `~/.jailbee/orchestration/inbox/`. Nothing
reaches GitHub, and there is no `jb` in here. Children cannot spawn further
children, and there are no partial grants: a request is accepted or
`refused` as a whole. The exact schemas are in
[`references/protocol.md`](references/protocol.md).

Always refer to containers by their **short name** (the branch-level name,
for example `fix-issue-12`), never one prefixed with the repo name.

## Read your grant first

```bash
cat ~/.jailbee/orchestration/grant.json
```

- `max_running_children` is the limit on children **running** at once.
  Stopped children do not count. A spawn over the limit waits as `queued`.
- `operations` lists the manifest types you may use. `spawn` and `message`
  are always there; `import`, `restart`, `stop` and `destroy` may not be.
- `grantable` is the most you may give a child: mounts by name, whether
  `loose` networking is allowed, and egress hosts.

If the file is missing you are not an orchestrator: stop and tell the human.

## Writing a child prompt

The child already receives a preamble that names its own skill, so do not
repeat protocol rules. Give it:

```text
Task: <one sentence>
Context: <issue text, relevant files and paths>
Acceptance: <the tests that must pass, how to run them>
Report: send `done` with a short summary when finished; send `question`
when you are stuck and need an answer.
Commit everything before you send `done`.
```

## Spawning

- `fork` (default) starts the child from your committed HEAD. Commit first:
  a dirty tree is refused.
- `new` starts a fresh checkout from a base (`--new [--base main]`).
- Grant the least rights the child needs, and only within `grantable`.
- Prefer the helper; it writes the manifest and body atomically and prints
  the id:

```bash
jailbee-inbox send spawn --branch fix/issue-12 --file prompt.md --note "GitHub #12"
```

  Without the helper, write the JSON yourself (see `references/protocol.md`):
  write `.<id>.json.tmp`, then `mv` it to `<id>.json`.
- Use a new id every time and keep a scheme (`spawn-fix-12`, `import-fix-12`).
  Ids are never reused, not even after a refusal.

## The inbox loop

```bash
jailbee-inbox wait --timeout 30m   # prints the path of the oldest unread message
```

Run it in the background if your agent supports it (for example Claude's
background Bash), or use a /loop-style cycle. If you must poll, never poll
more often than every 5 min. Handle each message (`jailbee-inbox read <id>`
prints it and moves it to `inbox/read/`; or `mv` the file there yourself).
`jailbee-inbox ls` lists what is unread.

## Status messages

Every manifest you send gets a `status` reply whose `ref` is its id:

- `queued`: accepted, waiting for a running slot to free up. Applies to
  `spawn` and `restart` alike.
- `started`: the child is being created, or a queued restart has now begun.
- `done`: the action completed (the detail says what).
- `refused`: final. Read `detail`, fix the request, and send it again under a
  new id.

The broker is not instant; expect seconds, not milliseconds.

## Answering children

A `question` from a child arrives as a `message` with its `from`. Answer with
a `message` to that child, setting `in_reply_to` to the question's id:

```bash
jailbee-inbox send message --to fix-issue-12 --reply-to h-3fa9c012 --text "Use the existing parser."
```

## When a child says done

1. Send `import` for the child. Wait for a `done` status whose detail names
   `refs/jailbee/from/<child>/<branch>`.
2. `git merge` (or cherry-pick) from that ref and resolve conflicts yourself.
3. Free the slot: a finished child is normally still running, so `stop`
   it first (`destroy` is refused while it runs: "stop it first"). Stopping
   alone frees the slot, so queued spawns and restarts can start. Then
   `destroy` it if your grant has that operation. `destroy` is refused if
   the child has commits you have not imported, unless `force` is true. Use
   `force` only when you mean to discard them.

## Rework

To redo a child's work, `restart` a stopped child with a new prompt. A
restart that runs at once is answered `done` ("child `x` restarted"); with
no free slot it is `queued`, then `started` when it later begins. Never
try to merge your own work into a child. If you need newer base work in a
child, spawn a fresh fork instead.

## Finishing

When everything is merged, offer the human a pull request through the
**jailbee-pr-review** / `jb pr` path. You cannot push. Summarise what each
child did.

## What not to assume

- The broker is not instant, and a stopped orchestrator's messages wait in
  the inbox until it runs again.
- Children cannot spawn, and the grant is all-or-nothing.
- A queued spawn does not start until a running child is stopped or
  destroyed.
