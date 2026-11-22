# Working with GitHub

How does JailBee handle issues, pull requests and reviews from inside a container?

The short version: the `gh` inside a container is **read-only**. When an agent
wants to change something on GitHub — file an issue, comment on a PR, post a
review — it **stages a proposal** in the host's *outbox*. You look at the
proposal on the host, publish it (or discard it), and check the *Sent log*.
Nothing reaches GitHub until you say so, and it is published with your host
credentials, not the container's.

```text
container (agent)                         host (you)
─────────────────                         ──────────
jailbee issue create …   ──stage──►   outbox (broker.db)
jailbee pr review submit …                │
                                          ▼
                                   jb outbox        inspect
                                   jb outbox apply  publish   ──► GitHub
                                   jb outbox sent   what landed
```

## Prerequisites

- Give the container a **read-only** token so the agent can read issues and
  PRs with `gh`. Setup, the recommended PAT shape and the `0600` file
  permission are in [Configuration → `github`](config.md#github) and
  [the git bridge](git-bridge.md#github-cli-gh-inside-containers).
- Keep your **publishing** credentials on the host (`gh auth login` there).
  The two identities are separate on purpose: a write-capable token inside the
  container would bypass the review step the outbox exists for.
- `jailbee doctor` reports a missing or too-permissive token.

## How does the agent propose a change?

You do not run these yourself; you ask the agent ("file an issue about the
flaky test", "review PR 42"). What the agent knows comes from two skills
installed in every container. Their sources:
[`jailbee-issue-management`](https://github.com/VRTFinland/jailbee/blob/main/docs/skills/jailbee-issue-management/SKILL.md)
and
[`jailbee-pr-review`](https://github.com/VRTFinland/jailbee/blob/main/docs/skills/jailbee-pr-review/SKILL.md).

Inside the container, the staging commands are:

| What | Command |
|---|---|
| New issue | `jailbee issue create --title … --body …` |
| Edit, comment, labels, assignees | `jailbee issue edit`, `comment`, `labels`, `assign` |
| Close or reopen an issue | `jailbee issue state N closed --reason completed` |
| General PR comment, reply | `jailbee pr comment N --body …`, `jailbee pr reply N --to ID --body …` |
| Inline review | `jailbee pr review add N --file F --line L --body …`, then `jailbee pr review submit N --body …` |
| PR title and description | `jailbee pr description N --title … --body-file …` |
| Branch to publish as a PR | `jailbee pr source …` |

Several related actions can be grouped with `--batch NAME` and staged together
with `jailbee outbox submit NAME --kind issue|pr`. Review drafts are submitted
with `jailbee pr review submit`, not `outbox submit`.

Each staging command prints the host command that publishes it, for example
`jb outbox apply <container> issue/001-flaky-test.json`. Proposals are
identified as `issue/<file>.json` or `pr/<file>.json`.

The agent can also check its own work:

```bash
jailbee outbox ls                   # what is staged and not yet published
jailbee outbox show issue/NAME      # one proposal
jailbee outbox discard issue/NAME   # withdraw a staged proposal
jailbee outbox discard --draft NAME --kind issue   # withdraw an unsubmitted draft
jailbee outbox sent                 # what the host has actually published
```

An agent that is an orchestrator's child cannot stage GitHub proposals at all;
it sends the content to its orchestrator instead. See
[orchestration](orchestration.md#publish-the-orchestrators-own-outboxes).

## How do I see what is waiting?

Staged proposals show up without asking: `jb ls` has an `ISSUES` column
(`✉N`), and the dashboard's `OUTBOX` column counts staged issue and PR actions.

On the host:

```bash
jb outbox                      # outbox manager (TTY), this repo's containers
jb outbox <container>          # narrow to one container
jb outbox ls                   # the same overview as a table; -o json for scripts
jb outbox show <container> pr/001-review.json
```

On a terminal, `jb outbox` opens a full-screen **outbox manager**: containers
and their proposals as a tree (proposal → actions → a review's inline
comments) beside a preview of the highlighted row. Keys are in the hint line:
`↑/↓` move, `Enter` expands, `Space` marks, `d` deletes, `p` publishes,
`X` clears the outbox, `v` toggles the preview, `m` shows the raw manifest,
`r` refreshes, `q` quits.

The terminal dashboard opens the same manager from a container's **Outbox**
menu entry (`Outbox (N pending)` moves to the top of the menu while something
is staged). See [the dashboard](dashboard.md).

Inspection reads only the host's outbox store: it makes no GitHub or network
calls and works on stopped containers.

`jb outbox show` prints a **revision token**. Passing it back with
`--revision` guarantees you publish exactly what you inspected; the manager and
the dashboards do this for you.

## How do I publish a proposal?

In the outbox manager (or the dashboard's Outbox box), put the cursor on a
proposal (or mark several with `Space`) and press `p`. The plan is shown above
three choices: **Publish**, **Publish as draft review** (offered when a PR
review is pending) and **Cancel**. Choosing Publish is the consent. The
publish runs in the background and its outcome lines appear in the box.

From a script or a plain terminal:

```bash
jb outbox apply <container> issue/001-flaky-test.json --dry-run   # show the plan
jb outbox apply <container> issue/001-flaky-test.json             # plan, confirm, publish
jb outbox apply <container> pr/001-review.json --draft            # as your pending review
```

`outbox apply` publishes **all pending actions of one manifest**, never just a
highlighted comment. There is no publish-everything command. `jb issue apply`
and `jb review apply` are the same command restricted to issue or PR proposals.

Useful flags:

| Flag | Meaning |
|---|---|
| `--dry-run` | Show the plan, publish nothing (`-o json` prints it as JSON) |
| `--draft` | Post review comments and replies as your *pending* GitHub review; you submit it on GitHub |
| `--revision TOKEN` | Refuse if the proposal changed since you inspected it |
| `--force` | Override a stale review anchor (PR proposals only) |
| `--foreign` | Consent to a PR or repository the container does not own; needed with `-y` |
| `-y` | Skip the confirmation only; never bypasses safety checks |

### Pull requests are published too

A proposal that is a **new PR** (a staged description awaiting its PR, or a
`pr source` manifest awaiting its push) is also published with `p` in the
dashboard and with `jb outbox apply`. Both hand over to `jb pr push`, which
pushes the branch with a fast-forward-only push, opens a draft PR, and asks its
own questions (AI-written description, branch name). When you mark several
rows, one confirmation covers all of them; ordinary manifests publish first,
then the PR creations one after another, stopping at the first failure or
cancel. See [Pull requests](git-bridge.md#reviewing-a-pull-request) and
[Stacked PRs](git-bridge.md#stacked-prs).

### When the outcome is uncertain

If GitHub may have accepted an action but the answer never arrived, the action
is marked **uncertain** and is never retried automatically. Check GitHub, then:

```bash
jb outbox resolve <container> issue/001-flaky-test.json 0 --applied --url URL
jb outbox resolve <container> issue/001-flaky-test.json 0 --retry
```

`--applied` records that it landed (an issue *create* also needs `--issue N`);
`--retry` forgets the record so `outbox apply` tries again.

## How do I discard a proposal?

Either side can withdraw a proposal before it is published.

- In the outbox manager, mark rows (or leave the cursor on one) and press `d`.
  One "No"-first question names exactly what goes.
- On the host: `jb outbox drop <container> pr/001-review.json`. Add
  `--action N` for one action, plus `--comment M` for one inline review
  comment (zero-based). Deleting an issue `create` that others refer to needs
  `--with-dependents`.
- To wipe every unpublished proposal in some containers:
  `jb outbox clear <container>… [--dry-run] [-y]`. Proposals with uncertain or
  partly published actions are kept.
- The agent can withdraw its own with `jailbee outbox discard`.

Deleting is local only; GitHub is not touched. Once an image has been uploaded
to GitHub, discarding the proposal cannot retract it.

## How do I check what was published?

Every action that lands is written to the **Sent log**:

```bash
jb outbox sent ls                 # newest first; --repo PREFIX, --limit N, -o json
jb outbox sent show 12            # one record in full, with its GitHub URL
jb outbox sent clear --before 2026-01-01 -y   # delete local records only
```

Each record carries the publisher (you or an orchestrator), the state
(`applied` or `uncertain`), the content and the URL. Records outlive the
container. In the dashboard, the Outbox box has a **Sent** tab (`Tab` switches,
`o` opens the URL) and the repo menu has a `Sent…` timeline (`s`). Inside the
container, `jailbee outbox sent` lists the container's own records.

## More tasks

### Add an issue to a project board

The agent stages `jailbee issue project N --project OWNER/NUMBER --add`
(`--set FIELD=VALUE` sets a field and adds the item if needed). Only boards the
host knows are accepted: the repo's `github.project` default, extras in the
host-local `github.projects` list, or an orchestrator's `publish.projects`
grant. You publish it like any other issue proposal. See
[Issue management](git-bridge.md#issue-management).

### Fix one of your own comments

The agent stages `jailbee issue comment-edit ID --body …` for a conversation
comment on an issue or PR (find `ID` with `jailbee issue comments N`), or
`jailbee pr comment-edit ID --body …` for a submitted review comment. The host
refuses it unless the publishing account wrote the comment and it is unchanged
since staging.

### Close or reopen an issue

`jailbee issue state N closed --reason completed|not_planned`, or
`jailbee issue state N open`. Closing and reopening *pull requests* is not a
staged action.

### Review someone else's PR

`jb new --pr N` creates a container on the PR's branch; the agent reads it and
stages comments with `jailbee pr review …`. Publish as a draft review to read
it in GitHub before you submit it. See
[Reviewing a pull request](git-bridge.md#reviewing-a-pull-request).

### Work in another repository

Every action names a `repo`: `.` for this project, a submodule path, or
`github:OWNER/NAME` for a repository outside it. The last kind shows a
foreign-repository warning and needs your explicit consent
(`--foreign` with `-y`).

## More

- [Git bridge: unified proposal management](git-bridge.md#unified-proposal-management)
- [Git bridge: the Sent log](git-bridge.md#the-sent-log)
- [Git bridge: issue management](git-bridge.md#issue-management)
- [The terminal dashboard](dashboard.md)
- [Commands](commands.md)
