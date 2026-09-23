# Issue outbox — manifest schema v1 (normative)

This document is the authoritative contract for every file written into
`~/.jailbee/issue-outbox/`. The host-side parser (`jb issue apply`/`ls`/
`show`/`drop`/`resolve`) matches this document field for field — if you are
writing a manifest by hand, follow it exactly rather than guessing from the
examples.

```json
{
  "version": 1,
  "actions": [
    {"type": "create", "repo": ".", "ref": "new-bug", "title": "…", "body": "…", "labels": ["bug"]},
    {"type": "edit", "repo": ".", "issue": 42, "title": "…",
     "expected": {"title": "…"}},
    {"type": "comment", "repo": ".", "issue_ref": "new-bug", "body": "…"},
    {"type": "labels", "repo": ".", "issue": 42, "add": ["needs-triage"], "remove": [],
     "expected": {"labels": ["bug"]}},
    {"type": "state", "repo": ".", "issue": 42, "state": "closed", "reason": "completed",
     "expected": {"state": "open"}}
  ]
}
```

Unlike the PR review outbox, there is **no top-level `repo`** — each action
names its own `repo`, so one manifest may freely mix the superproject and
any number of declared submodules.

## Envelope fields

| Field | Rule |
|---|---|
| `version` | Must be `1`. Anything else is refused by name. |
| `actions` | Non-empty list, applied in order. At most 50 entries per manifest. |

No other top-level field is allowed; an unknown key is refused, naming it.

## `repo` (every action)

