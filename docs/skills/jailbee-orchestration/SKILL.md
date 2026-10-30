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
  work comes back through `import`, followed by a merge or a separate PR. Use them only when
  the user asks for child containers (or names this workflow), for example
  to work a backlog of separate issues each on its own branch.

When a skill or plan tells you to dispatch subagents, use subagents, even
with a grant.

One child takes one whole unit of work: a ticket, or a whole plan. Never
split a plan's tasks across children; the child runs them itself, with its
own subagents. In the merged-fork workflow, when plans depend on each other, spawn a dependent plan only
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
  During creation, a child counts from the `started` reply until it is
  running and its create has finished, or its create has failed.
- `operations` lists the manifest types you may use. `spawn` and `message`
  are always there; `import`, `restart`, `stop`, `destroy` and `rights` may not be.
- `grantable` is the most you may give a child: mounts by name, whether
  `loose` networking is allowed, and egress hosts.

If the file is missing, tell the human. The broker writes `grant.json` only
after its first round over your container (a few seconds after the grant),
so look again once before concluding anything. The file can also outlive a
revoked grant; a request made after revocation is `refused` with
"orchestration grant revoked".

## Runtime rights

Ask for the least: prefer an egress host over loose, and a timed grant over
`never`; revoke when the need ends.

Use `jailbee-inbox send rights --child C --egress pypi.org --for 5m --wait`
to grant access within your current grant and repo cap. Effective loose
permission includes narrower egress within the repo cap. `--mount K` and
`--loose` grant other items; each grant needs `--for` (max 24h or `never`).
Use `--revoke-loose`, `--revoke-mount K`, `--revoke-egress H` without `--for`
for revoke-only. Granting needs operation `rights`; revoking needs only your
active grant. You cannot change your own rights. Rights a human added stay
untouched, and a grant never touches a right a human holds; a timed grant never shortens a right you own. A child's own revoke
arrives as a note so update your picture of its access.

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
  a dirty tree is refused. The child inherits the source's PR bindings; normal
  bindings are adopted, while a review source retains its review identity.
- `new` starts a fresh checkout from a base (`--new [--base main]`).
- Grant the least rights the child needs, and only within `grantable`.
- Prefer the helper; it writes the manifest and body atomically and prints
  the id. Give the prompt on stdin (`-`) or with `--text`:

```bash
jailbee-inbox send spawn --branch fix/issue-12 --note "GitHub #12" --wait - <<'EOF'
Task: fix issue 12 ...
EOF
```

  Never write prompt, summary or any scratch file inside the repo work tree
  (and so never pass a relative `--file`): an untracked file makes the tree
  dirty, and a dirty tree makes a `fork` spawn refused. If
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

New children default to `on_done: destroy`: after importing and delivering
`done`, the broker removes them. Use `--on-done keep` for multi-session
children. Without the `destroy` operation the default is `keep`; explicitly
requesting `destroy` is refused. Restart may change the policy; omission
keeps the current value.

## One PR per child

