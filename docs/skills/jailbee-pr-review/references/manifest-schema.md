# PR review outbox — manifest schema v1 (normative)

This document is the authoritative contract for every file written into
`~/.jailbee/pr-outbox/`. The host-side parser (`jb review apply`/`ls`/`show`/
`drop`) matches this document field for field — if you are writing a
manifest by hand, follow it exactly rather than guessing from the examples.

```json
{
  "version": 1,
  "repo": "owner/name",
  "pr": 1234,
  "head_sha": "abc1234def…",
  "actions": [
    {
      "type": "review",
      "event": "COMMENT",
      "body_file": "001-summary.md",
      "comments": [
        {"path": "src/worktime.py", "line": 88, "side": "RIGHT", "body": "…"},
        {"path": "src/worktime.py", "start_line": 120, "line": 134,
         "body_file": "001-comment-worktime.md"}
      ]
    },
    {"type": "reply", "comment_id": 99887, "body": "…"},
    {"type": "comment", "body_file": "001-general.md", "reply_to": 4455},
    {"type": "description", "body_file": "001-body.md", "title": null, "branch": null}
  ]
}
```

## Envelope fields

| Field | Rule |
|---|---|
| `version` | Must be `1`. Anything else is refused by name ("manifest version 2 needs a newer JailBee"). |
| `repo` | `owner/name`. Checked against the host's origin (gate 1). |
| `pr` | Integer, checked against the container's own PR (gate 2) — or `null`, meaning "the PR `jb pr` is about to open from this container". A `pr: null` manifest may contain **only** a single `description` action: comments need a PR that exists and a diff to anchor to. |
| `head_sha` | Full sha as GitHub reports `headRefOid`. Checked for staleness (gate 3), which applies only to `review` actions. `null` is allowed on a `pr: null` manifest. |
| `actions` | Non-empty list, applied in order. |

## Body fields

Every action and every line comment carries exactly one of `body` (inline
string) or `body_file` (path relative to the outbox root — a plain file
name, no `/`, no `..`). Both, or neither, is a validation error naming the
action index.

## `type: "review"`

One `POST /repos/{owner}/{repo}/pulls/{n}/reviews` with `commit_id` =
`head_sha`, `body` = the summary, `event` = `COMMENT`, and `comments[]`. All
line comments in one manifest therefore land as a *single* review: one
atomic API call, one notification, one collapsible block in the PR. At most
one `review` action per manifest.

Each entry in `comments[]`:

| Field | Rule |
|---|---|
| `path` | Repo-relative path, as it appears in the PR diff. No absolute paths, no `..`. |
| `line` | The line in the *new* file (or old, with `side: "LEFT"`). Required. |
| `start_line` | Optional; makes the comment span `start_line`..`line`. Must be `< line`. |
| `side` / `start_side` | `"RIGHT"` (default) or `"LEFT"`. |
| `body` / `body_file` | As above. |

GitHub rejects a line that is not part of the diff with 422. The host does
not pre-validate against the diff in v1 — the failure is reported as-is,
with the file and line named, and the whole review call fails atomically, so
nothing partial lands.

## `type: "reply"`

`POST /repos/{owner}/{repo}/pulls/{n}/comments/{comment_id}/replies`.
`comment_id` is a review-comment id you read via `gh api`. Replies are
separate calls, one per action, and are applied after the review.

## `type: "comment"`

`POST /repos/{owner}/{repo}/issues/{n}/comments`. Optional `reply_to` is an
*issue-comment* id. GitHub has no threading for issue comments, so
`reply_to` is not an API parameter: the host prepends

```
> [Replying to this comment](https://github.com/{owner}/{name}/pull/{n}#issuecomment-{id})
```

to the body. The permalink is constructed, not fetched. `reply_to` is
recorded in the plan output as "reply to general comment #<id>" so the user
sees the intent, not just a link.

## `type: "description"`

