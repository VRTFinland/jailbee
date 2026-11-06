---
name: jailbee-orchestration
description: Use only when the user explicitly asks for JailBee child containers and this container holds an orchestration grant (`~/.jailbee/orchestration/grant.json`) — spawning children, briefing them, answering their questions, importing and merging their commits, and cleaning them up. The grant alone is no reason to orchestrate; once invoked, keep the orchestrator role for the session, using subagents for investigation, review and light work, and children for work needing its own development environment. Trigger on "orchestrate child containers", "spawn a child container", "dispatch to containers", "merge the children", "orkestroi kontteja", "jaa tehtävät kontteihin", "käynnistä lapsikontti".
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

A grant is a permission, not a prompt to use it. Loading this skill means
the user asked you to orchestrate: stay the orchestrator for the rest of
the session. Save your context for watching children, merging their work,
reviews and conversation with the user.

For each new user task, investigate here first: root cause, relevant files
and open design questions. Use a subagent for anything beyond a few lookups.
Then choose the implementation vehicle by what the work needs:

- **Subagents** share this container and work tree and return their result
  straight to you. Use them for investigation and review. Light work needing
  no development environment (text, docs or skill wording) goes to a fresh
  subagent, or a forked copy of your own agent session where your agent has
  one (not a `fork` child container), when saving your context matters;
  tiny edits can be done inline.
  Delegation is the default for any reading or writing task larger than a few
  lookups: code surveys across many files, spec drafts, implementation plans,
  long write-ups and code reviews. Reading many files to anchor a plan burns
  the context that keeps children, merges and the final gate moving. After a
  brainstorm with the user, hand plan and spec writing to a fork of your own
  agent session (it inherits the conversation, so the decisions travel); use a
  fresh subagent for surveys and reviews. Keep in your own context the live
  conversation with the user, decisions, merges, manifests and short lookups.
- **Child containers** have their own checkout, branch, environment and
  long-running agent session. Use one when work benefits from its own
  development environment: code changes with test suites, services, long
  multi-step implementation or whole plans. Brief it with your findings and
  acceptance tests. The child implements, tests and commits; on `done`,
  merge or stage its work.

Beyond tiny light edits, do the work yourself only when it needs this
container's own live environment or the user asks you to.

When a skill or plan tells you to dispatch subagents, use them at the
appropriate level: here for investigation, review and light work; in the
implementing child for plan tasks.

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

An `adopted` inbox message means the host user attached an existing container
as your child. It has no import baseline and its agent is not supervised or
automatically relaunched. Ask the user to close a hand-started agent before
using `jailbee inbox send restart --child C --agent-only` to relaunch it under
supervision; otherwise you may start a second agent.

## Publishing your own staged manifests

If `grant.json.publish` covers every pending action, stage normally, then ask
the host broker to publish your own outbox file:

```bash
jailbee inbox send publish --issue a.json --wait
jailbee inbox send publish --pr b.json --wait
```

Issue kinds use `publish.issues` for the own repo/submodules, or
`publish.foreign_issues.actions` plus its repo list for foreign issues.
Assignment is an issue kind too: `assign_self` covers only assigning or
unassigning the publisher's own login (so it can also unassign the host user
from issues the user claimed by hand), `assign` covers any login and implies
`assign_self`.

PR source manifests need `publish.pull_requests`; reviews/replies/descriptions
need `publish.pr_reviews` and must target bound PRs in the own repository.
Existing unbound upstream branches, default/base heads, force pushes and
foreign PRs are refused. New PRs use the default base; updates require an
authored source-PR binding, not a review-only PR/stacked label or adopted
binding. Updating a PR opened some other way remains a host step. Children never publish.

`--wait` may return at `started`; keep your inbox waiter armed and read the
final `done`/`failed` status for the printed ref. Use the returned URLs and
created issue numbers, in manifest order; staging or `started` is not success.
One request runs at once. Budget is reserved for every pending action and is
never refunded on failure (only an in-lock policy refusal returns it). An uncertain result means stop and tell the human
to resolve it with `jb outbox` on the host; do not retry automatically. A
normal retry gets a new id. Without the required right, keep the manifest for
human review and give the host apply command instead.

## Runtime rights

Ask for the least: prefer an egress host over loose, and a timed grant over
`never`; revoke when the need ends.