For independent PRs, spawn each child with `--new --base main` (replace
`main` with the upstream base), not a fork of your work. This keeps
orchestrator commits out of the child PR. On `done`, use `imported_ref` and
`imported_sha` to stage one source manifest in your PR outbox; do **not**
merge the import. See the [source examples](../jailbee-pr-review/references/manifest-schema.md#source-publication-examples).
The host publishes each with `jb pr push <orchestrator> --source-manifest
<file>`. Let `on_done: destroy` clean up after import.

For rework, spawn with `--ref refs/jailbee/from/<child>/<branch>` or a local
branch as `--ref refs/heads/<name>`, or use `--pr N`. Import the result and
stage a numbered `pr: N` source manifest. A `--pr` child's own outbox is
lost on destroy; use `--on-done keep` to preserve it. Before reusing an
imported ref, publish its source manifest or preserve the commit under a
separate persistent ref and use that ref in the manifest. Staging alone does
not retain the ref; a later import can move it.

Spawn and restart may also name `--autostart-profile NAME` to select the repo's
startup stages; this is separate from the runner's LiteLLM `--profile`.

## Choosing the child's agent (`runner`)

A spawn may name an optional `runner`: `agent` (`claude` or `claude-jb`),
`profile` (LiteLLM profile, `claude-jb` only) and `model` (tier `fable`,
`opus`, `sonnet` or `haiku`).

- Run `jailbee-inbox runners` first to see the user's default and the profiles
  and tiers you may name.
- Omit `runner` to start the default. Choose per task: use a cheaper tier for
  routine, well-specified work and a stronger one for hard or open-ended work.
  Follow the user's guidance on model choice (CLAUDE.md or the task prompt)
  whenever it exists; when in doubt, use the default.
- Keep the default's agent and profile unless the user's guidance says
  otherwise; vary the `model` tier.
- A `restart` without `runner` keeps the child's previous runner;
  `--default-runner` puts it on the current default.
- If a runner is refused, tell the human; do not retry with another model.

Examples:

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

If `jailbee-inbox`, `inbox/` or `outbox/` is missing just after a grant,
wait briefly: the broker installs them on its next round, within a few
seconds. Do not hand-roll the protocol. If they stay missing, tell the
human; the broker may not be running (`jailbee orchestrate status` on the host).

```bash
jailbee-inbox wait --wake-on done,question,blocked,failed,refused --quiet-after 60m --timeout 30m
```

It exits 0 with a path when a message arrived, 1 on timeout and 2 on a usage
error. Run it in the background if your agent supports it (for example
Claude's background Bash), or use a /loop-style cycle. If you must poll, never poll
more often than every 5 min. Handle each message (`jailbee-inbox read <id>`
prints it and moves it to `inbox/read/`; or `mv` the file there yourself).
After `wait` returns, run `jailbee-inbox ls` and handle **every** unread
message before waiting again: replies to several manifests arrive as a burst.

Use `--wake-on`, not home-made wrappers that read/log notes or statuses:
**wait never consumes anything**, including nonmatches. It returns the oldest
matching unread path; without a filter any unread message matches. Message
kinds and status states share the filter vocabulary. A child's first message
after spawn/restart overrides the filter even if it is a note; read history
prevents later notes from overriding again. Explicitly read messages by id,
not filename, or a matching unread message keeps waking you.

`--quiet-after 60m` also returns exit 0 with `quiet-child CHILD` (not a path)
when a running child has sent nothing for an hour. Check that child; the
watchdog repeats until activity, stop or restart. Both read and unread reports
count as activity; a child with no reports is timed from its start. The broker
refreshes `children.json`; missing, corrupt or more than 60s-old state makes
this watchdog exit 2, so check the host broker instead of assuming all is well.

Only one ordinary `wait` may run per container; another exits 2. Concurrent
`send --wait` is supported: it reserves its status ref before publishing and
ordinary `wait` skips that ref while the sender lives. Reservations disappear
on process exit, including crashes/timeouts; stale lock files do not hide
replies. Do not explicitly `read` a pending sender's status from another
session. After a wake, handle all other unread messages, including filtered
notes, but leave reserved replies to their sender.

## Status messages

Every manifest you send gets a `status` reply whose `ref` is its id:

- `queued`: accepted, waiting for a running slot to free up. Applies to
  `spawn` and `restart` alike.
- `started`: the child is being created, or a queued restart has now begun.
- `done`: the action completed (the detail says what). A spawn's final
  `done` reads ``child `<short>` is ready; agent is alive``.
- `failed`: final. The action was accepted but did not complete. The detail
  says why, for example ``creation of child `<short>` failed: <error>``
  (followed by ` — the child was removed` when the child no longer exists),
  ``creation of child `<short>` stopped unexpectedly during <phase>`` or
  ``creation of child `<short>` ended without a container``. `see the worker log` stands in for a missing error.
- `refused`: final. Read `detail`, fix the request, and send it again under a
  new id.

Use `send --wait` for `spawn`, `update` and `destroy`. It prints and marks
only the matching status read: exit 0 for done/queued/started, 1 for
refused/failed, 2 for usage errors, 3 for timeout (`--timeout`, default 30m).
A timeout is not a refusal: read the eventual reply instead of resending.
The broker is not instant; expect seconds, not milliseconds.

Spawn and restart get `queued` or `started`, then final `done`
(``child `<short>` is ready; agent is alive``) or `failed`. `started` is not
proof the agent is working: the broker checks the real process twice, at
least two seconds apart, after setup. A never-started agent fails after a
120-second startup grace; immediate exits and failed setup also fail.
On `failed`, read the detail and tell the human; respawn under a new id only
when the cause is fixed. Still do not wait forever: if a `wait` times out
and a started child has not reported, send it a `message`. A `message` to a
stopped child is answered `queued` and delivered when it runs again.

After readiness the broker reports any prompted-agent exit without accepted
`done` as `blocked`: exit 0, errors and signals alike. It includes the exit
status/signal and a bounded tmux output tail when available; disappearance
of the wrapper or container is reported with unknown status. Only accepted
`done` and successful broker `stop`/`destroy` suppress this; a refused `done`
does not. Read the detail, then `stop` the still-running container and
`restart` with a new prompt, or tell the human. Restart launches a fresh
monitored run. A transient probe failure is retried.

## Answering children

A `question` from a child arrives as a `message` with its `from`. The host
gives every relayed message a fresh id (`h-<hex>`), so the id you read in the
inbox is the one to use. Answer with a `message` to that child, setting
`in_reply_to` to it:

```bash
jailbee-inbox send message --to fix-issue-12 --reply-to h-3fa9c012 --text "Use the existing parser."
```

## When a child says done

1. The report already carries `imported_ref` (`refs/jailbee/from/<child>/<branch>`)
   and `imported_sha`. If it has
   `import_error`, fix the cause and send `import --child C --wait` to retry.
   `on_done_reason` is not an error: the import succeeded, but the child was
   kept instead of destroyed for that reason.
2. In the merged-fork workflow, `git merge <imported_ref>` (or cherry-pick) and resolve conflicts yourself. For one PR per child, stage the source manifest instead and do not merge.
   Submodule commits are already local; run `git submodule update` after
   merging so the working trees match their gitlinks.
   Before reusing an imported `refs/jailbee/from/<child>/<branch>`, merge it
   in the merged-fork workflow or publish its source manifest in the per-child
   PR workflow: the next child's `done` overwrites the ref.
3. `on_done: destroy` schedules automatic cleanup after delivery. A done
   `keep` child needs only `jailbee-inbox send destroy --child C --wait`, not
   a separate stop. Destroy refuses new commits, uncommitted changes and an
   unreadable working tree; `force` deliberately discards them. If work
   changes after done, automatic cleanup keeps the child and tells you.

## Rework

For merged work, prefer a fresh fork for rework: it starts from your latest merged state. For independent PRs, use the ref or PR workflow above.
For an existing `keep` child, `jailbee-inbox send update --child C --wait`
transports your branch and tells the child which ref to merge. The broker
never merges it. To launch another session, stop then restart the child
with a new prompt; an immediate restart reports `done`, a queued one reports
`queued` then `started`.

## Finishing

When everything is merged, offer the human a pull request through the
**jailbee-pr-review** / `jb pr push` path. You cannot push. Summarise what each
child did.

## What not to assume

- The broker is not instant, and a stopped orchestrator's messages wait in
  the inbox until it runs again.
- Children cannot spawn, and the grant is all-or-nothing.
- A queued spawn does not start until a running child is stopped or
  destroyed.
