---
name: jailbee-orchestration
description: Use only when the user explicitly asks for JailBee child containers and this container holds an orchestration grant (`~/.jailbee/orchestration/grant.json`) — spawning children, briefing them, answering their questions, importing and merging their commits, and cleaning them up. Child containers are not subagents; the grant alone is no reason to use them, and ordinary delegation (subagents, parallel agents, plan execution) stays on the agent's own subagent tool. Trigger on "orchestrate child containers", "spawn a child container", "dispatch to containers", "merge the children", "orkestroi kontteja", "jaa tehtävät kontteihin", "käynnistä lapsikontti".
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

## Containers are not subagents

A grant is a permission, not a prompt to use it. Child containers never
replace your agent's own subagents:

- **Subagents** (your agent's Agent/Task tool, parallel agents, plan
  execution workflows) are the default for all ordinary delegation:
  research, review, implementing plan tasks, parallel independent fixes.
  They share this container and work tree, are cheap, and return their
  result straight to you.
- **Child containers** are heavy: each is a new container with its own
  checkout, branch, environment and long-running agent session, and its
  work comes back only through `import` and a merge. Use them only when
  the user asks for child containers (or names this workflow), for example
  to work a backlog of separate issues each on its own branch.

When a skill or plan tells you to dispatch subagents, use subagents, even
with a grant.

One child takes one whole unit of work: a ticket, or a whole plan. Never
split a plan's tasks across children; the child runs them itself, with its
own subagents. When plans depend on each other, spawn a dependent plan only
after its prerequisites are imported, merged and committed, as a `fork`, so
it starts from the merged work. If it is unclear which the user means, ask before spawning.

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

## Choosing the child's agent (`runner`)

By default a child runs the repo's usual agent with its usual model. A spawn
may name a different one in its optional `runner` field: `agent` (`claude` or
`claude-jb`), `profile` (LiteLLM profile, `claude-jb` only) and `model` (tier
`fable`, `opus`, `sonnet` or `haiku`). The child's own agent is not yours to
tune, so the rule is **default first**:

- Omit `runner` unless the user asked for a specific agent, profile or model,
  or the user's own instructions (CLAUDE.md, the task prompt) name one.
- Never pick a cheaper or stronger model on your own initiative, whatever the
  task looks like.
- A `restart` without `runner` keeps the child's previous runner; `"runner":
  null` clears it, so the child runs the default again.

List what you may name, and what omitting it starts (the default agent shows
as `unknown` when it is not known; then just omit `runner`):

```bash
jailbee-inbox runners          # or --json
jailbee-inbox send spawn --branch fix/issue-12 --agent claude-jb --profile grok --model sonnet - <<'EOF'
...
EOF
jailbee-inbox send restart --child fix-issue-12 --agent claude --model opus - <<'EOF'
...
EOF
jailbee-inbox send restart --child fix-issue-12 --default-runner --text "Try again."
```

`--profile` and `--model` need `--agent`, and `--profile` needs `--agent
claude-jb`; `--default-runner` is for `restart`
only and excludes `--agent`.

A runner that cannot be honoured is `refused`, never replaced by the default.
The detail is one of:

- ``agent `claude` is not enabled in this repo``
- ``agent `claude` has no prompt_args, so it cannot take a prompt``
- ``agents.claude.command must start with `claude` or `claude-jb` to take a runner (it is `...`)``
- ``claude-jb needs LiteLLM enabled in this repo``
- ``LiteLLM profile `P` is not served (configured: ...; run `jailbee litellm up`)``
- ``LiteLLM profile `P` has no `M` tier``

Fix the request or tell the human; do not retry with another model.

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

`wait` does not consume anything: while any message is unread it returns at
once, with the oldest one. Mark every message read (by its id, `h-<hex>`, not
its file name) before you wait again, or the loop spins on the same message.

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
  ``creation of child `<short>` stopped unexpectedly during <phase>`` or
  ``creation of child `<short>` ended without a container``. `see the worker log` stands in for a missing error.
- `refused`: final. Read `detail`, fix the request, and send it again under a
  new id.

The broker is not instant; expect seconds, not milliseconds.

A spawn gets `queued` or `started`, then a final `done` (``child `<short>` is
ready``) or `failed`. A restart gets `done` or `started` and no later final
status.
On `failed`, read the detail and tell the human; respawn under a new id only
when the cause is fixed. Still do not wait forever: if a `wait` times out
and a started child has not reported, send it a `message`. A `message` to a
stopped child is answered `queued` and delivered when it runs again.

A child whose prompted agent exits with status 1 to 127 sends you a
`blocked` message ``[jailbee] the agent `<agent name>` exited with status
<rc>``. The agent exited with an error status; it did not report. (A kill, status above 128, is deliberately not reported.) Read the
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
   The import also brings the child's submodule commits, but a merge only
   moves the gitlinks: run `git submodule update` afterwards so the
   submodule working trees match. No network is needed; the objects are
   already local.
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