`"."` for the superproject, or a submodule's path relative to the
superproject root, normalized POSIX-relative (no leading `/`, no `\`, no `.`
or `..` segment). **Never `owner/repo`** — the host alone resolves the
actual GitHub repository, from git remotes it already trusts, and refuses
any `repo` value it does not recognize as `.` or a declared submodule.

## Targeting an issue: `issue` vs `issue_ref`

Every action except `create` targets an issue with **exactly one** of:

| Field | Rule |
|---|---|
| `issue` | A positive integer — an issue that already exists. |
| `issue_ref` | The `ref` string of an earlier `create` action **in this same manifest**, targeting the issue that create is about to make. Must name a create in the *same* `repo`. A `ref` does not resolve across manifest files. |

## Body fields

Every action that carries prose (`create`, `edit`'s `body`, `comment`) uses
exactly one of `body` (inline string) or `body_file` (a plain file name at
the outbox root — no `/`, no `..`, not `.` or `..`). Both, or neither when
one is required, is a validation error naming the action index. Any single
body — inline or via `body_file` — is capped at 64 KiB.

## `type: "create"`

| Field | Rule |
|---|---|
| `repo` | As above. |
| `ref` | A manifest-local identifier for this new issue (unique within the manifest), used by later actions' `issue_ref`. Not sent to GitHub. |
| `title` | Non-empty string. Required. |
| `body` / `body_file` | Required (exactly one). |
| `labels` | Optional list of non-empty, non-duplicate (case-insensitive) strings. Default `[]`. |

No `expected` block — there is nothing to be stale against yet.

## `type: "edit"`

Changes `title` and/or `body` (at least one is required — an edit that
changes neither is refused).

| Field | Rule |
|---|---|
| `repo`, `issue`/`issue_ref` | As above. |
| `title` | Optional non-empty string — the new title. |
| `body` / `body_file` | Optional — the new body (exactly one of the two if present). |
| `expected.title` | Required **if and only if** `title` is present — the title `gh issue view` showed before this edit, or `null`. |
| `expected.body` | Required **if and only if** `body`/`body_file` is present — the body `gh issue view` showed before this edit, or `null`. |

`expected` carries no other keys, and never a field you are not changing —
an `expected.title` on an edit that only changes `body` is refused.

## `type: "comment"`

| Field | Rule |
|---|---|
| `repo`, `issue`/`issue_ref` | As above. |
| `body` / `body_file` | Required (exactly one); must resolve to a non-empty string. |

No `expected` block — a comment never conflicts with the issue's current
state.

## `type: "labels"`

Adds and/or removes existing labels; **never creates a new label** — a
label name in `add` must already exist on the repository (check with
`gh label list` first; an unknown label is refused, naming it).

| Field | Rule |
|---|---|
| `repo`, `issue`/`issue_ref` | As above. |
| `add` | Labels to add. Default `[]`. |
| `remove` | Labels to remove. Default `[]`. At least one of `add`/`remove` must be non-empty. A label cannot appear in both. |
| `expected.labels` | Required — the **complete** current label list `gh issue view` showed, not just the ones being touched. Every `remove` entry must be present in it; every `add` entry must be absent from it. |

The host computes the final label set itself (`expected.labels` minus
`remove` plus `add`, using the repository's canonical label casing) and
sends one full replacement — there is no separate "add" and "remove" call
on GitHub's side.

## `type: "state"`

Closes or reopens an issue.

| Field | Rule |
|---|---|
| `repo`, `issue`/`issue_ref` | As above. |
| `state` | `"open"` or `"closed"`. |
| `reason` | Required, `"completed"` or `"not_planned"`, **only** when `state` is `"closed"`. Forbidden when reopening. |
| `expected.state` | Required — the current state `gh issue view` showed. Must differ from `state` (an action that "changes" state to what it already is is refused). |

## One field, one action, per target

Within a single manifest, two actions may not both touch the same field of
the same issue (identified by `issue` number, or by the same `issue_ref`) —
e.g. two `edit` actions both changing `#42`'s title is refused when the
manifest is parsed. The refusal names only the *later* action's own index
(`"<manifest> action <n>: target already changes field 'title'"`) — it does
not say which earlier action first touched that field, so if a manifest has
several actions on the same target, check all of them. Split the conflicting
actions across manifests, or fold the change into a single action, instead.

## Caps (refusal, not truncation)

- Manifest file: ≤ 256 KiB
- Any single body (`body` or the file behind `body_file`, including
  `expected.body`): ≤ 64 KiB
- `actions`: ≤ 50 entries per manifest
- Pending manifests per container: ≤ 20
- Selected manifests offered to one `jb issue apply` run: ≤ 100 actions
  total, across every manifest selected

The caps exist so a runaway agent cannot produce a plan no human can read or
a payload `gh` chokes on. A manifest that exceeds any of these is refused
outright, naming the manifest file and which cap it broke.

## Worked example 1 — a multi-issue superproject batch

`~/.jailbee/issue-outbox/001-timeout-bugs.json`: file a new bug, then
immediately comment and label it, plus edit and close an unrelated existing
issue — all in the superproject (`repo: "."`).

```json
{
  "version": 1,
  "actions": [
    {
      "type": "create",
      "repo": ".",
      "ref": "timeout-bug",
      "title": "Requests to /sync time out after 30s under load",
      "body_file": "001-timeout-bug-body.md",
      "labels": ["bug"]
    },
    {
      "type": "comment",
      "repo": ".",
      "issue_ref": "timeout-bug",
      "body": "Reproduced locally with `ab -n 1000 -c 50`; the connection pool exhausts before the timeout fires."
    },
    {
      "type": "labels",
      "repo": ".",
      "issue_ref": "timeout-bug",
      "add": ["needs-triage"],
      "remove": [],
      "expected": {"labels": ["bug"]}
    },
    {
      "type": "edit",
      "repo": ".",
      "issue": 118,
      "title": "Dashboard CPU column shows 0% for frozen containers",
      "expected": {"title": "Dashboard CPU column is wrong for frozen containers"}
    },
    {
      "type": "state",
      "repo": ".",
      "issue": 118,
      "state": "closed",
      "reason": "completed",
      "expected": {"state": "open"}
    }
  ]
}
```

`~/.jailbee/issue-outbox/001-timeout-bug-body.md`:

```markdown
## What happens

Requests to `/sync` start timing out at ~30s once concurrency passes 50,
even though individual downstream calls complete in under 200ms.

## Suspected cause

The connection pool (`src/sync/pool.py`) is sized to 20 and blocks on
acquire with no queue timeout of its own — callers pile up behind the
30s HTTP-level timeout instead of failing fast.

## Repro

`ab -n 1000 -c 50 http://localhost:8080/sync` against a local checkout.
```

Notes on this example:

- `timeout-bug` is a manifest-local `ref` — the comment and labels actions
  target it with `issue_ref` because the real issue number does not exist
  until `create` runs.
- The label action's `expected.labels` lists the label set as it will be
  right after `create` applies (`["bug"]`), not the empty set — a `labels`
  action targeting a `create` in the same manifest must still account for
  that create's own `labels`.
- The `edit` and `state` actions target `#118`, an issue that already
  exists, by number — both carry the exact `title`/`state` `gh issue view
  118` showed beforehand.

## Worked example 2 — a mixed superproject/submodule triage batch with local refs

`~/.jailbee/issue-outbox/002-triage.json`: reopen a wrongly-closed
superproject issue, and separately file, then label, a new issue in a
submodule — a `ref` in one repo's actions never resolves against another
repo's `create`.

```json
{
  "version": 1,
  "actions": [
    {
      "type": "state",
      "repo": ".",
      "issue": 203,
      "state": "open",
      "expected": {"state": "closed"}
    },
    {
      "type": "comment",
      "repo": ".",
      "issue": 203,
      "body": "Reopening: the regression came back in 1.5.0 with a different stack trace, see the linked build log."
    },
    {
      "type": "create",
      "repo": "packages/lib",
      "ref": "lib-parse-crash",
      "title": "Parser crashes on empty trailing comment block",
      "body": "`parse_config(\"a: 1\\n#\")` raises `IndexError` instead of treating the trailing `#` as an empty comment.",
      "labels": ["bug"]
    },
    {
      "type": "labels",
      "repo": "packages/lib",
      "issue_ref": "lib-parse-crash",
      "add": ["needs-triage"],
      "remove": [],
      "expected": {"labels": ["bug"]}
    }
  ]
}
```

Notes on this example:

- `repo: "packages/lib"` must be a submodule path the host already knows
  about (declared in `.gitmodules` and visible in `git submodule status`)
  — an arbitrary path, or an attempt to write `owner/repo` directly, is
  refused.
- `lib-parse-crash` is scoped to its own `create` action's `repo`
  (`packages/lib`); the earlier `state`/`comment` pair on `#203` uses `repo:
  "."` and plain `issue` numbers, because that issue already exists in the
  superproject. The two `repo` values never share a `ref` namespace.
- Reopening `#203` and commenting on it are two separate actions because
  they touch different fields (`state` vs. a comment, which never has an
  `expected` block) — this is allowed even though both target the same
  issue, since neither field is touched twice.
- `expected.state` on the reopen is `"closed"` — the state `gh issue view
  203` showed just before this manifest was written — and `state: "open"`
  carries no `reason`, since `reason` is only valid (and required) when
  closing.
