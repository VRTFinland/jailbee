---
name: jailbee-issue-management
description: Use when creating, editing, commenting on, labeling, closing, reopening, triaging, or organizing GitHub issues from inside a JailBee container — the container's `gh` is read-only, so every write is staged as a manifest in the issue outbox for a human to publish on the host. Trigger on "create an issue", "file a bug", "edit this issue", "update the issue title/body", "comment on issue #N", "add a label", "remove a label", "close this issue", "reopen this issue", "triage these issues", "organize the issue backlog", "luo GitHub-issue", "muokkaa issueta", "kommentoi issueta", "lisää labeli", "poista labeli", "sulje issue", "avaa issue uudelleen", "järjestele issueja".
---

# Managing GitHub issues from inside a container — the outbox

**First:** If `JAILBEE_ROLE=child`, the role marker identifies you as a child,
or a staging command refuses you as a child, do not stage GitHub writes. Send
the orchestrator the exact repository, target, title, body and action content
with `jailbee inbox send message --text "..."`; use `--kind question` when you
need a result. Do not write GitHub outbox files or directories by hand.

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
read-only token anyway — use the staging commands instead, every time.

## Read path

Use the container's own `gh` to see an issue's current state — never guess
at numbers, titles, or label spelling:

- `gh issue view <n> --json number,title,body,labels,state,stateReason` —
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
`comment`, `labels` and `state` with an issue number. Use `--body-file PATH`
for long text and `--repo PATH` to select a repository. Commands create
`~/.jailbee/issue-outbox/` as needed; never write it manually. See the
normative [output schema](references/manifest-schema.md).

For related actions use `--batch NAME`, then `jailbee outbox submit NAME
--kind issue`. A create may set `--ref REF`; later actions in that same
batch target `@REF`. For edits, labels and state on `@REF`, expected values
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
to run publication commands**. Continue to stage with commands only; never call
host apply or direct `gh` mutations yourself.

## When you're done

Tell the user, verbatim: run `jb issue apply <container>` on the host —
**nothing has been published yet.** Everything you wrote is a proposal
sitting in the container's filesystem until then. The host also sees the
pending count without being told: `jb ls`'s ISSUES column shows `✉N` for
this container, and the dashboards' OUTBOX column counts it together with any staged PR actions.

## What not to assume

The outbox is not a log of what happened — it is a queue of what you'd
*like* to happen. A human may edit the issue's real state out from under a
pending manifest (the `expected` gate exists exactly for this), reject a
proposal outright, or ask you to fix it before it's applied. The only
record of what actually landed on GitHub is
`~/.jailbee/issue-outbox/applied.log`, written by the host after a
successful `jb issue apply`. Never assume a manifest you wrote has been
published; check that log (or ask) instead.
