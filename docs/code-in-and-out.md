# Getting code in and out

How do I move commits between the host and a container?

A container has its own clone of your repository, so its commits are not
visible to the host until you bring them over, and your host work is not
visible inside until you send it in. JailBee moves commits over a small
**git bridge** instead of round-tripping through GitHub. This page shows the
common tasks; [Git bridge and branch workflows](git-bridge.md) is the
reference for every flag and edge case.

Run every command on the host, from inside the repository, unless a step says
otherwise.

## How the bridge works

```text
        jailbee git fetch / checkout / pull
      ┌──────────────────────────────────────┐
      ▼                                      │
   host repo  ◄── objects it lacks ──►  container clone
   (your branches,                      (its own branch, working tree,
    GitHub credentials)                  no GitHub write access)
      │                                      ▲
      └──────────────────────────────────────┘
        jailbee git push (+ --merge / --rebase)
```

In clone mode the container is a git remote of the host. Commits travel over
`incus exec … git upload-pack` / `receive-pack`, so there is no Git server,
listening port or SSH key, and only objects the other side lacks are sent. Each
container records the **base branch** it was forked from; `jailbee git pull`
merges into that branch. Publishing to GitHub (`jailbee pr push`) runs on the
host with your credentials. In mount mode the container works in your host
checkout directly, so there is nothing to transfer. See
[Mount mode vs clone mode](git-bridge.md#mount-mode-vs-clone-mode).

Commands that take a container name ask for one with a picker when you leave
it out on a terminal; scripts should name it.

## Pull a container's commits to the host

You have committed inside `feat-foo` and want the work on the host. Three
commands, from least to most invasive:

```bash
jailbee git fetch feat-foo        # place the host branch; switch nothing
jailbee git checkout feat-foo     # fetch, fast-forward or create the branch, switch onto it
jailbee git pull feat-foo         # fetch and merge into the container's base branch
```

What you see: a short report of the refs that moved. A branch that has
diverged on the host is left alone with a warning (`fetch --force` overwrites
it, but never a checked-out branch).

Useful variations:

```bash
jailbee git fetch feat-foo --as alt     # write host branch 'alt' instead
jailbee git checkout feat-foo --as alt  # check out under another name
jailbee git pull feat-foo --current     # merge into the host's checked-out branch
jailbee git pull feat-foo --into dev    # merge into another host branch
jailbee git pull feat-foo --no-cleanup  # keep the container after the merge
```

`-b/--branch` always names a branch **inside the container**; the host side is
named with `--as` or `--into`. `pull` fast-forwards when it can and writes a
merge commit otherwise (`--ff` demands a fast-forward, `--no-ff` asks for a
merge commit). Be careful with `--cleanup`: it destroys the container after the
merge without the usual dirty-work confirmation.

More: [Container → host](git-bridge.md#the-git-bridge),
[Fast-forward policy](git-bridge.md#fast-forward-policy).

## Push host work into a container

You have changes on the host (a newer `main`, a fix) that the container
should have:

```bash
jailbee git push feat-foo                    # send a host branch in; action per config or prompt
jailbee git push feat-foo --merge            # …and merge it into the container's branch
jailbee git push feat-foo --rebase           # …or rebase onto it
jailbee git push feat-foo --current --merge  # send the host's checked-out branch
jailbee git push feat-foo --from-local       # send the local branch, not the fetched origin copy
```

`--plain` is transport only. With `--merge` or `--rebase`, a conflict leaves
the container in merge or rebase state; resolve it in `jailbee shell feat-foo`.
`--force` hard-resets the container's branch to what you push and discards
container-only commits, so use it deliberately.

Before it does anything, a push to an auto-picked container prints a plan
block (source, target, action, commit count) and asks to continue; reading that
block is the point. See
[Confirming an auto-picked container](git-bridge.md#confirming-an-auto-picked-container).

## Merge one container into another

To combine work from container `c1` into `c4` without touching the host
branches or working tree:

```bash
jailbee git merge c1 --into c4
jailbee git merge c1 c2 c3 --into c4     # one at a time, stop at the first conflict
jailbee git merge c1 --into c4 --plain   # move the refs only, run no merge
```

The merge runs inside `c4`, so a conflict is resolved there with
`jailbee shell c4`; the report prints the command that resumes the run. A
container cannot be both source and target. Details:
[Merging one container into another](git-bridge.md#merging-one-container-into-another--jailbee-git-merge).

## Tags and fast-forwards

Tags cross the bridge according to `pull.tags` (container → host, default
`reachable`) and `push.tags` (host → container, default `none`). Override them
for one run:

```bash
jailbee git pull feat-foo --tags          # every tag the container has
jailbee git push feat-foo --follow-tags   # only tags reachable from the pushed branch
jailbee git fetch feat-foo --no-tags
```

No setting ever moves an existing tag, and a conflicting tag fails the
transfer. `--ff` / `--no-ff` on `pull`, `push --merge` and `merge` control
whether a merge must fast-forward. See [Tags](git-bridge.md#tags) and
[Fast-forward policy](git-bridge.md#fast-forward-policy); the config keys are
under [`pull`](config.md#pull) and [`push`](config.md#push).

## Submodules

Submodules travel with the superproject in both directions, so `fetch`,
`checkout`, `pull`, `push` and `merge` need no extra step. The report has a
separate `Submodules` block. To put the host's working trees on a branch, use
`jailbee branch`:

```bash
jailbee branch master                    # host: whole tree to master
jailbee branch master --submodules-only
jailbee branch master --container feat-foo
```

A submodule that has commits of its own gets its own PR:

```bash
jailbee submodule pr push feat-foo             # auto-target, draft PR
jailbee submodule pr push feat-foo libs/foo    # explicit submodule
```

More: [Submodules](git-bridge.md#submodules).

## Review a pull request in a container

```bash
jailbee new --pr 1234
```

This needs the [`gh` CLI](https://cli.github.com/) authenticated on the host.
JailBee fetches the PR head and checks the container out at that commit; your
own branches stay untouched. Fork PRs work too.

When the author pushes more commits, refresh the container from the PR head
(the fetch runs on the host, so no network change is needed):

```bash
jailbee git push review-1234 --pr --rebase    # or --merge
```

The dashboards offer the same as **Refresh from PR head**. See
[Reviewing a pull request](git-bridge.md#reviewing-a-pull-request).

## Open or update a pull request

From a container's own branch, `jailbee pr push` creates a draft PR, or pushes
new commits into the one that exists:

```bash
jailbee pr push feat-foo                  # draft PR, or update the existing one
jailbee pr push feat-foo --ready          # mark it ready for review
jailbee pr push feat-foo --title "Fix parser" --body "Details…"
jailbee pr push feat-foo --web            # open the PR in the browser afterwards
```

If the branch already has an open PR, JailBee offers to push into it rather
than open a duplicate. The title, body and head branch can be proposed by a
configured agent (`--ai` / `--no-ai`); `--description` regenerates the text of
an existing PR, which is otherwise left alone. `--force` uses
`--force-with-lease`. Tags are never sent to GitHub.

On a container created with `jailbee new --pr`, the first `jailbee pr push`
asks whether to push into the reviewed PR's head or open a new PR on top of it.
Scripts say which:

```bash
jailbee pr push review-1234 --yes                          # push into PR 1234's head
jailbee pr push review-1234 --stacked --as fix/review      # open a PR based on PR 1234
```

`jailbee pr ls`, `jailbee pr open`, `jailbee pr bind`, `jailbee pr unbind` and
`jailbee pr fetch` list, open and manage a container's PR binding. More:
[A PR against the PR](git-bridge.md#a-pr-against-the-pr----stacked),
[Updating an adopted PR](git-bridge.md#updating-an-adopted-pr).

## Stacked PRs for your own work

When PR 2 builds on PR 1, base the second container on the first branch. Bring
the first branch to the host before you create the second container:

```bash
jailbee new feat/a --no-attach
# … commit inside feat-a …
jailbee git checkout feat-a
jailbee pr push feat-a

jailbee new feat/b feat/a --no-attach
# … commit inside feat-b …
jailbee pr push feat-b --base feat/a
```

After a review fix in `feat-a`, send it up the chain:

```bash
jailbee git checkout feat-a
jailbee git push feat-b --from feat/a --from-local --merge
```

When PR 1 merges, re-point the second container at the new base:

```bash
git checkout main && git pull --ff-only
jailbee git retarget feat-b main --merge
```

Retargeting changes the container's base, not PR 2's base on GitHub. A squash
or rebase merge of PR 1 rewrites ancestry, so check PR 2's commit list. The
full walkthrough is [Stacked PRs](git-bridge.md#stacked-prs).

## GitHub writes from inside a container

An agent inside a container cannot write to GitHub. It stages issues,
comments and reviews for you to publish. That is covered in
[Working with GitHub](github.md).

## More

- [Git bridge and branch workflows](git-bridge.md)
- [Working with GitHub](github.md)
- [Configuration: `pull` and `push`](config.md#pull)
- [Commands](commands.md)
