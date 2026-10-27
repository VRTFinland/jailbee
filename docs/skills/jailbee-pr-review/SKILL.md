---
name: jailbee-pr-review
description: Use when reviewing a GitHub pull request from inside a JailBee container and proposing comments, replies, or a description rewrite — the container's `gh` is read-only, so every write is staged as a manifest in the PR review outbox for a human to publish on the host. Trigger on "review this PR", "comment on line", "reply to this review comment", "post my review", "update the PR description", "katselmoi tämä PR", "kommentoi riviä", "vastaa kommenttiin", "päivitä PR:n kuvaus".
---

# Reviewing a PR from inside a container — the outbox

You are inside a JailBee container reviewing a pull request (typically one
created with `jailbee new --pr <n>`). Your `gh` here can **read** GitHub but
must never **write** to it: every review comment, reply, general comment, or
description you produce is written as a JSON manifest file into a fixed
outbox directory, and a human on the host reviews and publishes it later with
`jb review apply`. Nothing you write here reaches GitHub by itself.

## Read path

Use the container's own `gh` to see the PR's current state — never guess at
IDs or line numbers:

- `gh pr view <n> --json number,title,headRefOid,baseRefName,body` — the PR's
  basic facts.
- `gh api repos/{owner}/{repo}/pulls/<n>/comments` — the existing **line**
  (review) comments. This is where the `comment_id` values you need for a
  `reply` action come from.
- `gh api repos/{owner}/{repo}/issues/<n>/comments` — the existing **general**
  (issue-level) comments, for a `comment` action's `reply_to`.
- `gh pr diff <n>` — the diff. Every line comment you write must anchor to a
  line that is actually part of this diff, or GitHub rejects it.

## Never write to GitHub directly — use the outbox instead

**Never run** `gh api -X POST`, `gh api -X PATCH`, `gh api -X PUT`,
`gh api -X DELETE`, `gh pr comment`, `gh pr review`, or `gh pr edit` inside
this container. The token available here is read-only by design, and even if
it weren't, every write goes through the outbox so a human sees the exact
text before it becomes public. Posting straight from the container would
skip that gate entirely — write a manifest instead, every time.

## Finding the PR number

1. Use the number the user gave you.
2. Otherwise, resolve it from the checked-out commit:
   `gh api repos/{owner}/{repo}/commits/$(git rev-parse HEAD)/pulls`.
3. Otherwise, ask the user.

A `jb new --pr` clone sits on a detached PR-head commit, so a bare
`gh pr view` (with no argument) may not resolve to the right PR — don't rely
on it alone.

For a numbered manifest targeting this container's PR, ask the host user to
bind it with `jailbee pr bind <container> N` if it is not already bound. Forks
inherit their source's PR binding. Other same-repository targets may be
published through the host's explicit foreign-target confirmation; the binding
is not a blanket requirement for every outbox publication.

## Finding `head_sha`

Use `gh pr view <n> --json headRefOid` — the sha as GitHub itself reports the
PR's head, not `git rev-parse HEAD`. The manifest's `head_sha` records the
commit your line comments are anchored against; the host checks it for
staleness before publishing a `review` action.

## Writing a PR description worth using

A `description` action is not busywork: when this container's branch is
published with `jb pr push`, a pending description manifest **replaces the
in-container Claude run** that would otherwise write the title and body. So
write it the way that run would:

`repo` may name the superproject or any submodule repository. Inside the
container, read a submodule's real upstream with
`git -C <subpath> remote get-url origin`. A matching description is consumed
by `jb submodule pr push` for that submodule; its comments remain publishable with
`jb review apply`.

- First, `gh pr view --json number,title` for this branch. A PR that already
  exists goes in the manifest's `pr` field; only a branch with no PR at all
  gets `pr: null`. `jb pr push` will not apply a `pr: null` description to a PR it
  did not open itself, and an existing PR is often exactly that — one the user
  opened elsewhere and bound on the host with `jb pr bind <container> N`.
