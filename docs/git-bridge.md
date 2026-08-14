# Git bridge and branch workflows

## The git bridge

In clone mode the container has its own working tree, so commits move between
host and container over a small bridge — **the container acts as a git remote** —
instead of round-tripping through GitHub. Each container records the **base
branch** it was forked from (`user.jailbee.base_branch`, set at `jailbee new` time); pulls
merge into that base and `jailbee ls` / `jailbee git diff` measure "ahead" against it.

**Container → host:**

```bash
jailbee git fetch feat-foo       # → refs/jailbee/<short>/<branch> (transport only)
jailbee git checkout feat-foo    # fetch + fast-forward/create the host branch
jailbee git checkout feat-foo --as alt     # …under a different host branch name
jailbee git pull feat-foo        # fetch + merge the container branch into its BASE branch
jailbee git pull feat-foo --current        # merge into the host's checked-out branch
```

On every container → host command, `-b/--branch` selects which branch is read
**inside the container** — it never names the host-side branch. Naming the host
side is a separate flag per command: `jailbee git checkout --as <name>`,
`jailbee git pull --into <name>`. A `-b` naming a branch the container doesn't have
is rejected up front, with the container's actual branch names listed.

`jailbee git pull` defaults to a `--no-ff` merge commit into the recorded base branch
(not the host's current HEAD). `--into <branch>` retargets it, `--current` merges
into whichever branch the host currently has checked out instead (mirrors
`jailbee git push --current`; mutually exclusive with `--into`), `--ff` fast-forwards
only, `--checkout` checks the base out (staying on it) if it isn't current, and
`--cleanup`/`--no-cleanup` force or skip the post-merge destroy + branch-delete
(otherwise driven by the `pull:` config block). With no name on a TTY it opens a
multi-select picker and stops at the first failure.

**Host → container:**

```bash
jailbee git push feat-foo                  # send a host branch in (source/action per config or prompt)
jailbee git push feat-foo --current --merge  # send current branch + merge it in the container
jailbee git push feat-foo --pr             # PR containers: refresh the PR head from GitHub first
jailbee git push feat-foo --from-local     # send the host's local branch, unfetched
jailbee pr feat-foo                        # create a draft PR, or push new commits + optionally update it
```

`--merge`/`--rebase` apply the pushed ref to the container's branch (conflicts
left for `jailbee shell`); `--plain` is transport only. With no name on a TTY it opens
a multi-select picker (failures don't stop the batch; ✓/✗ summary at the end).

### Confirming an auto-picked container

With two or more containers, `jailbee git push` / `pull` / `checkout` show a picker.
With exactly one and no name argument they used to just go, which is where the
"wrong branch" mistakes happened: neither the source branch nor the target
branch is necessarily visible in the command line — `push.default_source`
defaults to the container's `user.jailbee.base_branch` label, `push.push_from`
defaults to the `origin/` copy, and `jailbee git pull` merges into the recorded
base branch.

So in that case the command now prints what it is about to do and waits:

```
Push  host ──▶ container
  container : feat-foo  (app-feat-foo, Running)
  source    : origin/main  a1b2c3d "Bump deps"
  target    : feat/foo     9f8e7d6 "WIP parser"
            : 4 commit(s) to apply
  action    : merge
Continue? [Y/n]
```

Enter proceeds — the value is in reading the block, not in an extra keystroke.
Declining aborts before anything reaches the container, a host branch, or the
working tree — except for what already ran to *build* the plan you're
looking at: `push`'s hoisted fetch (below), and, when `push.default_source:
ask` led you to pick the PR-head option on a PR container, the refresh that
already fetched the PR head from GitHub into the host's
`refs/jailbee/pr/<N>/head` before the block was even printed. For `push`
specifically, the host may already have run `git fetch origin <source>` by
the time you decline: that fetch is hoisted ahead of the prompt so the block
shows the tip the push would really send, not a stale one. It only advances
the host's `origin/<branch>` remote-tracking ref — no container, no host
branch, no working tree — so declining is still safe, just not a literal
no-op on the push path.

`--no-confirm` skips the prompt for one run, `--confirm` forces it when the
config has it off, and `confirm.auto_target: false` turns it off for the
repo or the user. Off a TTY, `pull` and `checkout` still print the block and
only skip the prompt. `push` behaves differently: without an explicit name
it requires a TTY in the first place — off one it errors with "No container
name given…" before it ever lists containers — so it never shows the block
there either. A container named explicitly, or chosen from the picker, is
never confirmed. If JailBee cannot build the plan at all (the container vanished,
a daemon hiccup), the confirmation is silently skipped and the operation
proceeds to produce its own error.

### Which copy of the source branch travels

By default `jailbee git push` fetches `origin/<source>` on the host and pushes
*that* ref, not `refs/heads/<source>`. `git fetch` only moves
`refs/remotes/origin/<branch>`; the local branch advances on `git pull`. So for
a branch you never check out on the host — usually the base branch that `push`
sends by default — the local ref is stale precisely when you just fetched.
Pushing it would also force-move the container's `refs/jailbee/base/<base>` anchor
backwards, inflating `jailbee ls` AHEAD counts.

- `--from-local` — push `refs/heads/<source>` as-is, skipping the fetch. Use
  when the host has commits not yet on origin.
- `--from-origin` — force the origin ref (overrides `push.push_from: local`
  and the `--current` default).
- `--no-fetch` — push the origin ref without refreshing it first.
- `--current` always resolves locally: the checked-out branch is the work in
  progress.
- `--pr` bypasses the choice: the head is fetched into `refs/jailbee/pr/<N>/head`
  and that exact ref is pushed, so `--from-local`/`--from-origin` are rejected
  alongside it.

When the origin ref is pushed while the local branch holds commits it lacks,
`jailbee` warns with the count and points at `--from-local` — nothing is dropped
silently. Configure the defaults with `push.push_from` / `push.autofetch`
(see [config.md](config.md#push)).

> Throughout this document `origin` means *the upstream copy*, not
> necessarily a remote literally called `origin`. jailbee resolves which
> remote that is (see [Which remote is the
> upstream?](config.md#which-remote-is-the-upstream)); the flag names
> `--from-origin` / `--from-local` and the `push_from: origin` value keep the
> word regardless of what your remote is called.

Both `jailbee git push` and `jailbee git pull` print a one-line
`<source> (…) ──▶ <target> (…)` banner before the detailed summary, so the
direction of the sync is always unambiguous at a glance.

`jailbee pull` / `jailbee push` / `jailbee diff` are top-level aliases. `jailbee pr` is a
first-class top-level command in its own right (`jailbee git pr` is its hidden
alias). **There is no `jailbee git merge`** — it was replaced by `jailbee git pull`.
All bridge commands refuse on mount-mode containers (they share the host
tree — use git on the host directly).

## Submodules

A container holds its own clone, which is what makes it disposable — and
what makes submodules the hard part, because a sub-repo the peer has never
seen has no objects to check out. JailBee moves them with the superproject,
in both directions, without a round trip through the submodule's upstream.

**On `jailbee new`.** Submodules are initialised recursively and *offline*:
each one's URL is pointed at the matching subdirectory of the read-only
`/mnt/host-source` mount, `submodule update --init` runs from there, and
`submodule sync` then repoints `origin` at the real upstream. Nothing is
fetched over the network, and every submodule lands on the container's
branch. Set `new.submodules: false` to skip the whole step (see
[config.md](config.md#new)).

**On `jailbee git push` / `pull` / `checkout`.** Submodule objects travel over
the same `ext::` transport the superproject uses. A sub-repo the peer is
missing is created there first, so adding a submodule on one side and
syncing works without preparing the other side by hand. Failures are loud:
a `SubmoduleError` stops the operation rather than leaving the peer with a
superproject whose gitlinks point at objects it doesn't have.

**What you see.** `jailbee pull` prints a delimited `── Submodules` block
after git's own output — per submodule `new → <sha>`, `<sha> → removed`, or
a commit count with insertions and deletions — so a gitlink that moved is
never buried in the superproject's diff.

**Conflicting gitlinks.** When both sides moved the same submodule, git stops
at `CONFLICT (submodule)` and leaves the pointer to you. JailBee merges it
instead: inside each conflicted submodule it merges their commit into ours and
stages the result, recursing into nested submodules whose own gitlinks conflict
in turn. One pass attempts every submodule — a failure never stops the sweep —
so a single `jailbee pull` (or `jailbee git push --merge`) hands you one report
of everything rather than one conflict per run. If that clears the merge, the
superproject commit is made for you and the operation succeeds. What is left
over is grouped by what it needs:

- **in merge state** — git stopped mid-merge here: resolve, `git add`, `git commit`
- **skipped, not touched** — a dirty sub-repo (commit or stash, then re-run) or
  a gitlink that exists on one side only (pick a side by hand)

Ordinary file conflicts are never auto-resolved; they are listed alongside so
you see the whole picture before starting.

**Branch placement.** `jailbee submodule checkout` recursively puts submodules
on the superproject's branch. It is purely local — it moves nothing between
host and container — and works on either side: with no argument it aligns
the host repo, with a container name it aligns that container.

```bash
jailbee submodule checkout               # host repo, current branch
jailbee submodule checkout -b feat/x     # host repo, explicit branch
jailbee submodule checkout feat-foo      # container 'feat-foo', its branch
```

**Before you destroy.** `jailbee destroy`'s pre-flight check counts a changed
submodule as work at risk — added, removed, committed ahead, or merely dirty
— and names it in the summary, so a container is not thrown away because
only its sub-repo held the change.

Each submodule also carries its own base anchor, seeded from the gitlink
recorded at the superproject's `refs/jailbee/base/<base>`, which is what lets
per-submodule comparisons stay meaningful on a stacked branch.

## Stacked PRs

When PR1 (`feat/a`) is waiting for review and PR2 builds on top of it,
base the second container on the first one's branch. The host is the hub:
containers never push to GitHub directly, and chain maintenance is
merge-based (never rebase a branch with work stacked on it).

```bash
jailbee new feat/a                       # work → jailbee git checkout feat-a → git push → PR1
jailbee new feat/b feat/a                # work → jailbee git checkout feat-b → git push → PR2 (base: feat/a)

# Review fix lands in container A:
jailbee git checkout feat-a && git push  # PR1 updates
jailbee git push feat-b --merge          # fix flows into B (push source = B's base = feat/a)

# PR1 merges on GitHub:
git checkout main && git pull
jailbee git retarget feat-b main --merge # B's base flips to main, main merged in
jailbee destroy feat-a --force
```

For longer chains, repeat the propagation per link: `jailbee git checkout
feat-b && git push`, then `jailbee git push feat-c --merge`.

## Choosing the starting point for `jailbee new`

`<base>` always names the container's **base branch** — the
`user.jailbee.base_branch` label and the `refs/jailbee/base/<base>` anchor that
`jailbee ls` AHEAD/MERGE and `jailbee git pull` are measured against. Whether
`jailbee new` *forks* off it depends on whether `<branch>` already exists in
the source repo:

| Invocation | `<branch>` in source? | `<base>` in source? | Result |
|---|---|---|---|
| `jailbee new X` | no | — | clone default branch, `checkout -b X`; base = default branch |
| `jailbee new X` | yes | — | clone `X` directly (review/test); base = default branch |
| `jailbee new X Y` | no | yes | clone `Y`, then `checkout -b X`; base = `Y` |
| `jailbee new X Y` | yes | yes | clone `X` directly; base = `Y` (confirmed first, `-y` skips) |
| `jailbee new X Y` | (any) | no | error — base Y not in source (run `git fetch origin Y`) |

No auto-fetch: missing refs fail fast with the exact command to run. A base
that exists only as `refs/remotes/origin/<base>` is accepted; the anchor is
seeded from that tip.

The last row is how you put an existing branch on the right base at creation
time — `jailbee git retarget` is for changing a base afterwards.

**Shortcut:** `jailbee new --current` resolves `<branch>` from the host
repo's currently checked-out branch (via `git symbolic-ref --short HEAD`).
Cannot be combined with positionals; errors on detached HEAD.

## Background creation

`jailbee new` blocks until the container is fully provisioned (init, clone,
autostart) — often a few minutes. Pass `--background` (`-b`) to provision
detached and get the shell back immediately:

```bash
jailbee new feat/foo --background
```

Track progress with `jailbee ls`: a `JOB` column shows the live phase
(`creating` → `cloning` → `autostart`) and the row drops back to a normal
running container once it's ready. A failed background creation shows
`failed` and leaves the container intact for inspection (`jailbee shell
<name>`, then `jailbee destroy`). The detailed worker log is written under
`${XDG_STATE_HOME:-~/.local/state}/jailbee/logs/`.

Once you have fixed things by hand (`jailbee shell <name> --force`), clear the
record with `jailbee job clear <name>`; the container is not touched.

To make background the default, set it in `~/.config/jailbee/global.yaml`
(applies to every repo) or a repo's `.jailbee/config.yaml`:

```yaml
new:
  background: true
```

With that default on, `--no-background` forces a one-off foreground run, and
so does an explicit `--attach shell`/`--attach tmux` (or the `--tmux` /
`--shell` shorthands) — a detached creation has no shell to attach to, so
asking to attach means asking for the foreground. `--attach none` /
`--no-attach` don't force foreground and combine fine with `--background`.
Passing `--background` together with `--attach shell`/`--attach tmux`,
`--tmux`, or `--shell` is a usage error.

## Mount mode vs clone mode

`jailbee new <name>` is **clone mode**: the host repo is `git clone --shared`'d
into `/home/dev/<repo>` inside the container. The container has its own
working tree, isolated from the host's. `jailbee git fetch / checkout / pull / push`
transfer commits between the two (see [The git bridge](#the-git-bridge)).

`jailbee new <name> --mount` (or `-m`) is **mount mode**: the host directory
at `cfg.repo_root` is bind-mounted RW into the container at the same
in-container path. The container and the host share one working tree.

When to use mount mode:

- The host directory contains submodules or nested checkouts that are
  awkward to clone.
- You edit on the host (e.g. IDE on the host) and want to run/test in
  the container without manual sync.
- The host directory is not a git repo at all — clone mode is
  unavailable; mount mode does not require `.git`.

Trade-offs:

- Autostart steps (`npm install`, `git fetch`, ...) mutate the host
  working tree because the bind is shared.
- `jailbee git fetch / checkout / pull / push` do not work on mount-mode containers
  — they error and tell you to use git on the host directly.
- Concurrent writes from multiple mount-mode containers to the same
  file are not coordinated by `jailbee`; the kernel handles concurrent
  writes and git's own index lock handles concurrent git ops.

Example:

```bash
jailbee new mountfoo --mount
jailbee shell mountfoo
# inside container, /home/dev/<repo> IS the host directory; edits
# show up on the host immediately.
```

## Reviewing a pull request

To spin up a container from a GitHub PR:

```bash
jailbee new --pr 1234
```

This fetches the PR's head into the source repo as
`refs/jailbee/pr/1234/head` and checks the container's clone out at that
commit. The head deliberately does not land in a branch: `git fetch`
refuses to update a `refs/heads/*` ref that is checked out in any
worktree, so fetching into one broke `jailbee new --pr` whenever the host had
the PR's own branch checked out — and a stale or diverging local branch of
that name must never decide what the container is built from. Your
branches are left untouched. The PR number is stored on the container as
`user.jailbee.pr=1234` for future tooling.

The command requires the [`gh` CLI](https://cli.github.com/) and an
authenticated session (`gh auth login`). Cross-repository (fork) PRs
work without additional configuration.

### Round-tripping a PR container

Pull in commits the author pushed after the container was created — the
fetch runs **on the host**, so no `jailbee net loose` is needed:

```bash
jailbee git push <name> --pr --rebase    # or --merge
```

Push your own commits back to the PR's head branch:

```bash
jailbee pr <name>                        # asks once, then updates PR #N
jailbee pr <name> --yes                  # skip the confirmation
```

The confirmation is recorded on the container (`user.jailbee.pr_adopted`), so
later `jailbee pr` runs push new commits without asking again. Fork PRs are
refused: their head lives in another repository, so pushing to `origin`
would create an unrelated branch instead of updating the PR.

The PR is still not JailBee's own, so on every run it stays hands-off in ways a
jailbee-authored PR does not:

- The **description is never regenerated** unless you ask for it
  (`--description`, `--title`, `--body`). The interactive "Update the PR
  description with Claude?" offer is suppressed — it would replace the PR
  author's text.
- **`--force` asks a second time**, naming the head branch it would
  overwrite; `--yes` skips that too, and without a TTY it is an error.
- **`--as` is rejected** (exit 2). That holds for any container with a PR,
  jailbee-authored ones included: the PR's head branch is fixed, so pushing to
  a different name would leave the PR untouched.

### A branch that already has a PR

The same treatment applies to a container that was never created from a PR at
all — `jailbee new <existing-branch>`, where the branch happens to have an open PR
already. Before opening anything, `jailbee pr` asks GitHub whether the container's
branch has a PR (`gh pr view <branch>`) and, if it does, offers to push to that
PR instead:

```
Branch 'alice/work-type' already has PR #77 by @alice (OPEN);
  head 'alice/work-type' → base 'main'.
Push this container's commits to PR #77 instead of opening a new one? [Y/n]
```

Confirming records `user.jailbee.pr` / `user.jailbee.pr_branch` / `user.jailbee.pr_adopted`
— not `user.jailbee.pr_author`, because JailBee found this PR rather than opening it, so
the hands-off rules above stay in force. Declining exits without publishing and
points at `--as <other-branch>` for opening a separate PR; `--yes` skips the
question, and without a TTY it is an error.

Two cases fall through to opening a new PR, each with a printed reason: a
**closed or merged** PR (no longer a target for further work) and a **fork** PR
(its head lives in the fork, so a same-named local branch is a different
branch). Passing `--as` skips the lookup entirely — it already says you want a
separate PR. The lookup is best-effort: no `gh`, no network or an origin that
is not on GitHub simply means the ordinary create path runs.

Without this, the AI-proposed head branch name (`claude.ai_pr_branch`) would
publish the work under a *new* branch and `gh pr create` would happily open a
duplicate PR for it.

## GitHub CLI (`gh`) inside containers

The `gh` binary is baked into every container's golden image. To make
it authenticate (for AI agents like Claude that call `gh pr view`,
`gh issue create`, etc.), opt in via `~/.config/jailbee/global.yaml`:

```yaml
github:
  enabled: true
  api_tokens:
    sampleapp:     github_pat_AAA...   # one entry per GitHub owner
    personal-tool: github_pat_BBB...
```

The dict keys are `container_prefix` values from each repo's
`.jailbee/config.yaml`. Each container picks exactly one token — the one
matching its repo's prefix.

GitHub fine-grained PATs are scoped per **resource owner** (one user
or one org), which is why this is a map and not a single string:
a user working across multiple orgs maintains one entry per owner.

**Recommended PAT shape:**

1. github.com → Settings → Developer settings → Personal access tokens
   → Fine-grained tokens → Generate new token.
2. Resource owner: pick the org (or your account) whose repos the
   agents will touch from this container.
3. Repository access: **Only select repositories** → pick your work
   repos for that owner.
4. Repository permissions:
   - Contents: Read
   - Issues: Read and write
   - Pull requests: Read and write
   - Metadata: Read
5. Copy the token (`github_pat_...`), paste into `api_tokens`.

After editing, run `chmod 600 ~/.config/jailbee/global.yaml`. `jailbee doctor`
warns if perms are loose, if the token is empty, or if it's a classic
PAT (`ghp_*`) — those can't be scoped to specific repos.

PR merge, PR close, and existing-issue editing are *intentionally*
left out of the recommended scope: keep agent write access narrow.

The `github` block must live in `~/.config/jailbee/global.yaml`, never a
repo's `.jailbee/config.yaml` — `jailbee` rejects it at the repo layer so tokens
can't leak via git commits. See [`config.md`](config.md#github) for the
full field reference, the `0600` permission requirement, and the
`jailbee doctor` checks.
