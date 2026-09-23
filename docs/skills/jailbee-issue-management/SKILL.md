---
name: jailbee-issue-management
description: Use when creating, editing, commenting on, labeling, closing, reopening, triaging, or organizing GitHub issues from inside a JailBee container — the container's `gh` is read-only, so every write is staged as a manifest in the issue outbox for a human to publish on the host. Trigger on "create an issue", "file a bug", "edit this issue", "update the issue title/body", "comment on issue #N", "add a label", "remove a label", "close this issue", "reopen this issue", "triage these issues", "organize the issue backlog", "luo GitHub-issue", "muokkaa issueta", "kommentoi issueta", "lisää labeli", "poista labeli", "sulje issue", "avaa issue uudelleen", "järjestele issueja".
---

# Managing GitHub issues from inside a container — the outbox

You are inside a JailBee container working with GitHub issues (creating one,
editing a title or body, commenting, adding or removing labels, closing, or
reopening). Your `gh` here can **read** GitHub but must never **write** to
it: every issue action you produce is written as a JSON manifest file into a
fixed outbox directory, and a human on the host reviews and publishes it
later with `jb issue apply`. Nothing you write here reaches GitHub by
itself.

## Never write to GitHub directly — use the outbox instead

**Never run** any of the following inside this container:

```text
gh issue create
gh issue edit
gh issue comment
gh issue close
gh issue reopen
gh api -X POST|PATCH|PUT|DELETE
GraphQL mutations
```

The token available here is read-only by design (Contents, Issues, Pull
requests, and Metadata all scoped to Read), and even if it weren't, every
write goes through the outbox so a human sees the exact change before it
becomes public. Attempting any of the commands above will fail against a
read-only token anyway — write a manifest instead, every time.

## Read path

Use the container's own `gh` to see an issue's current state — never guess
at numbers, titles, or label spelling:

- `gh issue view <n> --json number,title,body,labels,state,stateReason` — an
  existing issue's current fields. **Read this before every `edit`,
  `labels`, or `state` action** and copy the exact current
  title/body/labels/state into that action's `expected` block — the host
  refuses to *apply the whole batch you selected* (every manifest in that
  `jb issue apply` run, not just this one action) if any `expected` no
  longer matches what is actually on GitHub, so a stale or guessed value
  aborts the run rather than silently overwriting someone else's change. A
  `comment` action has no `expected` block and no staleness check, but read
  the issue first anyway so your comment text is accurate.
- `gh issue list --json number,title,labels,state [--label ...] [--state ...]`
  — for finding or triaging a set of issues.
- `gh label list --json name` — the repository's exact label spelling and
  casing, so `add`/`remove` names match (GitHub label names are
  case-preserving but the host also folds case for comparison).

## Which repository

Every action names a `repo` field: `"."` for the superproject, or a
submodule's path relative to the superproject root (e.g. `"packages/lib"`),
exactly as it appears in the repository layout — **never** `owner/repo`.
The host resolves the actual GitHub repository itself, from git remotes it
already trusts: the superproject's own configured upstream remote for `.`,
and for a submodule, the path declared in `.gitmodules` together with that
submodule's own upstream remote when it is checked out on the host — detected
by a five-step fallback (the sole remote, then `origin`, then
`remote.pushDefault`, then the current branch's tracked remote, then a
uniquely-identifiable `refs/remotes/<r>/HEAD`), or, if it isn't checked out
on the host, the URL `.gitmodules` declares for it. A manifest can only
target `.` or a path the host recognizes there, never an arbitrary GitHub
repository. If you are unsure whether a path is a submodule the host will
accept, check `git submodule status --recursive` from the superproject
root — a path that isn't listed there is not a valid `repo` value.

## Writing a manifest

- Fixed path: `~/.jailbee/issue-outbox/` inside this container.
- One manifest per file, named `NNN-<topic>.json` (e.g. `001-triage.json`,
  `002-bugfix-issue.json`) so the numeric prefix makes the intended order
  visible — manifests are, by default, applied in filename order.
- Prose longer than roughly 20 lines does not belong inline in the JSON —
  put it in a sibling `.md` file at the outbox root and reference it with
  `body_file` instead of `body`.
- **Read the current state first, always.** Every mutating action
  (`edit`, `labels`, `state`) carries an `expected` block that must match
  what `gh issue view` shows *right now* — title/body for `edit`, the full
  current label set for `labels`, the current state for `state`. Copy those
  values exactly; do not paraphrase or guess them. This is not busywork: it
  is the host's only defense against applying your proposal on top of an
  issue someone else changed in the meantime.
- A brand-new issue you plan to comment on, label, or edit further **within
  the same manifest** does not have a number yet — give its `create` action
  a local `ref` (e.g. `"ref": "new-timeout-bug"`) and point later actions in
  that *same manifest* at it with `"issue_ref": "new-timeout-bug"` instead
  of `"issue"`. A `ref` only resolves within the manifest file that defines
  it — it cannot be referenced from a different manifest.
- The manifest format itself — envelope fields, every action type,
  defaults, and the caps — is normative and lives in
  [`references/manifest-schema.md`](references/manifest-schema.md). Read it
  before writing a manifest by hand; do not guess at field names or shapes.

## When you're done

Tell the user, verbatim: run `jb issue apply <container>` on the host —
**nothing has been published yet.** Everything you wrote is a proposal
sitting in the container's filesystem until then.

## What not to assume

The outbox is not a log of what happened — it is a queue of what you'd
*like* to happen. A human may edit the issue's real state out from under a
pending manifest (the `expected` gate exists exactly for this), reject a
proposal outright, or ask you to fix it before it's applied. The only
record of what actually landed on GitHub is
`~/.jailbee/issue-outbox/applied.log`, written by the host after a
successful `jb issue apply`. Never assume a manifest you wrote has been
published; check that log (or ask) instead.