Use `jailbee inbox send rights --child C --egress pypi.org --for 5m --wait`
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
repeat protocol rules. Sparse messaging is the child's default (no progress
notes; only `question`, `blocked`, scope changes and `done`), so you need not
ask for it. Give it:

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
- HEAD forks seed configured `fork.warm_paths` (Git-ignored build outputs) by btrfs reflink; repeatable `--warm PATH` replaces the list and `--no-warm` disables it (mutually exclusive, fork only). Other pools or a changed source skip with a note.
- `new` starts a fresh checkout from a base (`--new [--base main]`).
- Grant the least rights the child needs, and only within `grantable`.
- Prefer the helper; it writes the manifest and body atomically and prints
  the id. Give the prompt on stdin (`-`) or with `--text`:

```bash
jailbee inbox send spawn --branch fix/issue-12 --note "GitHub #12" --wait - <<'EOF'
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

Children must send GitHub requests as messages containing the exact target and
content; stage them in your own outbox using `jailbee issue` or `jailbee pr`.
Never write GitHub outbox files or directories by hand. Pending child GitHub
outbox proposals make the broker keep the child on done, even with `keep`
already configured; inspect and resolve the leftovers. A broker stop records a
pending count; an externally stopped child without that record has unknown
outbox state and is conservatively kept.

## One PR per child

For independent PRs, spawn each child with `--new --base main` (replace
`main` with the upstream base), not a fork of your work. This keeps
orchestrator commits out of the child PR. On `done`, substitute `imported_ref`
and `imported_sha` in this command to stage a new PR source and description;
do **not** merge the import:

```bash
jailbee pr source --ref REF --sha SHA --repo owner/name --body "Describe the change"
```

See the [source examples](../jailbee-pr-review/references/manifest-schema.md#source-publication-examples).
The host publishes each with `jb pr push <orchestrator> --source-manifest
<file>`. Let `on_done: destroy` clean up after import.

For rework, spawn with `--ref refs/jailbee/from/<child>/<branch>` or a local
branch as `--ref refs/heads/<name>`, or use `--pr N`. Import the result and
stage a numbered source with `jailbee pr source --ref REF --sha SHA --repo
owner/name --pr N`. A description is optional for a numbered PR; add `--body`
or `--body-file` when replacing it. Children cannot stage GitHub proposals;
relay their exact content into your own outbox with `jailbee issue` or
`jailbee pr`. Before reusing an
imported ref, publish its source manifest or preserve the commit under a
separate persistent ref and use that ref in the manifest. Staging alone does
not retain the ref; a later import can move it.

Spawn and restart may also name `--autostart-profile NAME` to select the repo's
startup stages; this is separate from the runner's LiteLLM `--profile`.
For light children, use `send spawn --autostart-profile none --wait` to select
no configured repository stages or profile services while leaving the generated
`agents` stage in effect. An explicit `--services` selection still overrides
the profile's service set. Alternatively, use `send spawn --services web --wait`
or `send restart --child C --services web` to select those services and their
dependencies. `--services ''` selects no services; omission on spawn uses the
profile or all, while omission on restart retains the child's explicit service
selection. Nonservice stages still follow the selected profile.
On restart, `--autostart-profile none` alone preserves explicit services. Use
`send restart --child C --autostart-profile none --services ''` for a light
child with no services. The broker has no `--reset-services` option, so an
existing explicit selection cannot be cleared back to the profile default.
Prepare runs once per service/container and
cannot exceed the
child's stored rights. Ordinary containers are bounded by creation-time approved
elevation (legacy containers use host-effective declarations). Re-assessment is
needed to widen it; an accepted host or orchestrator refresh can update it. Live profile
switches defer unprepared elevated services and their dependents until restart.
Do not combine with `--agent-only`.

## Refreshing a child's services

After `update --merge` changes dependencies or migrations, or the child asks, use
`jailbee inbox send refresh --child C --services db,web --wait`. The `refresh`
operation is required; on the host add it to an old grant with
`jb orchestrate grant ORCHESTRATOR --allow refresh`; a bare grant preserves old operations.
Final status gives per-service outcomes. Accepted child-edited commands run only
within the grant and repository cap. Elevated refresh pauses an idle agent and
resumes the same session after restoration, even on ordinary prepare failure.
Unknown session or agent state, insufficient rights, a busy child, or foreign
live agents cause refusal. Use `--force` only after the child says its work is
paused or stopped; never interrupt a working child. Host `--yes` only consents
to foreign agents, not force. Restricted SSH cannot run host refresh.
If the final status is missing and execution is uncertain, inspect manually before
retrying; prepare may have run. If restoration is unconfirmed, resume is withheld;
follow the network and mount recovery commands. If supervision registration fails, resume is also
withheld and service outcomes remain available. After restoring safety, use
`jailbee inbox send restart --child C --agent-only --resume SESSION --text 'Continue your task.'`.

## Choosing the child's agent (`runner`)

A spawn may name an optional `runner`: `agent` (`claude` or `claude-jb`),
`profile` (LiteLLM profile, `claude-jb` only) and `model` (tier `fable`,
`opus`, `sonnet` or `haiku`).

- Run `jailbee inbox runners` first to see the user's default and the profiles
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
jailbee inbox runners          # or --json
jailbee inbox send spawn --branch fix/issue-12 --agent claude-jb --profile grok --model sonnet - <<'EOF'
...
EOF
jailbee inbox send restart --child fix-issue-12 --agent claude --model opus - <<'EOF'
...
EOF
jailbee inbox send restart --child fix-issue-12 --default-runner --text "Try again."
```

