---
name: jailbee-issue-management
description: Use when creating, editing, commenting on, labeling, closing, reopening, triaging, or organizing GitHub issues from inside a JailBee container — the container's `gh` is read-only, so every write is staged as a manifest in the issue outbox for a human to publish on the host. Trigger on "create an issue", "file a bug", "edit this issue", "update the issue title/body", "comment on issue #N", "add a label", "remove a label", "assign this issue", "assign it to me", "unassign", "close this issue", "reopen this issue", "triage these issues", "organize the issue backlog", "luo GitHub-issue", "muokkaa issueta", "kommentoi issueta", "lisää labeli", "poista labeli", "sulje issue", "avaa issue uudelleen", "järjestele issueja".
---

# Managing GitHub issues from inside a container — the outbox

**First:** If `JAILBEE_ROLE=child`, the role marker identifies you as a child,
or a staging command refuses you as a child, do not stage GitHub writes. Send
the orchestrator the exact repository, target, title, body and action content
with `jailbee inbox send message --text "..."`; use `--kind question` when you
need a result. Do not write GitHub outbox files or directories by hand.

You are inside a JailBee container working with GitHub issues (creating one,
editing a title or body, commenting, adding or removing labels, assigning or
unassigning people, closing, or reopening). Your `gh` here can **read** GitHub but must never **write** to
it: every issue action you produce is staged as a proposal in the host outbox
store through the `jailbee issue` commands, and a human on the host reviews and publishes it
later with `jb issue apply`. Nothing you write here reaches GitHub by
itself.

## Orchestrator publication

An orchestrator whose `jailbee inbox grant` output has matching
`publish.issues` (own repo/submodules), or `publish.foreign_issues` (listed
repos and kinds), stages normally and then runs
`jailbee inbox send publish --issue NAME.json --wait` instead of asking the
human to apply it. `started` is acceptance, not success: read the final status
for the printed ref and use its issue numbers/URLs. An uncertain outcome must
be resolved by the human through `jb outbox` on the host, never retried
automatically. Without coverage, keep the human handoff. Children never publish.

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
read-only token anyway — use the staging commands instead, every time.

## Read path

Use the container's own `gh` to see an issue's current state — never guess
at numbers, titles, or label spelling:

- `gh issue view <n> --json number,title,body,labels,assignees,state,stateReason` —
  inspect current fields. Commands capture expected state automatically.
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
on the host, the URL `.gitmodules` declares for it. `.` and submodule paths
are the trusted resolution path. Use `github:<owner>/<name>` for a repository
outside this project (the named exception, not an arbitrary GitHub slug). If
you are unsure whether a path is a submodule the host will accept, check `git
submodule status --recursive` from the superproject root — a path that isn't
listed there is not a valid `repo` value (unless you intend the `github:`
form).

Use `github:<owner>/<name>` only for a repository outside this project (an
upstream bug report). The human sees a foreign-repository warning and
confirms it separately. Leave `labels` empty, because GitHub silently drops
labels from users without triage rights. `issue_ref` works only within the
same `repo` value.

## Staging issue actions

Use `jailbee issue create --title "Title" --body "Details"`, or `edit`,
`comment`, `labels`, `assign` and `state` with an issue number. Use `--body-file PATH`
for long text and `--repo PATH` to select a repository. Commands stage the
proposal in the host outbox store; never write outbox files by hand. See the
normative [output schema](references/manifest-schema.md).

Assignment: `jailbee issue assign 42 --me` assigns the publishing account
(`@me`, resolved on the host at publish time), `--unassign-me` removes it,
and `--add LOGIN` / `--remove LOGIN` (repeatable) name other users. Give at
least one of these flags; a login already assigned on `--add`, or not
assigned on `--remove`, is refused before anything is staged. `--me` and
`--unassign-me` are exempt from those checks, because the container cannot
resolve `@me` to a login.

For related actions use `--batch NAME`, then `jailbee outbox submit NAME
--kind issue`. A create may set `--ref REF`; later actions in that same
batch target `@REF`. For edits, labels, assign and state on `@REF`, expected values
are derived automatically from the earlier create under the draft lock.
Inspect with `jailbee outbox ls` and `jailbee outbox show issue/NAME`;
discard a draft with `jailbee outbox discard NAME --kind issue`.

## Human inspection and approval

Tell the human they can inspect on the **host** with `jb outbox <container>`
(or `jb outbox browse <container>`) and
`jb outbox show <container> issue/NNN-topic.json`. A container named `ls`
needs `jb outbox browse ls`. The local Qt dashboard also has native Outbox
inspection; SSH browsing is read-only.

For human approval on the host, `jb outbox apply <container>
issue/NNN-topic.json --revision TOKEN` publishes all pending actions in that
one manifest, preserving the existing `expected`, journal and recovery
gates. `TOKEN` comes from inspection; a changed proposal requires refresh
and renewed inspection. The original `jb issue apply <container>` remains
available. These are instructions to the human, **not permission for you
to run host publication commands**. Stage with the commands only. Without
matching orchestrator publish rights, stop there. With those rights, use
`send publish` as described above; never call host apply or direct `gh`
mutations yourself.

## When you're done

If a broker `done` confirms publication, report its issue numbers and URLs.
If there is no final result yet, say publication is pending. For manifests
not sent through a granted orchestrator publish request, tell the user to
run `jb issue apply <container>` on the host: **nothing has been published
yet.** Everything still queued is a proposal
sitting in the host outbox store until then. `jailbee outbox sent` lists what
was published. The host also sees the
pending count without being told: `jb ls`'s ISSUES column shows `✉N` for
this container, and the dashboards' OUTBOX column counts it together with any staged PR actions.

## What not to assume

The outbox is not a log of what happened — it is a queue of what you'd
*like* to happen. A human may edit the issue's real state out from under a
pending manifest (the `expected` gate exists exactly for this), reject a
proposal outright, or ask you to fix it before it's applied. The only
record of what actually landed on GitHub is the host's sent log:
`jailbee outbox sent` (add `--json`, `--limit N` or `--kind issue`) lists
what the host published from this container, newest first, with each
action's state (`applied`, or `uncertain` while a human resolves it) and
GitHub URL. A published proposal also disappears from `jailbee outbox ls`.
Never assume a manifest you wrote has been published; check `jailbee outbox
sent` (or ask) instead.