- Set the `description` action's `title` to the intended PR title as a JSON
  string, separate from `body`/`body_file`. For a new PR, write a concise
  title based on the change; for a body-only update, copy the current title
  from `gh pr view <n> --json title`. With `jb pr push`, a missing, `null`, or
  blank title is replaced by the body's first non-blank line (without leading
  Markdown `#` markers), even when updating an existing PR. A body opening
  with `Why?` would therefore rename the PR to `Why?`.
- Read `git log <base>..HEAD` and `git diff <base>...HEAD` for the commits
  and cumulative diff.
- Follow `.github/pull_request_template.md`, or a file under
  `.github/PULL_REQUEST_TEMPLATE/`, heading by heading, if the repo has one.
- Find the spec, plan, or issue the branch implements (look in `docs/`,
  `specs/`, commit messages) and describe the change against that stated
  intent — say plainly what it deliberately leaves out.
- Read `CONTRIBUTING.md`, `CLAUDE.md`, `AGENTS.md` for the repository's own
  rules on commits and pull requests.
- `gh issue view <n>` for a referenced issue; add `Closes #<n>` only when
  merging this PR really does close it.
- Propose a head branch name that follows the repo's existing convention —
  it becomes the manifest's `branch` field.

**One rule is deliberately different here: running the test suite is
allowed.** The in-container agent run behind `jb pr push` forbids it only
because that run has a time budget (`pr.timeout`, 600 s by default) and a
test suite can eat all of it. Writing to the outbox has no such budget — run whatever you need to
describe the change accurately.

## The outbox contract

- Fixed path: `~/.jailbee/pr-outbox/` inside this container.
- One manifest per file, named `NNN-<topic>.json` (e.g. `001-review.json`,
  `002-description.json`) so the numeric prefix makes the intended order
  visible.
- Prose longer than roughly 20 lines does not belong inline in the JSON —
  put it in a sibling `.md` file at the outbox root and reference it with
  `body_file` instead of `body`.
- The manifest format itself — envelope fields, every action type, defaults,
  and the caps — is normative and lives in
  [`references/manifest-schema.md`](references/manifest-schema.md). Read it
  before writing a manifest by hand; do not guess at field names or shapes.

New containers already have both `~/.jailbee/pr-outbox/` and
`~/.jailbee/issue-outbox/`, created as the container user before agents run.
This does not retroactively create them in older containers; idempotent
`mkdir -p ~/.jailbee/pr-outbox ~/.jailbee/issue-outbox` is permitted when
staging there. Inspection never bootstraps missing stores.

## Human inspection and approval

Tell the human they can inspect on the **host** with `jb outbox <container>`
(or `jb outbox browse <container>`) and
`jb outbox show <container> pr/NNN-topic.json`. A container named `ls` needs
`jb outbox browse ls`. The local Qt dashboard also has native Outbox
inspection; SSH browsing is read-only.

For human approval on the host, `jb outbox apply <container>
pr/NNN-topic.json --revision TOKEN` publishes all pending actions in that
one manifest, not only a highlighted comment. `TOKEN` comes from inspection;
a changed proposal requires refresh and renewed inspection. The original
`jb review apply <container>` remains available. These are instructions to
the human, **not permission for you to run publication commands**. Continue
to stage files only; never call host apply or direct `gh` mutations yourself.

## When you're done

Tell the user, verbatim: run `jb review apply <container>` on the host —
**nothing has been published yet.** Everything you wrote is a proposal sitting
in the container's filesystem until then.

## What not to assume

The outbox is not a log of what happened — it is a queue of what you'd
*like* to happen. A human may edit the repo's remote state out from under a
pending manifest, reject it outright, or ask you to fix it before it's
applied. The only record of what actually landed on GitHub is
`~/.jailbee/pr-outbox/applied.log`, written by the host after a successful
`jb review apply`. Never assume a manifest you wrote has been published;
check that log (or ask) instead.
