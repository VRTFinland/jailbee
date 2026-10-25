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

If the file is missing, tell the human. The broker writes `grant.json` only
after its first round over your container (a few seconds after the grant),
so look again once before concluding anything. The file can also outlive a
revoked grant; a request made after revocation is `refused` with
"orchestration grant revoked".

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
  the id. Give the prompt on stdin (`-`) or with `--text`:

```bash
jailbee-inbox send spawn --branch fix/issue-12 --note "GitHub #12" - <<'EOF'
Task: fix issue 12 ...
EOF
```

  Never write prompt, summary or any scratch file inside the repo work tree
  (and so never pass a relative `--file`): an untracked file makes the tree
  dirty, and a dirty tree makes a `fork` spawn and an `import` refused. If
  you need a file, use an absolute path under `/tmp`.
- Note the id the helper prints (`<type>-<UTC time>-<hex>`, for example
  `spawn-20261025101500-a1b2`): it is the `ref` of the status reply.
- The child's short name comes from the branch: lowercased, every character
  outside `[a-z0-9-]` becomes `-`, runs of `-` collapse (`fix/Issue_12`
  becomes `fix-issue-12`). A name that is already taken is refused ("already
  exists"), so pick a distinct branch.

  Without the helper, write the JSON yourself (see `references/protocol.md`):
  write `.<id>.json.tmp`, then `mv` it to `<id>.json`.
- Use a new id every time and keep a scheme (`spawn-fix-12`, `import-fix-12`).
  Ids are never reused, not even after a refusal.

## The inbox loop

```bash
jailbee-inbox wait --timeout 30m   # prints the path of the oldest unread message
```

It exits 0 with a path when a message arrived, 1 on timeout and 2 on a usage
error. Run it in the background if your agent supports it (for example
Claude's background Bash), or use a /loop-style cycle. If you must poll, never poll
more often than every 5 min. Handle each message (`jailbee-inbox read <id>`
prints it and moves it to `inbox/read/`; or `mv` the file there yourself).
`jailbee-inbox ls` lists what is unread.

## Status messages

Every manifest you send gets a `status` reply whose `ref` is its id:

- `queued`: accepted, waiting for a running slot to free up. Applies to
  `spawn` and `restart` alike.
- `started`: the child is being created, or a queued restart has now begun.
- `done`: the action completed (the detail says what). A spawn's final
  `done` reads ``child `<short>` is ready``.
- `failed`: final. The action was accepted but did not complete. The detail
  says why, for example ``creation of child `<short>` failed: <error>``
  (followed by ` — the child was removed` when the child no longer exists),
  ``restart of child `<short>` failed: <error>``, ``creation of child
  `<short>` stopped unexpectedly during <phase>`` (a restart says
  `restart of child`) or ``creation of child `<short>` ended without a
  container``. `see the worker log` stands in for a missing error.
- `refused`: final. Read `detail`, fix the request, and send it again under a
  new id.

The broker is not instant; expect seconds, not milliseconds.

A spawn gets `queued` or `started`, then a final `done` (``child `<short>` is
ready``) or `failed`; a restart that begins later gets `failed` if it fails.
On `failed`, read the detail and tell the human; respawn under a new id only
when the cause is fixed. Still do not wait forever: if a `wait` times out
and a started child has not reported, send it a `message`. A `message` to a
stopped child is answered `queued` and delivered when it runs again.

A child whose prompted agent exits with status 1 to 127 sends you a
`blocked` message ``[jailbee] the agent `<agent name>` exited with status
<rc>``. The agent crashed or was killed; it did not report. Read the
child's output, then `restart` it with a new prompt or tell the human.

## Answering children

A `question` from a child arrives as a `message` with its `from`. The host
gives every relayed message a fresh id (`h-<hex>`), so the id you read in the
inbox is the one to use. Answer with a `message` to that child, setting
`in_reply_to` to it:

```bash
jailbee-inbox send message --to fix-issue-12 --reply-to h-3fa9c012 --text "Use the existing parser."
```

## When a child says done

1. Send `import` for the child. Wait for a `done` status whose detail names
   `refs/jailbee/from/<child>/<branch>`.
2. `git merge` (or cherry-pick) from that ref and resolve conflicts yourself.
3. Free the slot: a finished child is normally still running, so `stop`
   it, then `destroy` it if your grant has that operation (stop first is
   the recommended order). Stopping alone frees the slot, so queued spawns
   and restarts can start. `destroy` is refused if the child has commits
   you have not imported, unless `force` is true. Use `force` only when you
   mean to discard them.

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
