# Issue tracker: GitHub (through the JailBee issue outbox)

Issues and specs for this repo live as GitHub issues on `VRTFinland/jailbee`.

Development happens inside JailBee containers, where `gh` is **read-only**.
Read with `gh`; every write is staged with `jailbee issue ...` into the issue
outbox and published from the host (or by an orchestrator whose grant covers
it). Never run `gh issue create|edit|comment|close|reopen` or a mutating
`gh api` call. The `jailbee-issue-management` skill has the full rules.

## Conventions

- **Read an issue**: `gh issue view <number> --json number,title,body,labels,assignees,state,comments`.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Label spelling**: `gh label list --json name`.
- **Create an issue**: `jailbee issue create --title "..." --body-file PATH` (or `--body "..."` for short text).
- **Comment on an issue**: `jailbee issue comment <number> --body "..."`.
- **Apply / remove labels**: `jailbee issue labels <number> --add LABEL` / `--remove LABEL`.
- **Assign yourself**: `jailbee issue assign <number> --me` (`--unassign-me` to undo).
- **Close / reopen**: `jailbee issue state <number> closed --reason completed` / `jailbee issue state <number> open`.
- **Related actions**: stage them in one batch with `--batch NAME`; a create may
  set `--ref REF` and later actions in the batch target `@REF`. Submit with
  `jailbee outbox submit NAME --kind issue`.
- **Sub-issues and dependencies** need mutating `gh api` calls, which the
  outbox does not stage. Put `Part of #<parent>` / `Blocked by: #<n>, #<n>` at
  the top of the child body instead, and ask the human if the native GitHub
  links are wanted.

`--repo .` (the default) is this repository; never use `owner/repo` there.

## Publishing

- **Orchestrator with a grant**: if `jailbee inbox grant` reports that
  `publish.issues` covers every pending action kind in the manifest (currently
  `create`, `comment`, `state`, `assign_self` — **not** labels), run
  `jailbee inbox send publish --issue NAME.json --wait` and read the final
  `done`/`failed` status for the created issue numbers. `started` is not
  success; an uncertain result goes to the human, never an automatic retry.
- **Otherwise**: leave the manifest staged and tell the human to inspect and
  publish it on the host with `jb outbox <container>` / `jb outbox apply`.
- **Child containers never publish**; they send the exact content to the
  orchestrator.

## Local tickets

Umbrella issues and specs live on GitHub. The implementable tickets that
`/to-tickets` splits a spec into live in `.local/tickets/`, so a breakdown of a
dozen or more tickets does not flood the tracker or need host-side label
publishing. `.local/` is the host repo's directory, mounted into every
container of the repo, so the tickets are shared across containers.
`.local/tickets/` is its own local-only git repository: never add a remote.

- **Layout**: one directory per spec, `<spec-issue>-<slug>/`, holding a
  `README.md` index (parent issue, ticket table) and one file per ticket,
  `NN-<slug>.md`, numbered from `01` in dependency order (blockers first).
- **Ticket file**: the `/to-tickets` local template, plus a `**Parent:** #N`
  line naming the spec issue. `**Blocked by:**` lists ticket numbers in the
  same directory.
- **Status**: the `**Status:**` line is `ready-for-agent`,
  `in-progress (<container>)` or `done (<commit>)`, where `<commit>` is the
  jailbee commit that completes it.
- **Frontier**: every `ready-for-agent` ticket whose blockers are all `done`.
- **Claim / finish**: edit the status line, then commit inside
  `.local/tickets/` with `git commit -- <path>`. Several containers share the
  repository; if the index lock is held, wait and retry.
- **When the spec is done**: close the spec issue on GitHub as in
  **Publishing** below; the ticket files stay as the record.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues: read
with `gh pr view <number> --comments` and `gh pr diff <number>`; stage writes
through the PR review outbox (`jailbee-pr-review` skill).

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Stage a `jailbee issue create`, then publish it as in **Publishing** above.

## When a skill says "fetch the relevant ticket"

A spec or umbrella issue: read it as in **Read an issue** above. A ticket from
`/to-tickets`: read its file under `.local/tickets/` (see **Local tickets**).

## When `/to-tickets` says "publish the tickets"

Use its local-files form, under `.local/tickets/<spec-issue>-<slug>/` as in
**Local tickets** above, not GitHub issues.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body.
- **Child ticket**: an issue with `Part of #<map>` at the top of its body, listed in a task list in the map body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`).
- **Blocking**: a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's task list), drop any with an open blocker or an assignee; first in map order wins.
- **Claim**: `jailbee issue assign <n> --me`, the session's first write.
- **Resolve**: `jailbee issue comment <n> --body "<answer>"`, then `jailbee issue state <n> closed`, then append a context pointer (gist + link) to the map's Decisions-so-far.

Labels cannot be published by the orchestrator grant, so `wayfinder:*` labels
are staged for the human (and the labels themselves must first exist in the
repo — creating them is a host step).