`--profile` and `--model` need `--agent`, and `--profile` needs `--agent
claude-jb`; `--default-runner` is for `restart`
only and excludes `--agent`. A model tier may have the exact lowercase suffix
`[1m]`, for example `--model 'opus[1m]'`; quote it to prevent shell globbing.
For `claude-jb`, the profile must serve the base tier.

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

If `inbox/` or `outbox/` is missing just after a grant, wait briefly: the
broker creates the directories on its next round. If they stay missing, tell
the human; the broker may not be running (`jailbee doctor` on the host reports
it, and `journalctl --user -u jailbee-broker` shows its log). If `jailbee inbox` is missing, the broker never installs the command:
ask the human to run `jb base build` on the host and recreate the container.
The broker leaves the stale standalone helper that old images retain in place.
Do not hand-roll the protocol.

```bash
jailbee inbox wait --wake-on msg:done,question,blocked,idle,status:failed,status:refused --quiet-after 60m --timeout 30m
```

It exits 0 with a path when a message arrived, 1 on timeout and 2 on a usage
error. Run it in the background if your agent supports it (for example
Claude's background Bash), or use a /loop-style cycle. If you must poll, never poll
more often than every 5 min. Handle each message (`jailbee inbox read <id>`
prints it and moves it to `inbox/read/`; or `mv` the file there yourself).
After `wait` returns, run `jailbee inbox ls` and handle **every** unread
message before waiting again: replies to several manifests arrive as a burst.
For a background orchestrator waiter, use `--timeout none` only together with
`--quiet-after` so silent children still wake it, and add `--digest 55m` so you
see the `inbox status` table at least hourly even if the heuristic misses a
stalled child.

Use `--wake-on`, not home-made wrappers that read/log notes or statuses:
**wait never consumes anything**, including nonmatches. It returns the oldest
matching unread path; without a filter any unread message matches. A bare
word matches a message kind or a status state (`done` is both, so a bare `done`
wakes on `delivered to` acks); `msg:<kind>` matches only messages and
`status:<state>` only status replies. A child's first message
after spawn/restart overrides the filter even if it is a note; read history
prevents later notes from overriding again. Explicitly read messages by id,
not filename, or a matching unread message keeps waking you.

`--quiet-after 60m` also returns exit 0 with `quiet-child CHILD` (not a path)
when a running child that has not reported done shows no activity for an hour.
Activity is a message from the child (read or unread), an agent event or a
commit; CPU use is not activity, because a hung process keeps using some. A
child with none is timed from its start. A long silent build or test run also
wakes you: check `LONGEST` before concluding the child is stuck. Check that child (see
"Checking on children"); the watchdog repeats until activity, stop or restart.
The broker refreshes `children.json`; missing, corrupt or more than
15-minute-old state makes this watchdog exit 2, so check the host broker
instead of assuming all is well.

`--digest 60m` is an optional extra: when an hour has passed since the last
digest and a child is running, `wait` exits 0 printing `digest` on the first
line, then the `jailbee inbox status` table. That is one wakeup per period for
all running children (the time is kept in `~/.jailbee/orchestration/digest-at`;
the first digest is due one period after the first `wait --digest`). A real
message due at the same time, or a `quiet-child` wakeup, wins; the digest comes
at the next wait. It needs fresh `children.json` like `--quiet-after`. A
`blocked` or `idle` report from the broker counts as a message from the child.

`jailbee inbox children` (`--json` for the raw entries) shows each child's
state from that file. `running` is the **container** state: a child that sent
`done` with `on_done: keep` stays `running: true` until it is stopped or
destroyed, and its services stay available. `done: true` means its `done` was
accepted (its agent has usually exited). Never report a child as stopped
because it is done.

Only one ordinary `wait` may run per container; another exits 2. Concurrent
`send --wait` is supported: it reserves its status ref before publishing and
ordinary `wait` skips that ref while the sender lives. Reservations disappear
on process exit, including crashes/timeouts; stale lock files do not hide
replies. Do not explicitly `read` a pending sender's status from another
session. After a wake, handle all other unread messages, including filtered
notes, but leave reserved replies to their sender.

## Checking on children

Before messaging a child to ask how it is doing, run `jailbee inbox status`
(`--child C` for one, `--json` for the raw fields). It costs the child nothing.
Columns: `NAME STATE AGENT ACTIVE CPU MEM HEAD DIRTY LONGEST LAST-MSG`; `?`
means unknown, and a stopped child shows little more than `HEAD`. A stale
snapshot only warns on stderr there and still prints (exit 0), unlike
`wait --quiet-after/--digest`, which exit 2.

- Fine: a recent `ACTIVE` (agent events or commits), recent commits, agent
  `alive:busy`.
- Look at it: agent alive but no `ACTIVE` change for about 30 minutes, even
  with some CPU (a hung process still burns CPU); one
  process in `LONGEST` (the longest-running tool process of the agent, by
  process name) running for hours; agent `dead` without done (also reported as
  `blocked`; `starting` means the agent is not launched yet, not dead); `waiting` (also reported as `idle`).
- Only then message the child.

CPU is measured container-wide and is shown for information only: it never
counts as activity, so `--quiet-after` fires for a child whose agent is stuck on
a busy process. Judge such a child by `ACTIVE`, commits and `LONGEST`.

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
  A worker that stopped unexpectedly also says what it left: ` — the child was
  removed`, ` — the child was kept, stopped`, or `` — the child was kept
  running; `stop` it before a `restart` ``; a running child refuses `restart`
  until you `stop` it. The broker log on the host names the worker's log.
- `refused`: final. Read `detail`, fix the request, and send it again under a
  new id.

Use `send --wait` for `spawn`, `update` and `destroy`. It prints and marks
only the matching status read: exit 0 for done/queued/started, 1 for
refused/failed, 2 for usage errors, 3 for timeout (`--timeout`, default 30m;
`none` waits without a deadline and never exits 3).
A timeout is not a refusal: read the eventual reply instead of resending.
The broker is not instant; expect seconds, not milliseconds.

Spawn and restart get `queued` or `started`, then final `done`
(``child `<short>` is ready; agent is alive``) or `failed`. `started` is not
proof the agent is working: the broker checks the real process twice, at
least two seconds apart, after setup. A never-started agent fails after a
120-second startup grace; immediate exits and failed setup also fail. A child
that reports done before readiness is confirmed still gets a final `done`
(``child `<short>` is ready; it already reported done``).
On `failed`, read the detail and tell the human; respawn under a new id only
when the cause is fixed. Still do not wait forever: if a `wait` times out
and a started child has not reported, send it a `message`. A `message` to a
stopped child is answered `queued` and delivered when it runs again.

After readiness the broker reports any prompted-agent exit without accepted
`done` as `blocked`: exit 0, errors and signals alike. It includes the exit
status/signal and a bounded tmux output tail when available; disappearance
of the wrapper or container is reported with unknown status. Only accepted
`done` and successful broker `stop`/`destroy` suppress this; a refused `done`
does not. Read the detail, then use `restart --agent-only` on the still-running
idle child with a new prompt, or tell the human. Stop then ordinary restart
is still available when container startup needs repeating. Either launches a
fresh monitored run. A transient probe failure is retried.

A live prompted agent awaiting input also yields a broker-generated `idle`
message after 15 minutes (repo `orchestration.idle_after_seconds`, default
900; also the repeat interval). It names the child, elapsed idle time, last
accepted child message kind/first line and recent pane output when available.
Read the last message and answer the child or tell the human; `idle` is not
completion or permission to interrupt a live agent with `restart --agent-only`.
Activity or accepted child traffic resets the episode. Accepted `done`,
broker stop/destroy and agent-exit `blocked` end monitoring; a child-sent
`blocked` suppresses idle until its next accepted message. A refused `done`
does not silence it. Detection uses the prompted process's adapter session
signal (currently Claude), not unchanged pane output; unsupported agents or
unknown signals rely on the separate quiet-child watchdog.

## Answering children

A `question` from a child arrives as a `message` with its `from`. The host
gives every relayed message a fresh id (`h-<hex>`), so the id you read in the
inbox is the one to use. Answer with a `message` to that child, setting
`in_reply_to` to it:

```bash
jailbee inbox send message --to fix-issue-12 --reply-to h-3fa9c012 --text "Use the existing parser."
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
   `keep` child needs only `jailbee inbox send destroy --child C --wait`, not
   a separate stop. Destroy refuses new commits, uncommitted changes and an
   unreadable working tree; `force` deliberately discards them. If work
   changes after done, automatic cleanup keeps the child and tells you.

## Rework

For merged work, prefer a fresh fork for rework: it starts from your latest merged state. For independent PRs, use the ref or PR workflow above.
For an existing `keep` child, `jailbee inbox send update --child C --wait`
transports your branch and tells the child which ref to merge. To send a
different source without switching your branch, add `--ref refs/heads/topic`
(or a full 40-hex commit or imported `refs/jailbee/from/...` ref), or use
`--base release/1` for a host-resolved base as with spawn `--new --base`.
`--ref` and `--base` are mutually exclusive. Explicit sources land at
`refs/jailbee/update/<orchestrator>/<manifest-id>`, separate from branch and
child-import refs; omitted selectors preserve today's branch transport.
Plain update leaves the child to merge it. Prefer `--merge` when the child is
idle or between tasks: the broker checks conflicts first, then fast-forwards,
creates a merge commit, or leaves an already-contained source unchanged. A dirty
worktree, merge in progress, detached HEAD, mount-mode or stopped child, or any
conflict (including conservatively detected gitlink conflicts) refuses the
request. There is no idle gate; coordinate with the child. A message with
`merged_head` means the branch has moved and the child must re-read files, not
merge the imported ref again. Use plain update while the child is mid-task.
For an idle RUNNING child, launch another session without repeating heavy startup:

```bash
jailbee inbox send restart --child fix-issue-12 --agent-only --agent claude --model haiku --on-done keep --text "Merge the imported ref, then implement the next task."
```

Only accepted `done` or an ended monitored agent (reported/reportable as
`blocked`) permits relaunch. A working child, uncertain liveness or active
startup worker is refused; never interrupt work or retry with force. The old
agent window ends; the fresh session gets the child preamble and new prompt.
Container boot, setup and every `on_start` action are skipped; services remain
running. Runner omission keeps the previous choice, explicit flags replace it,
and `--default-runner` selects the current default. `--on-done` is allowed;
`--autostart-profile` is incompatible. No additional slot or reservation is
needed, even at capacity, and agent-only restart never queues. It returns
`started`, then ready-and-alive `done` or startup `failed`; later unfinished
agent exit yields `blocked`. Without `--agent-only`, stop the child first;
ordinary restart runs boot/autostart and can queue.

To resume a conversation, find the child's latest session with
`jailbee inbox children` (or `children --json`), then restart with a new prompt:

```bash
jailbee inbox send restart --child fix-issue-12 --resume last --agent-only --text "Continue the task."
```

Use `--resume ID` for a specific session; only sessions recorded for that child
are accepted. Omit `--agent-only` for a stopped child, and optionally select a
runner using the same flags as above. The prompt becomes a new turn in the
resumed conversation. Unsupervised children do not record new sessions.

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