The existing `pr.edit_pr`, i.e. `gh pr edit`, when applied by
`jb review apply`; consumed directly by `jb pr` when that command runs first.
`body_file`/`body` replaces the whole description. `title` and `branch` are
optional; `null` means "unchanged" on the update path. `branch` is the
proposed head branch name and is used **only** on the `jb pr` create path —
`jb review apply` ignores it, because renaming the head of an existing PR is
not a description edit. At most one `description` action per manifest.

## Caps (refusal, not truncation)

- Manifest file: ≤ 256 KB
- Any single body (`body` or the file behind `body_file`): ≤ 64 KB
- `comments[]`: ≤ 100 entries
- `actions`: ≤ 50 entries
- Pending manifests per container: ≤ 20

The caps exist so a runaway agent cannot produce a plan no human can read or
a payload `gh` chokes on. A manifest that exceeds any of these is refused
outright, naming the manifest file and which cap it broke.

## Worked example 1 — a review pass with three line comments

`~/.jailbee/pr-outbox/001-review.json`:

```json
{
  "version": 1,
  "repo": "acme/widgets",
  "pr": 1234,
  "head_sha": "abc1234def5678901234567890123456789012",
  "actions": [
    {
      "type": "review",
      "event": "COMMENT",
      "body": "Two blocking findings, one nit. Details inline.",
      "comments": [
        {
          "path": "src/worktime.py",
          "line": 88,
          "side": "RIGHT",
          "body": "This rounds half-down where the spec says half-up — off by one minute on the boundary."
        },
        {
          "path": "src/worktime.py",
          "start_line": 120,
          "line": 134,
          "side": "RIGHT",
          "body_file": "001-comment-worktime.md"
        },
        {
          "path": "src/worktime.py",
          "line": 200,
          "side": "RIGHT",
          "body": "Nit: this variable name shadows the outer `total`."
        }
      ]
    }
  ]
}
```

`~/.jailbee/pr-outbox/001-comment-worktime.md` (referenced by the
`start_line`..`line` span above, because the explanation runs long):

```markdown
Extract this into a helper; the loop body duplicates the rounding logic from
`compute_daily_total` above almost verbatim. Two call sites already drifted
from each other (this one uses `//`, the other uses `round()`), which is
exactly the kind of bug a shared helper prevents.
```

Notes on this example:

- All three comments ride in **one** `review` action, so they post as a
  single GitHub review rather than three separate notifications.
- The first and third comments use inline `body`; the second uses
  `body_file` because the explanation is longer than a one-liner.
- The second comment is a span comment (`start_line: 120` to `line: 134`);
  the first and third are single-line (`side` defaults to `"RIGHT"` and is
  given here only for clarity).

## Worked example 2 — a `pr: null` description manifest

Written before the PR exists yet, to hand `jb pr` a title, body, and
proposed head branch instead of running its own Claude pass:

`~/.jailbee/pr-outbox/002-description.json`:

```json
{
  "version": 1,
  "repo": "acme/widgets",
  "pr": null,
  "head_sha": null,
  "actions": [
    {
      "type": "description",
      "title": "fix(worktime): round half-up at the minute boundary",
      "body_file": "002-body.md",
      "branch": "fix/worktime-half-up-rounding"
    }
  ]
}
```

`~/.jailbee/pr-outbox/002-body.md`:

```markdown
## Background

`compute_daily_total` rounded fractional minutes half-down, which disagreed
with the payroll spec's half-up rule at exact half-minute boundaries.

## Change

- `src/worktime.py`: switch `//` to `round()` with `ROUND_HALF_UP`.
- Extracted the shared rounding logic used by both call sites into
  `_round_minutes`.

## Testing

Added `tests/test_worktime.py::test_half_minute_boundary_rounds_up`;
existing rounding tests still pass.
```

Notes on this example:

- `pr: null` and `head_sha: null` together mean "no PR exists for this yet" —
  this manifest may contain **only** the single `description` action; a
  `pr: null` manifest with a `review`, `reply`, or `comment` action is
  refused, since there is no PR to anchor them to.
- `branch` is populated because this text is meant for the `jb pr` **create**
  path, where a proposed head branch name matters. On the `jb review apply`
  update path, `branch` is ignored.
