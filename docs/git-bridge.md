# Git bridge and branch workflows

## The git bridge

In clone mode the container has its own working tree, so commits move between
host and container over a small bridge — **the container acts as a git remote** —
instead of round-tripping through GitHub. The transport is git's own `ext::`
helper running `incus exec --user <uid> … git upload-pack` (reading) or
`git receive-pack` (writing) inside the container. It needs access to the
Incus daemon, but no separate Git server, listening Git port or SSH key. Little travels either way: the container was cloned with
`git clone --shared`, so its alternates point at the host's object store and
only the objects the other side actually lacks are sent. Each container records
the **base branch** it was forked from (`user.jailbee.base_branch`, set at
`jailbee new` time); pulls merge into that branch. Status and the default
committed diff compare against the host's live target commit for that branch,
not a container's pinned transport anchor.

**Container → host:**

```bash
jailbee git fetch feat-foo       # places host + submodule branches; does not switch branches
jailbee git fetch feat-foo --as alt        # write host branch 'alt' instead
jailbee git fetch feat-foo --force         # overwrite a diverged host branch
jailbee git checkout feat-foo    # fetch + fast-forward/create the host branch, AND switch onto it
jailbee git checkout feat-foo --as alt     # …under a different host branch name
jailbee git pull feat-foo        # fetch + merge the container branch into its BASE branch
jailbee git pull feat-foo --current        # merge into the host's checked-out branch
```

`jailbee git fetch` fetches into `refs/jailbee/<short>/<branch>`, transports
submodule objects, and attempts to place the host branch and same-named
submodule branches at the container's commits. It does **not switch
branches**. If the superproject target is already checked out, it attempts
`git merge --ff-only`, which can update the index and working tree; local
changes that obstruct that merge cause a refusal. Otherwise it places refs
only. Submodule placement does not switch their working trees and can refuse
to move a checked-out branch.

A diverged host branch is left alone with a warning; `fetch --force` can
overwrite it, but never forces a checked-out branch. `jailbee git checkout`
fetches, fast-forwards or creates the host branch, and switches onto it,
aligning submodules too. To switch onto a branch placed by `fetch`, run
`jailbee branch <branch>` afterwards.

On `fetch`, `checkout` and `pull`, `-b/--branch` selects which branch is read
**inside the container** — it never names the host-side branch. Naming the host
side is a separate flag per command: `jailbee git fetch --as <name>`,
`jailbee git checkout --as <name>`, `jailbee git pull --into <name>`. A `-b`
naming a branch the container doesn't have is rejected up front, with the
container's actual branch names listed.

`jailbee git pull` merges the container's branch into the recorded base branch
(not the host's current HEAD). By default (`pull.ff: auto`) it fast-forwards
when the host branch is strictly behind the container's tip and writes a merge
commit otherwise — the same rule `git merge` itself uses; `--into <branch>`
retargets the merge, `--current` merges into whichever branch the host
currently has checked out instead (mirrors `jailbee git push --current`;
mutually exclusive with `--into`), `--ff` demands a fast-forward and fails on
divergence, and `--no-ff` requests a merge commit on the checked-out-target
path (set `pull.ff: never` to make that the default). `--checkout` checks the
target out (staying on it) if it isn't current. `--cleanup` selects `always`
for post-merge container destruction and host source-branch deletion;
`--no-cleanup` skips both, otherwise the `pull:` config block decides.
Cleanup is skipped if the target tip did not move, and branch deletion still
requires a distinct, fully merged local branch. **`--cleanup` (or
`pull.destroy_container: always`) skips the dirty/unknown-work confirmation
before destroying the container**: uncommitted work can be lost even when
the transferred commits merged successfully. The default `prompt` policies
ask on a terminal and skip cleanup without one. See [Tags](#tags) and [Fast-forward
policy](#fast-forward-policy) below for the full picture. With no name on a
TTY it opens a multi-select picker and stops at the first failure.
Several names (`jailbee git pull a b`) run the same batch as the picker, in
order, with no picker.

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
Several names (`jailbee git push a b`) run the same batch with source and action
resolved once and no picker; `--pr` and `--force` take exactly one name (exit 2).

With the default `push.ff: auto`, `--merge` requires a fast-forward when the
container is already on the branch being pushed (the normal `--pr` case),
and permits a regular merge otherwise. If both tips have unique commits,
JailBee prints both counts and asks whether to merge instead. `--no-ff`
permits that up front; `--ff` refuses divergence. Without a TTY the automatic
divergence case errors and names `--no-ff`.

**Current limitation:** for merges inside a container, `--no-ff` removes the
ff-only restriction but does not pass Git's `--no-ff`. A regular merge can
still fast-forward, subject to Git configuration. It does not guarantee a
merge commit; this also applies to `push.ff: never` and `jailbee git merge`.

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

Containers named on the command line or ticked in the picker are never
confirmed as a batch: choosing them is the decision. The dashboards ask once
themselves before a bulk push, pull or merge over two or more containers
(default No); for merge they pass `--confirm`, so the CLI asks once the
targets are picked and can name both ends. `confirm.auto_target` does not
govern that question.

`--no-confirm` skips the prompt for one run, `--confirm` forces it when the
config has it off, and `confirm.auto_target: false` turns it off for the
repo or the user. Off a TTY, `pull` and `checkout` still print the block and
only skip the prompt. `push` behaves differently: without an explicit name
it requires a TTY — off one it exits 2 naming the missing container and
eligible candidates, without showing the confirmation block. A container
named explicitly, or chosen from the picker, is never confirmed. If JailBee cannot build the plan at all (the container vanished,
a daemon hiccup), the confirmation is silently skipped and the operation
proceeds to produce its own error.

### Which copy of the source branch travels

By default `jailbee git push` fetches `origin/<source>` on the host and pushes
*that* ref, not `refs/heads/<source>`. `git fetch` only moves
`refs/remotes/origin/<branch>`; the local branch advances on `git pull`. So for
a branch you never check out on the host — usually the base branch that `push`
sends by default — the local ref is stale precisely when you just fetched.
Pushing it can also force-move the container's `refs/jailbee/base/<base>`
transport anchor backwards; status no longer uses that anchor, but in-container
operations that read it may see the older point until the anchor is refreshed.

- `--from-local` — prefer `refs/heads/<source>` as-is and skip fetching. Use
  when the host has commits not yet on origin.
- `--from-origin` — prefer the origin ref (overrides `push.push_from: local`
  and the `--current` default).
- `--no-fetch` — do not refresh the selected origin ref before sending it.
- `--current` selects the checked-out branch and defaults to local preference;
  `--from-origin` can override that preference.
- `--pr` bypasses the choice: the head is fetched into `refs/jailbee/pr/<N>/head`
  and that exact ref is pushed, so `--from-local`/`--from-origin` are rejected
  alongside it.

These are preferences, not strict ref requirements: when the preferred local
or remote-tracking ref is absent, JailBee falls back to the other. The normal
origin fetch is best-effort; a failed fetch is reported but can still be
followed by a push of an existing, possibly stale ref. Check the reported
source if freshness matters.

When the origin ref is pushed while the local branch holds commits it lacks,
JailBee warns with the count and points at `--from-local`. Configure the
defaults with `push.push_from` / `push.autofetch`
(see [config.md](config.md#push)).

> Throughout this document `origin` means *the upstream copy*, not
> necessarily a remote literally called `origin`. jailbee resolves which
> remote that is (see [Which remote is the
> upstream?](config.md#which-remote-is-the-upstream)); the flag names
> `--from-origin` / `--from-local` and the `push_from: origin` value keep the
> word regardless of what your remote is called.

### What the push writes inside the container

A push updates up to three refs in the container:

| Ref | When |
|---|---|
| `refs/jailbee/host/<source>` | always — the transport's landing ref, force-updated |
| `refs/jailbee/base/<base>` | when the pushed source *is* the container's base branch, to refresh that container's in-clone transport anchor |
| `refs/heads/<source>` | when it can be fast-forwarded (see below) |

`refs/jailbee/base/<base>` remains a transport/bridge anchor for operations
inside containers; it is **not** the `jailbee ls` status baseline. Each status
gather snapshots the host's local `refs/heads/<base>` once per base branch and
uses that same commit for all matching containers. If the local branch does not
exist, status falls back to `refs/remotes/<upstream>/<base>` and marks BASE as
tracking; it never fetches, fast-forwards, or rewrites anchors. A
container-to-container `jailbee git merge` still transports directly and
changes no host branch. After a bridge pull or merge moves the host target, the
next status gather sees that new local tip automatically.

When both local and tracking refs exist, local always wins. Tracking is only
the last-fetched copy: if it is ahead of local, status reports `local target
behind fetched <upstream>/<base>`; if the refs have diverged it reports
`diverged`. These notices do not claim to know the remote server's current
state. If neither ref exists, or the selected host commit object is unreadable
from the container, comparison-dependent values are `?`; JailBee does not
fall back to a pinned anchor or another branch. Live worktree and merge-state
information remain independently available.

In status output, **DIFF ±** is the direct committed-tree line diff
(`git diff <host-target> HEAD`); equal trees are `clean` even when histories
differ. **↑** counts commits unique to the container and **↓** counts commits
unique to the host target, using graph ancestry, so both can be nonzero when
DIFF ± is clean. WT remains separate. Explicit JSON `git_status` exposes
`target_diff`, `ahead_count`, `behind_count`, `base_source`, `base_sha`,
`tracking_relation` and `upstream_ref`; per-submodule line fields are
`target_ins`/`target_del`, while commit counts remain `ahead_commits` and
`behind_commits`. The old `ahead_diff`, `ahead_ins` and `ahead_del` names are
retired; see [column migration](config.md#ls--dashboard--remembered-columns).

`jailbee git diff <name>` uses the same live host target and direct comparison
by default. `--incoming` selects the former three-dot contribution diff;
`--stat` applies to either committed view, while `--wt` is unchanged and
`--all` combines the default committed view with working-tree changes. A
missing target or unreadable target object is a clear command error, not a
silent comparison against an anchor. Unlike fetch/checkout/pull, `diff`
currently ignores its accepted `-b/--branch` option: it reads the container's
current HEAD and working tree. `LOCAL ±`/`L↑`, when opted in, remain a
separate comparison against the host's currently checked-out HEAD.

The `refs/heads/<source>` update exists because everyday branch-name
commands do not resolve through the `refs/jailbee/*` namespace: a container
whose local `dev` never moved makes an in-container
`git rebase dev` silently use a stale base. Its `origin` is the real upstream
URL, so an in-container fetch needs both permitted network access and any
required credentials; the host bridge avoids that dependency.

The update is strictly fast-forward and never fails a push:

- **HEAD's own branch is skipped.** Moving it would leave the index and working
  tree describing a commit the branch no longer points at, and
  `receive.denyCurrentBranch` refuses a push into it for the same reason. This
  is the case for a container forked from the base branch itself, and for every
  `--pr` push (a PR container is checked out on the head ref). `--merge` and
  `--rebase` advance that branch themselves.
- **An absent branch is created.** `git clone` gives the container only the
  host's HEAD branch, so a container created off `dev` from a host sitting on
  `main` has no local `dev` at all until the first push.
- **A diverged branch is reported and left alone** — a commit made in the
  container on that branch is never discarded here. Reconcile it by hand in
  `jailbee shell`, or compare against `refs/jailbee/host/<source>`.
- The write uses `git update-ref`'s compare-and-swap form, so a commit made in
  the container mid-push loses the race rather than the commit.

The push summary prints one line when the branch was created or fast-forwarded,
a warning when it diverged or the update failed, and nothing in the two benign
cases (already current, or HEAD's own branch).

Both `jailbee git push` and `jailbee git pull` print a one-line
`<source> (…) ──▶ <target> (…)` banner before the detailed summary, so the
direction of the sync is always unambiguous at a glance.

`jailbee pull` / `jailbee push` / `jailbee diff` / `jailbee fetch` / `jailbee checkout` /
`jailbee retarget` / `jailbee merge` are top-level aliases. `jailbee pr` is a
first-class top-level command in its own right (`jailbee git pr` is its hidden
alias). `jailbee merge` came last and was withheld at first — that bare verb
used to name today's `jailbee git pull` — but the merge target is never
inferred, so the old `jailbee merge <name>` shape asks which container to merge
into rather than quietly merging into the host. All bridge commands
refuse on mount-mode containers (they share the host tree — use git on the
host directly).

## Tags

Tag transport is governed by `pull.tags` (container → host) and `push.tags`
(host → container) — see [config.md](config.md#pull) and
[config.md](config.md#push) for the config keys. `--tags` / `--follow-tags` /
`--no-tags` override the config key for one run on `jailbee git fetch`,
`checkout`, `pull` and `push` (including its `--merge`/`--rebase`/`--force`
modes); the three are mutually exclusive, and a flag always beats the
configured default.

| policy | container → host (`fetch`, `checkout`, `pull`) | host → container (`push`) |
|---|---|---|
| `none` | no tags cross | no tags cross |
| `reachable` | tags reachable from the fetched branch, lightweight and annotated alike — this is what git's automatic tag-following has always done here, flag or no flag | tags reachable from the pushed branch, computed with `git tag --merged` rather than `git push --follow-tags` (which sends annotated tags only and would silently drop lightweight ones) |
| `all` | every tag the container has | every tag the host has |

`pull.tags` defaults to `reachable`, `push.tags` to `none`: tags have always
flowed container → host implicitly (git's own tag-following) and never the
other way, and these keys make that existing behaviour explicit and
configurable rather than changing it.

**No policy ever re-points an existing tag.** No refspec carries `+`, so a tag
that already exists at the destination keeps pointing where it does; moving
one stays a deliberate manual `git push --force`.

**Conflicting tags behave differently by direction and policy.** Under
`reachable`, container → host silently skips an existing tag through Git's
automatic tag-following. Host → container uses explicit per-tag refspecs, so
a conflicting tag fails the transport. Under `all`, both directions fail.

These transfers are **not atomic**: other transport refs or tags may already
have updated when a tag is rejected. Subsequent merge/rebase or host branch
placement steps do not run. Thus `push --merge --tags` does not reach the
merge, and a tag-fetch failure in `pull --tags` occurs before the host target
branch is merged or advanced, although its `refs/jailbee/...` transport ref
may have moved. Inspect both sides and resolve a conflicting tag deliberately;
do not force-update a tag merely to silence the error.

`jailbee git merge` (container → container) transports no tags on either
leg, regardless of `pull.tags`/`push.tags` or the CLI flags: the relay runs
source → host → target through the same two transports above, and inheriting
the host's tag set would push it into a target container that asked for none
of it.

`jailbee pr` / `jailbee git push --pr` never send tags to the GitHub origin,
and there is no flag to make them — pushing a tag to a shared remote is an
outward-facing, effectively irreversible act.

`jailbee git retarget <name> <base> --merge` does **not** honour `push.tags`
or `push.ff`: it sends no tags and uses the automatic push merge rule.
Normally that permits a regular merge; if the container is already on the
new base branch, it uses the same ff-only/divergence handling as `push --merge`.
There are no tag or ff overrides on `retarget`. The base label and anchors
change **before** the optional merge: a failed merge does not undo retargeting.

## Fast-forward policy

`ff` (`pull.ff` / `push.ff`, or `--ff`/`--no-ff`) controls whether a merge
must fast-forward. Host-side pull can also request a non-fast-forward merge;
container-side merges have the `--no-ff` limitation described above. `auto`,
the default for both keys, means something different in each direction:

| operation | `never` | `auto` (default) | `always` |
|---|---|---|---|
| `jailbee git pull`, merging into the currently checked-out branch | request a merge commit (`--no-ff`) | fast-forward when the host branch is strictly behind, merge commit otherwise — git's own default | demand a fast-forward; error on divergence |
| `jailbee git pull --into <other>` / `--checkout`, target **not** checked out and fast-forwardable | fast-forwarded at ref level | fast-forwarded at ref level | fast-forwarded at ref level |
| `jailbee git pull --checkout`, target diverged | merge commit | merge commit (a fast-forward isn't possible here, so `auto` behaves like `never`) | error — refuses to merge |
| `jailbee git push --merge` | regular merge permitted; fast-forward still possible | ff-only when the container is on the pushed branch, regular merge otherwise; asks (TTY) or errors (no TTY) on divergence in the ff-only case | demand a fast-forward; error on divergence |
| `jailbee git merge` (flag only, no config key) | regular merge permitted; fast-forward still possible | ff-only when the target is on the branch read from the source, regular merge otherwise; asks (TTY) or errors (no TTY) on divergence in the ff-only case | demand a fast-forward; error on divergence |

A target branch that is **not checked out** is fast-forwarded at ref level
under every value of `ff` — that is a ref move (`git.fast_forward_branch`),
not a merge, so `never` cannot force a merge commit there without checking
out a branch nobody asked to check out. `--checkout` adds a courtesy checkout
onto that branch afterwards; it does not change which of the two paths ran.

`jailbee git checkout` stays fast-forward-only regardless of `pull.ff` — its
contract is "fast-forward, or tell me to `jailbee git pull`" — and divergence
there already points at `jailbee git pull`.

`jailbee git fetch` always uses a forced refspec (`+`) into its own
`refs/jailbee/<short>/*` namespace; `ff` does not apply to it. Updating the
container's local source branch after a push is fast-forward-only. On the
host, however, `fetch --force` permits non-fast-forward placement of both
superproject and submodule branches, subject to the checked-out-branch
protections described above.

On the host-side merge path, `pull.ff: never` requests `git merge --no-ff`;
even there, Git creates no commit when the target already contains the source.

**`push`'s `auto` is not `pull`'s `auto`.** `push`'s is the pre-existing
branch-condition rule (unchanged by tag/ff support); `pull`'s fast-forward-
when-possible behaviour is new — see the CHANGELOG for what that changes
against the previous release.

## Merging one container into another — `jailbee git merge`

```bash
jailbee git merge                        # pick the sources, then the targets
jailbee git merge c1 --into c4
jailbee git merge c1 c2 c3 --into c4     # one at a time, stop on conflict
jailbee git merge c1 --into c4 --into c5 # both targets take c1
jailbee git merge c1                     # pick the targets only
jailbee git merge c1 --into c4 --plain   # transport only
jailbee git merge c1 --into c4 --no-ff   # permit a merge rather than require ff-only
jailbee git merge c1 --into c4 -b feat/x # read feat/x from c1
```

Objects travel source → host → target; **no host branch, index or
superproject working tree is touched** (a host sub-repo can still be
created, for a submodule born in the source container that the host has
never seen). The merge runs inside the target on whatever it has checked
out, so conflicts are resolved there, in `jailbee shell <target>`.

Neither end is ever inferred, but either may be left out on a TTY and is
then asked for — the **sources first, the targets second**. Only running,
clone-mode containers are offered: mount mode and a stopped container are
refused at both ends anyway. Both prompts are checkboxes, and the sources are
merged in the order the rows were **listed**, not the order they were ticked
(the prompt says so) — with `-b` the source prompt becomes single-select,
because one branch cannot describe several sources, while the target prompt
stays a checkbox. Off a TTY both ends must be given, and the error names the
ones that are missing.

**A container cannot be named at both ends.** Merging a container into itself
would merge a branch into that same container's checked-out branch, which says
nothing the target's own `git merge` does not say better. Each prompt hides
the rows the other end already holds, and a collision that was typed instead
(`jailbee git merge c1 --into c1`) is refused before anything is merged.

Several sources are merged into each target **one at a time, in the order
given**, and stop there at the first conflict or failure: the next source
would otherwise land on a tree left in merge state. Targets are separate
containers and do not share that constraint, so **a target that stops does not
stop the ones after it**. Every target prints what landed, what stopped it,
what was not attempted, and the command that resumes where it left off — that
report is the reason several sources are allowed at all. With several targets
each block is headed by a rule naming its target, and the run closes with a
roll-up of the state of every one of them:

```
Summary: 1 of 2 targets complete
  c4  stopped  merged c1 — stopped at c2: merge conflicts (c3 not attempted)
  c5  ok       merged c1, c2, c3
```

By default the merge requires a fast-forward when the target is on the same
branch read from the source, and permits a regular merge otherwise. If both
tips have unique commits in the ff-only case, JailBee shows the counts and
asks whether to merge; without a TTY it stops and names `--no-ff`.
`--no-ff` permits that merge up front; `--ff` refuses divergence. As with
`push --merge`, the current container-side implementation does not pass Git's
`--no-ff`, so a merge commit is not guaranteed. See
[Fast-forward policy](#fast-forward-policy).

`--plain` transports the refs only and runs no merge; its report says
"transported", not "merged" — do not read `--plain` as a kind of merge.
`--ff`/`--no-ff` with `--plain` is exit 2.
`-b`/`--branch` reads a specific branch from the source and only applies with
exactly one source. **It does not carry submodules reliably** — the transport
enumerates the source container's *checked-out* state, not the branch being
read; see [Submodules](#submodules).

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

**On `jailbee git fetch` / `checkout` / `pull` / `push` / `merge`.** Submodule
objects travel over the same `ext::` transport the superproject uses. A
sub-repo the peer is missing is normally created there first. Remote-SSH
sessions do not create a newly introduced host submodule working tree: they
warn and skip that submodule's transport, leaving it for a local host run.
A `SubmoduleError` stops the operation, but does not roll back refs or merges
that already succeeded; the missing-object verification may happen only
after the superproject changes. Every
`submodule update` JailBee runs passes `--checkout`, so a submodule declared
`update = none` is checked out and transported like any other, and a
configured `merge` or `rebase` mode is replaced by a checkout too. `fetch`
additionally attempts to place each same-named submodule branch at the
container's state without switching its working tree. Read the placement
report: a checked-out or diverged branch may be left alone.

**What you see.** `jailbee pull` prints a delimited `── Submodules` block
after git's own output — per submodule `new → <sha>`, `<sha> → removed`, or
a commit count with insertions and deletions — so a gitlink that moved is
never buried in the superproject's diff. `jailbee git merge` prints the same
block once per source, comparing the target's HEAD before and after that
integration. It reads the commits inside the target, where any newly created
merge commit exists; a fast-forward need not create a merge commit at all.

**Path limitation.** Module paths containing spaces are not supported by the
transport ref namespace (`refs/jailbee-sub/<source>/<path>/...`), because Git
ref names cannot contain spaces. This is distinct from declared module names:
a name containing spaces or `=` works when its module path has no spaces.

**Container-side limitation.** The transported submodules for `jailbee git
merge`, `fetch`, `pull` and `checkout` are enumerated from the container's
checked-out state, not from a branch selected with `-b` / `--branch`. A
submodule that exists only on that other branch, or a gitlink commit that no
local sub-repo branch reaches, may therefore be absent. The superproject merge
can succeed despite a missing submodule commit: a gitlink is a tree entry, not
a guarantee that its object is present in the sub-repo. A later
`submodule update --init --recursive --checkout` can fail **after the
superproject has advanced**, reporting a missing commit or uninitialized
submodule. It can also fall back to upstream when that endpoint and the
necessary credentials are available, whether on the host or under the
container's egress policy. Strict mode does not universally forbid this; it
depends on the allowlist. Upstream cannot supply a commit never pushed there.

Use `-b` only when you know that branch's submodule set matches the container's
checkout — for a repo with no submodules it is unaffected. To move a branch
that is not checked out *with* its submodules, check it out in the container
first (`jailbee shell <name>`, `git checkout <branch>`), then run the bridge
command without `-b`.

**Submodules that exist on some branches only.** A host sub-repo lives in its
gitdir (`.git/modules/<name>`); the working tree is optional. Switching the
host to a branch without a submodule (`jailbee branch`, `jailbee git checkout`,
or a pull that checks out or merges) removes that submodule's files and keeps
its gitdir — branches, remotes and stash included — so switching back restores
it. Ignored files (`.env`, build output) are kept and listed. The switch is
refused before anything changes when a vanishing submodule has uncommitted or
untracked changes. A pull brings a submodule the host has not seen into a new
gitdir without touching the working tree, whatever branch the host has checked
out.

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

**Branch placement.** `jailbee branch` checks out the host superproject
branch when requested and attempts to align submodules recursively. It is
purely local — it moves nothing between host and container — and works on either side:
with no `--container` it works on the host repo, with `--container <name>`
on that container.

```bash
jailbee branch                              # host, align to current branch
jailbee branch master                       # host, whole tree to master
jailbee branch master --submodules-only
jailbee branch --container feat-foo         # container 'feat-foo', its branch
jailbee branch master --container feat-foo
```

`jailbee submodule checkout` is a hidden alias kept for compatibility; it
prints a pointer to `jailbee branch`.

On the host, a BRANCH argument checks that branch out in the superproject
first and then aligns the submodules to it — one command to jump the whole
tree back to `master` and out again, the counterpart of `jailbee git checkout
<container>` (which does the same thing towards a container's branch).
`--submodules-only` leaves the superproject where it is: a deliberate
mismatch, or a detached HEAD you want to keep. A container's branch is its
identity, so BRANCH with `--container` never switches it — there it is pure
submodule placement, and `--submodules-only` combined with `--container` is
rejected (exit 2): there is nothing for it to opt out of.

Placement never rewinds a submodule branch. When a submodule's local branch
is ahead of the gitlink recorded in the superproject — a submodule commit
published without bumping the pointer — the newer branch stays checked out
and the run warns instead: bump the gitlink with `git add <sub> && git
commit` in the superproject. Dirty submodules, missing gitlink commits and
branches that diverge from the recorded gitlink are warned about rather than
forcibly aligned. Check the branch report for anything left unchanged.

**Before you destroy.** `jailbee destroy`'s pre-flight check counts a changed
submodule as work at risk — added, removed, committed ahead, or merely dirty
— and names it in the summary, so a container is not thrown away because
only its sub-repo held the change.

Each submodule also carries a base anchor, seeded from the gitlink recorded
at the superproject's `refs/jailbee/base/<base>`. Submodule PR candidate
counts use this anchor (falling back to `origin/HEAD` if missing). Status
instead compares committed gitlinks against the **live host target**, so
these views can differ on a stacked branch or after the host base advances.

**Submodule pull requests.** `jailbee submodule pr [<name>] [<path>]` opens or
updates a PR in a submodule's own GitHub repository — a separate repo from the
superproject, so a separate PR from `jailbee pr`. Its signal is that same base
anchor, never the superproject's gitlink diff `jailbee ls` uses: when you've
committed inside a submodule but not yet committed the gitlink bump in the
superproject, the gitlink diff reads zero while the anchor sees exactly the
commits the PR is for. That gap is reported as information, not an error.

```bash
jailbee submodule pr feat-foo              # auto-target, draft PR
jailbee submodule pr feat-foo libs/foo     # explicit submodule
jailbee submodule pr feat-foo --ready      # mark ready for review
```

Base and head come from the submodule's own data, not the superproject's:
base is `--base` > `submodule.<name>.branch` declared in `.gitmodules` (found by
descending from repo root, unless `.`) > the sub-repo's `<remote>/HEAD` > `main`; head
is `--as` > the configured agent's proposal > the branch the commits were
read from. The remote is resolved per submodule, since a submodule may name
its upstream something the superproject doesn't.

Before the superproject push, `jailbee pr` offers submodules with commits ahead
of their own base, a recorded submodule PR, or a pending outbox description for
their repo. On a TTY, a checklist starts with all candidates ticked; `--yes`
takes all. Off a TTY without `--yes`, they are skipped with a warning.
`--no-submodules` opts out. `--no-ai`, `--no-outbox` and `--ready`/`--draft`
apply to the submodule PRs too; other flags apply only to the superproject.
A submodule failure does not automatically abort superproject publication.
On a TTY without `--yes`, JailBee asks whether to publish it anyway; declining
stops before its push. Continuing still leaves the run with exit 1.
Submodule outbox partial failures also exit 1, keeping the published PR's
recorded identity so a retry updates it rather than opening another PR.

Both `jailbee pr` and `jailbee submodule pr` refresh cross-links: a
`<!-- jailbee:submodule-prs -->` block in an authored superproject PR and a
`Part of …` block in authored submodule PRs. Only the managed blocks change;
an unchanged block causes no body edit. Foreign PR bodies are left untouched.
Merge the submodule PRs first, so the superproject's gitlink points at merged
commits; the merge order is advice, not a gate.

## Stacked PRs

When PR1 (`feat/a`) is waiting for review and PR2 builds on it, base the
second container on the first branch. First bring A's commits to the host:
a branch existing only in container A cannot be the source of `jailbee new`.
Keep chain maintenance merge-based rather than rewriting branches with work
stacked on them. Publication runs on the host, with host credentials.

This example assumes `main` is the base and `origin` is the upstream. Run the
commands on the host, returning there after working inside each container:

```bash
jailbee new feat/a --no-attach
# ... commit work inside feat-a, then return to the host ...
jailbee git checkout feat-a
git push -u origin feat/a
gh pr create --draft --head feat/a --base main

jailbee new feat/b feat/a --no-attach
# ... commit work inside feat-b, then return to the host ...
jailbee git checkout feat-b
git push -u origin feat/b
gh pr create --draft --head feat/b --base feat/a

# After committing a review fix inside A:
jailbee git checkout feat-a
git push origin feat/a
jailbee git push feat-b --from feat/a --from-local --merge

# After PR1 merges on GitHub:
git checkout main
git pull --ff-only
jailbee git retarget feat-b main --merge
# Verify the result and any remaining work/proposals before cleanup:
jailbee destroy feat-a
```

Retargeting changes JailBee's container base, not PR2's base on GitHub.
Inspect PR2 and update its GitHub base separately if needed. A squash or
rebase merge of PR1 rewrites ancestry; verify PR2's commit list and diff
rather than assuming that merging `main` removes all parent commits.

For longer chains, repeat the propagation per link: `jailbee git checkout
feat-b && git push`, then `jailbee git push feat-c --merge`.

## Merging several containers through one

To combine features developed in parallel, you can integrate them in one
container and run the project's tests there before bringing the result to the
host. This avoids using the host working tree for the intermediate merges.
JailBee does not run tests or lint automatically unless you configured them;
check the combined result before publishing or cleaning up.

Since all three sources are themselves containers here, `jailbee git merge`
does this directly, without a host checkout for each source:

```bash
jailbee new feat/a --no-attach
jailbee new feat/b --no-attach
jailbee new feat/c --no-attach
#   ... work in each container ...

jailbee git merge feat-a feat-b --into feat-c   # one at a time, stop on conflict
#   conflict? resolve inside feat-c, run the gates there, commit the merge,
#   then re-run from the resume command the summary prints

git checkout main
jailbee git pull feat-c --current    # all three features land on main
```

### The long way — through `push --current`

The equivalent using only commands that predate `jailbee git merge`, and still
what to reach for when a source is a plain host branch with no container of
its own:

```bash
jailbee git checkout feat-a          # host HEAD → feat/a, ff-only, from the container
jailbee git push feat-c --current    # feat/a into container c, merged into its branch
jailbee shell feat-c                 # resolve, run the gates, commit the merge
exit                                # return to the host before the next bridge command

jailbee git checkout feat-b
jailbee git push feat-c --current
jailbee shell feat-c
exit                                # return to the host

git checkout main
jailbee git pull feat-c --current    # all three features land on main
```

`--current` is what makes this work. `push`'s default source is
`default_source: base` — the container's *base* branch — so a bare
`jailbee git push feat-c` would send `main` into container c, not `feat/a`.
`--current` (like `--pr`) also resolves the source locally and skips the host
fetch, which matters here because `feat/a` may exist nowhere but the host and
its own container.

The action comes from `push.default_action`. Its built-in default is `ask`, so
the commands above open a picker and you choose *merge*; with
`default_action: merge` configured they merge with nothing extra typed. `ask`
needs a terminal, though — run this recipe from a script and it exits with
*"push.default_action is 'ask' but no TTY is available"*, so a scripted version
has to spell the flag out. Either way it must be a merge or a rebase — `plain`
only transports
`refs/jailbee/host/<branch>` into the container and never attempts to apply it,
so no conflict ever surfaces to resolve. Spelling the flag out
(`jailbee git push feat-c --current --merge`, as `## Stacked PRs` above does)
is always unambiguous.

A conflict leaves container c in merge state, exactly as it would on the host.
Resolve it in `jailbee shell feat-c` or `jailbee tmux feat-c` and commit there.

Two things the last `pull` does not cover:

- Cleanup of container c and the merged `feat/c` branch follows
  `pull.destroy_container` and `pull.delete_branch` (each accepts `prompt`,
  `always` or `never`), subject to the cleanup safeguards and work-loss
  warning [above](#the-git-bridge).
- Containers a and b are untouched — their commits reached `main` through c,
  not through their own pull. Destroy them yourself when the branch is merged:
  `jailbee destroy feat-a`, then `jailbee destroy feat-b` (one name per
  invocation; with no name and a TTY you get a picker).

## Choosing the starting point for `jailbee new`

`<base>` always names the container's **base branch** — the
`user.jailbee.base_branch` label, transport anchor `refs/jailbee/base/<base>`,
and host target used by status and `jailbee git pull`. Whether
`jailbee new` *forks* off it depends on whether `<branch>` already exists in
the source repo:

| Invocation | `<branch>` in source? | `<base>` in source? | Result |
|---|---|---|---|
| `jailbee new X` | no | — | clone default branch, `checkout -b X`; base = default branch |
| `jailbee new X` | yes | — | clone `X` directly (review/test); base = default branch |
| `jailbee new X Y` | no | yes | clone `Y`, then `checkout -b X`; base = `Y` |
| `jailbee new X Y` | yes | yes | clone `X` directly; base = `Y` (confirmed first, `-y` skips) |
| `jailbee new X Y` | (any) | no | error — base Y not in source (run `git fetch origin Y`) |

Here, a branch can exist locally or as a remote-tracking ref. An explicit
base must already resolve: JailBee does not fetch a wholly missing base to
make it valid. Origin-mode cloning applies when the selected source is the
repo's default branch with no explicit base and `new.clone_from: origin`
(the default), or when the source has only a remote-tracking ref. On that
path, `new.autofetch: true` refreshes the source on the host; a fetch failure
aborts creation. An explicit local base or an existing local feature branch
is cloned locally without that refresh. See [creation source
settings](config.md#new).

The fourth row gives an existing branch the intended comparison base at
creation time; `jailbee git retarget` changes it afterwards. The base label
does not mean an existing branch has been rebased or merged onto that base.

**Shortcut:** `jailbee new --current` uses the host's checked-out branch as
the container branch. `jailbee new X --current` instead creates or checks out
`X` with the host's current branch as its base. `--current` cannot be combined
with an explicit BASE positional and fails on detached HEAD.

## Background creation

By default, `jailbee new` runs creation and cloning in the foreground.
Autostart may hand off to a background supervisor at a stage with
`detach: true`; `--wait` waits for all stages, while `--no-wait` hands off
after the first. Pass `--background` (`-b`) to detach the creation itself
after host-side preflight and confirmation:

```bash
jailbee new feat/foo --background
```

Track progress with `jailbee ls`: the `JOB` column shows creation phases
such as `creating`, `cloning` and `autostart`, or `autostart:<stage>` after
handoff to stage supervision. The job disappears on success. A failed job
shows `failed`; a worker that vanished is marked separately. Inspect its log
with `jailbee job log <name>`; worker logs live under
`${XDG_STATE_HOME:-~/.local/state}/jailbee/logs/`.

A failed creation leaves any container already created for inspection; an
early failure may leave only a job record. `jailbee shell <name>` offers to
enter an existing container despite a failed creation or boot job. A failed
detached autostart supervisor does not gate attachment. After inspecting or
repairing the failure, clear the record with `jailbee job clear <name>`;
this neither repairs nor deletes the container, and refuses a nonterminal job
whose worker is still alive.

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

`jailbee start` and `jailbee restart` take the same `--background` / `-b` /
`--no-background` flags, tracked the same way (phases `starting` →
`autostart`). There the slow part is the `on_start` autostart run, not the
boot, and one config key — `boot.background: true` — makes both detach by
default. A detached boot is refused while another background job for that
container is still live, since two of them would interleave their autostart
steps.

## Mount mode vs clone mode

`jailbee new <branch>` is **clone mode**: the host repo is `git clone --shared`'d
into `/home/dev/<repo>` inside the container. Its container name is derived
from the branch or set with `--name`; use the short name shown by
`jailbee ls`. The container has its own
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
- Concurrent writes from the host or multiple mount-mode containers are
  not coordinated by JailBee. Git's lock files protect some Git operations,
  not arbitrary edits or build tools; use one writer at a time to avoid lost
  changes or corrupted outputs.
- Isolation does not protect this mounted directory: a command in the
  container can modify or delete the host's files, including uncommitted work.

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

Without a name on a TTY, `--pr` selects one running clone-mode container
with a `user.jailbee.pr` label (or uses the only eligible container). Name
the container explicitly in scripts. The action flag is optional: drop it
and the merge/rebase/plain choice follows
`push.default_action`, which defaults to `ask` (see
[Configuration](config.md#push)) — a prompt on a TTY, and an error naming
the config key off one. Pass the flag in scripts.

Both dashboards offer this as **"Refresh from PR head"** in the action
menu, on review containers only: a PR JailBee opened from the container's
own branch has its head downstream of the container, where the refresh
could only be a no-op. The Qt dashboard asks the action in a dialog and
never asks for a source, because `--pr` already is one.

Publish your own commits — either into the PR, or as a PR of your own:

```bash
jailbee pr <name>                        # asks once (menu), then updates PR #N
jailbee pr <name> --yes                  # non-interactive: push into PR #N's head
jailbee pr <name> --stacked --as fix/x   # non-interactive: open a PR based on PR #N
```

The first run on a review container asks what to publish, because both
answers are legitimate:

```
Container 'review-1234' was created from PR #1234 by @alice (OPEN);
  head 'alice/work-type' → base 'main'.
? What should jailbee publish?  (Use arrow keys)
 ❯ push these commits to PR #1234's head 'alice/work-type'
   open a NEW PR based on 'alice/work-type'  (stacked)
   cancel
```

The confirmation is recorded on the container (`user.jailbee.pr_adopted`), so
later `jailbee pr` runs push new commits without asking again. Fork PRs are
refused: their head lives in another repository, so pushing to `origin`
would create an unrelated branch instead of updating the PR.

Off a TTY the choice must come from a flag — `--yes` or `--stacked` — since
neither can be guessed; `--yes` keeps the meaning it had before `--stacked`
existed.

### A PR against the PR — `--stacked`

Reviewing a PR often produces work of its own: a fix on top of the author's
branch, a test they asked for. `--stacked` publishes it as a PR **based on**
PR #N's head branch rather than pushing into it, so the author reviews your
commits separately and merges them into their own branch.

```bash
jailbee pr review-1234 --stacked --as fix/worktime-review
```

That PR is JailBee's own, so it needs a head branch of its own: `--as`, or
the configured agent's proposal (confirmed on a TTY). Publishing under the
reviewed PR's head is refused with exit 2 — it would silently update that PR instead. This
is what `--no-ai --stacked` hits, since the proposed name then defaults to
the container's branch, which *is* that head.

It is recorded separately, under `user.jailbee.stacked_pr` /
`stacked_pr_branch` / `stacked_pr_author` / `stacked_pr_base`.
`user.jailbee.pr` goes on naming the **reviewed** PR, which is the point:
`jailbee ls`'s PR column and `jailbee git push --pr` ("refresh from PR
head", above) keep tracking the parent, so the author's new commits still
come to you over the bridge. Later `jailbee pr` runs read the stacked labels
and update your PR with no question asked, and `--open` prefers it over the
parent.

Two more things follow from the container's own base anchor being the
reviewed PR's *base* (e.g. `main`), not its head:

- Opening a stacked PR offers to **retarget** the container onto the PR head
  (`--retarget` / `--no-retarget`; the default asks on a TTY and otherwise
  skips, printing the command). This changes the container's recorded base;
  status and diff then resolve that branch against the host's live local (or
  last-fetched tracking) ref. If that target is available, its ↑ count measures
  unique container commits from that target; without a matching host ref,
  comparison status is `?` rather than a PR-anchor fallback. The fetched PR
  head itself is stored in JailBee's `refs/jailbee/pr/<N>/head`, not necessarily
  a host branch.
- When PR #N merges, `jailbee git retarget <name> main` moves your stacked PR's
  container onto the merged base as usual.

A **fork** PR cannot be stacked on: its head is not a branch in your origin,
so it cannot be the base of a PR opened there. JailBee says so and names the
fork branch to open the stacked PR against instead. `--stacked` is also
refused on a container that already publishes to a PR's head (adopted or
JailBee-authored) — that head is fixed, and a stacked PR needs its own — and
on a container that was never created from a PR, where `jailbee new <branch>
<base>` is the way to base work on another branch.

### Updating an adopted PR

When publishing into the reviewed or adopted PR's head (not your separately
created stacked PR), JailBee keeps these hands-off rules:

- The **description is not regenerated automatically**. Use `--description`
  to request agent regeneration, or `--title`/`--body` to supply text.
  The usual interactive regeneration offer is suppressed. A pending outbox
  description explicitly naming this PR may also replace its text after
  interactive confirmation; without a TTY it remains pending.
- **`--force` asks a second time**, naming the head branch it would
  overwrite; `--yes` skips that too, and without a TTY it is an error.
- **`--as` is rejected** (exit 2). That holds for any container with a PR,
  jailbee-authored ones included: the PR's head branch is fixed, so pushing to
  a different name would leave the PR untouched. On a review container that
  has not decided yet, the rejection waits for the choice: `--as` is legal
  with `--stacked` (it names the new PR's head) and refused — before any push
  or label write — when the run adopts.

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

Without this, the AI-proposed head branch name (`pr.ai_branch`) would
publish the work under a *new* branch and `gh pr create` would happily open a
duplicate PR for it.

## GitHub CLI (`gh`) inside containers

The `gh` binary is baked into every container's golden image. To make
it authenticate (for AI agents like Claude that call `gh pr view`,
`gh issue view`, etc. — **read-only** calls; see below), switch it on in
`~/.config/jailbee/global.yaml`:

```yaml
github:
  enabled: true
```

and give each repo its token in that repo's host-local file,
`~/.config/jailbee/repos/<container_prefix>.yaml`:

```yaml
github:
  token: github_pat_AAA...
```

Each container gets exactly one token — its own repo's. GitHub fine-grained
PATs are scoped per **resource owner** (one user or one org), so repos of
different owners need different tokens; repos of one owner may share one.

Before 1.6.0 the tokens lived in a `github.api_tokens` map in `global.yaml`.
That map still loads but is deprecated and removed in 2.0.0;
`jailbee config migrate --apply` moves each entry into its repo's host-local
file.

**Recommended PAT shape:**

1. github.com → Settings → Developer settings → Personal access tokens
   → Fine-grained tokens → Generate new token.
2. Resource owner: pick the org (or your account) whose repos the
   agents will touch from this container.
3. Repository access: **Only select repositories** → pick your work
   repos for that owner.
4. Repository permissions — **read-only, all four**:
   - Contents: Read
   - Issues: Read
   - Pull requests: Read
   - Metadata: Read
5. Copy the token (`github_pat_...`) into the repo's host-local file as
   `github.token` (`jailbee config edit --local` opens it).

Protect the file with `chmod 600 ~/.config/jailbee/repos/<container_prefix>.yaml`
(replace the placeholder with the actual prefix). Insecure permissions produce
a config-load warning, not a refusal; `jailbee doctor` reports them as a failed
check. Doctor also flags an empty value or a classic PAT (`ghp_*`, which cannot
be scoped to selected repositories). Its read-only permission check is only
an informational reminder: JailBee does not verify or reduce the token's
*effective* permissions.

JailBee writes the configured token into `/etc/profile.d/jailbee-github.sh`
inside the container for login shells to export as `GH_TOKEN`. It is readable
by container processes, not confined to the `gh` binary. New/start/restart and
apply paths refresh this file when a token is configured; disabling the
integration or removing the config value does not erase a previously written
token. Revoke an exposed token at GitHub and remove stale container copies.

**Two identities, on purpose.** Supply a read-only token for the container
and keep the host's publishing credentials separate. The intended workflow
stages issue and PR mutations as JSON manifests in `~/.jailbee/issue-outbox`
or `~/.jailbee/pr-outbox`; a human reviews and publishes them on the host
with `jailbee issue apply`, `jailbee review apply` or `jailbee outbox apply`,
using the host's independently authenticated `gh`.

This separation depends on the credentials you grant. Strict networking
allows the configured GitHub API endpoint; it does not distinguish read
requests from writes. A write-capable token or other credentials available
inside the container can bypass the outbox review step. See the
`jailbee-issue-management` and `jailbee-pr-review` skills for the intended
read-only-container workflow.

The `github` block must never live in a repo's `.jailbee/config.yaml` —
`jailbee` rejects it at the repo layer so tokens can't leak via git commits.
See [`config.md`](config.md#github) for the
full field reference, the `0600` permission requirement, and the
`jailbee doctor` checks.

## Unified proposal management

`jb outbox` brings PR and issue proposals into one view without moving files
or changing manifest schemas. Physical stores remain
`/home/dev/.jailbee/pr-outbox/` and `/home/dev/.jailbee/issue-outbox/` inside
the container (`~/.jailbee/pr-outbox/` and `~/.jailbee/issue-outbox/` for its
user). New containers bootstrap both directories as the container user,
before agents run, including with `--no-clone` or `--no-autostart`. Existing
containers are not retroactively repaired; an agent may use idempotent
`mkdir -p` there. Inspection never creates missing directories and makes no
`gh` or network calls.

```bash
jb outbox                          # terminal overview and drill-down
jb outbox smoke                    # shorthand for outbox browse smoke
jb outbox browse ls                # escape a container/subcommand collision
jb outbox ls --all-repos -o json
jb outbox show smoke pr/001-review.json -o json
jb outbox drop smoke pr/001-review.json --action 0 --comment 1
jb outbox drop smoke issue/001-triage.json --action 0 --with-dependents
```

Logical IDs are `pr/<filename>.json` and `issue/<filename>.json`, not paths
and not GitHub IDs. Action and inline review-comment selectors are
**zero-based**. `--comment` requires `--action` on a PR `review` action.
With neither selector, `drop` deletes the whole manifest; removing the last
action deletes it too. Deleting an issue `create` with `issue_ref`
dependents is refused unless `--with-dependents` explicitly includes them.
The terminal browser asks about that cascade, then confirms the exact scope;
Qt shows the expanded scope in its confirmation dialog. Referenced body
files are retained while another pending manifest in the same store still
uses them. Surviving JSON fields are preserved, not rebuilt from defaults.

Selective deletion is an edit, allowed only before any recorded publication
progress. PR progress sidecars/receipts and issue journals block edits;
malformed or unreadable evidence is not treated as empty. For a whole issue
manifest with settled progress only, `--archive-journal` permits local
deletion. First run `jb outbox show <container> issue/<manifest>.json` and
inspect its receipts and pending actions before approving whole-manifest
archival deletion. Direct `outbox drop` previews deletion scope, not receipts
or action content. Uncertain outcomes must first be reconciled with the
existing `jb issue resolve` command. There is no general text editor in the
browser.

`show` includes the revision token, raw JSON, full proposed text and available
receipts. `ls`/`show` support `-o json` (also `--format`/`--output`) for
structured inspection. A stopped or inaccessible container is
**unavailable**, not empty; missing stores and rejected unsafe files are
reported separately. Overview exits 2 if any container is unavailable,
while still emitting its structured result.

A human on the host can approve exactly one manifest:

```bash
jb outbox apply smoke pr/001-review.json --revision TOKEN
jb outbox apply smoke issue/001-triage.json --revision TOKEN
```

`TOKEN` is the revision from inspection. `--revision` on `drop` or `apply`
refuses a changed proposal, including a changed referenced body or progress
evidence: refresh and inspect again, do not blindly reuse child indices.
The browsers carry that revision automatically. Publication always covers
**all pending actions in the selected manifest**, even when an action or
comment is highlighted; there is no publish-all-across-manifests operation.
Existing repository/PR/head gates, issue `expected` checks, host identity,
confirmation, receipts, resume and cleanup rules remain authoritative.
`--force` overrides PR stale anchors only and is invalid for issues.
`--yes` skips confirmation only; `--dry-run` publishes nothing but publication
preflight can still read GitHub, unlike inspection.

A PR manifest normally names a PR the container owns: the one it was created
from (`jb new --pr`), opened (`jb pr`), adopted (`jb pr --pr N`) or stacked
(`--stacked`). It may also name **any other PR of the same repository** — a
description fix or a reply on a neighbouring PR, say. The repository lock
still holds; only the ownership check is relaxed, and never silently. The plan
ends with a highlighted warning naming the PR, its author and the container,
directly above the confirmation, which on a terminal is the consent. With `-y` nobody reads
that warning, so `-y` alone refuses such a manifest: add `--foreign` (on
`jb outbox apply` and `jb review apply`).

The unified outbox (`jb outbox apply`, the terminal browser and both
dashboards' Outbox entries) goes one step further: a numbered PR manifest may
name a PR in **a repository that is neither the superproject nor one of its
submodules** — a description fix on a plugin repository the change also
touches, say. Its plan warns that the repository is not the container's, and
the same confirmation or `--foreign` is the consent. A `pr: null` manifest is
never sent to another repository, and `jb review apply` and `jb pr` keep the
repository lock. For issue manifests, `--foreign`
acknowledges a different boundary: a repository outside the superproject and
its submodules, as described [below](#targeting-the-superproject-or-a-submodule).
The terminal
browser and both dashboards' publication terminals ask under the plan, so
they need no flag. Publication asks exactly once, under the plan: the
terminal dashboard's Publish… and the browser's publish entry open the plan
without a question of their own. `jb pr`'s post-push
offer never publishes to
another PR: it holds such a manifest back and names
`jb review apply --foreign <container>`. `jb review ls` lists it with the state
`not bound`.

The terminal browser offers containers, proposals, actions and comments,
with Back, Refresh and Exit at each level. Without a TTY, the shorthand
prints an overview rather than prompting. The terminal dashboard's
**Outbox** entry lists the container's proposals in the same picker panels as
its other menus: a proposal offers Show (paged), Publish… and Delete….
A description awaiting a PR offers **Create PR…** instead of Publish…:
it opens the existing `jb pr` or `jb submodule pr` flow for the resolved
repository scope, with that command's own checks and confirmations. An
unresolved scope is not offered for creation. Publish runs the explicit
`outbox apply` pinned to the listed revision, which asks under its plan;
Delete is confirmed in the dashboard with "No" first and runs `outbox drop`
pinned to the listed revision. **Browse actions & comments…**
opens this browser for deleting a single action or comment. Over
remote SSH each entry appears only when the session's policy permits its
command. The local Qt dashboard instead opens a native non-modal tree with
plain, read-only proposal text, Refresh and Delete selected. Qt publication
opens a host terminal running the exact selected `outbox apply` with its
revision; that terminal owns the authoritative approval. Launching it is
not evidence of publication. Refresh after returning. Closing during load
hides the dialog while its owned worker finishes; shutdown waits for workers
rather than terminating them.

Over remote SSH the browser is always read-only, even in unrestricted mode.
Shorthand is policy-checked as `outbox browse`; use explicit `outbox drop`
or `outbox apply` commands for mutations, each subject to remote command and
host-protection policy. Native Qt is not an SSH browser.

The existing `jb review ls/show/apply/drop`, `jb issue
ls/show/apply/drop/resolve`, `jb pr` and `jb submodule pr` workflows remain
available. The unified interface does not authorize agents to publish:
agents stage proposals; humans inspect and approve on the host.

## Issue management

With the recommended read-only token, a container's `gh` can inspect issues
but cannot mutate them through that credential (see [GitHub CLI (`gh`)
inside containers](#github-cli-gh-inside-containers)). Stage proposed issue
creates, edits, comments, labels and state changes as JSON manifests in
`~/.jailbee/issue-outbox/`; a human reviews and applies them from the host
with `jailbee issue apply`. The manifest schema itself is normative in the
**jailbee-issue-management** skill
(`docs/skills/jailbee-issue-management/references/manifest-schema.md`); this
section covers the host-side workflow the schema is applied through.

### Reading, triaging, and writing the manifest

Inside the container, an agent reads the current state with the container's
own read-only `gh` (`gh issue view`, `gh issue list`, `gh label list`) before
proposing any change — an `edit`/`labels`/`state` action carries an
`expected` block copied from that read, and the host refuses to apply
anything whose `expected` no longer matches what is actually on GitHub (see
[Stale-`expected` refusal](#stale-expected-refusal) below). The manifest is
written into `~/.jailbee/issue-outbox/NNN-<topic>.json`, one file per batch
of related actions; prose longer than roughly 20 lines goes into a sibling
`.md` file referenced by `body_file` instead of inline. Nothing reaches
GitHub until a human runs `jailbee issue apply` on the host — the container
filesystem is the only place the proposal exists until then.

### Targeting the superproject or a submodule

Every action names its own `repo`:

- `"."` for the superproject.
- A declared submodule's path relative to the superproject root, such as
  `"packages/lib"`.
- `"github:owner/name"` for an explicit GitHub repository. A bare
  `"owner/name"` is not a GitHub-slug target.

Unlike the PR review outbox (one `repo` per manifest), an issue manifest may
mix these targets action by action. The host resolves local paths from its
own configuration: the configured upstream for `.`, and a declared
submodule's own upstream remote when checked out on the host, otherwise its
`.gitmodules` URL. Submodule remote detection tries the sole remote, then
`origin`, `remote.pushDefault`, the current branch's tracked remote, and a
unique `refs/remotes/<r>/HEAD`.

An explicit `github:` slug matching a known local repository uses that
repository's identity. Any other valid slug is a **foreign repository**:
the preview names it, and interactive confirmation must approve it. With
`--yes`, add `--foreign` on `jailbee issue apply` or `jailbee outbox apply`;
`--yes` alone refuses fresh foreign actions. The flag acknowledges the
broader target, not extra GitHub permissions: publication still uses the
host's account and remains subject to GitHub's permissions and the normal
proposal checks. Unknown local paths are refused.

### `jailbee issue ls / show / apply / drop / resolve`

```bash
jailbee issue ls [<name>] [-o table|json] [--fields …]
jailbee issue show [<name>] [<manifest>]
jailbee issue apply [<name>] [--manifest <name>] [--yes] [--foreign] [--dry-run]
jailbee issue drop [<name>] [<manifest>] [--archive-journal] [--yes]
jailbee issue resolve [<name>] [<manifest>] [<action>] (--applied --url <url> [--issue <n>] | --retry) [--yes]
```

`jailbee issue ls` lists every pending manifest across this repo's
containers — CONTAINER, MANIFEST, ACTIONS (counts by kind), REPOS, STATE
(`pending`, `in progress`, `uncertain`, or `error`) — read from each
container's own outbox and host journal, **never a GitHub read**. Naming a
container narrows the listing to it; omitting it does **not** open a
picker the way most `jailbee` commands do — it lists everything at once. A
stopped container's outbox cannot be read at all, so it is named in a note
under the table rather than shown as empty. `jailbee issue show` prints a
manifest's full proposed text and journal state, never truncated, for
actually reading a body before deciding. On a terminal the Markdown bodies are
wrapped to the window and coloured; HTML tags and image URLs are shown as text
rather than hidden, and piped output is the raw text.

Pending manifests are also visible without running `issue ls`:
`jailbee ls`'s ISSUES column shows `✉N`. The dashboards use a combined
OUTBOX column by default and an **Outbox** menu for inspecting proposals and
publishing one manifest at a time. `jailbee destroy`'s guard names unapplied
issue actions alongside other work at risk.

### One combined preview and approval

`jailbee issue apply` builds a single plan covering **every selected
manifest and every repository it touches** — not one confirmation per
manifest or per action — and prints it in full (host GitHub login, then per
manifest, per repository, every action's before/after text) before asking
once, `Proceed?`. Declining, or `--dry-run` (which stops right after the
plan with no confirmation, no recheck, and no mutation at all), leaves the
outbox and GitHub exactly as they were. Naming no container (and no
`--manifest`) auto-picks the one running container in this repo with
anything pending, and only asks when more than one does — off a TTY with
several candidates it errors, naming them.

### `--yes` and non-TTY behavior

`--yes`/`-y` (on `apply`, `drop`, and `resolve`) skips only the confirmation
prompt; it never skips the read-only checks, and on `apply` the post-
confirmation stale recheck below always runs regardless. Without `--yes`,
and without a TTY to ask on, every one of these commands refuses outright
(exit 2) rather than proceed silently — there is no safe default answer for
"should this write to GitHub" or "should this delete a proposal".

### Stale-`expected` refusal

Two checks guard the fields a pending proposal declares in `expected`.
While building the plan, JailBee compares those action-specific fields with
a live issue read. A mismatch refuses the **whole selected batch**. A
pending action whose `issue_ref` points to a not-yet-published `create` is
instead checked against the proposed issue's initial title, body, labels
and open state; there is no live issue to read yet.

After confirmation (or immediately under `--yes`), JailBee re-reads pending
actions that have a known issue number and declared expected fields. A
mismatch aborts before any mutation. Unrelated fields and already-applied
actions are not rechecked. This reduces the approval-time race; it is not
an atomic GitHub transaction, and changes after the last read remain possible.

### Stop/resume behavior and manifest-local `ref`s

`jailbee issue apply` dispatches one manifest's actions in order and stops
the entire run at the first action it cannot durably resolve one way or the
other — nothing later in that manifest, or in any manifest after it in the
batch, is attempted. A `create` action's issue number is recorded in the
host journal the instant it lands, keyed by the manifest-local `ref` a
later action in the *same manifest* names with `issue_ref`. Re-running
`jailbee issue apply` — the ordinary way to resume — skips every action the
journal already shows `applied`, restores that number so a further
`issue_ref` later in the file still resolves, and continues from wherever
the run actually stopped.

### `uncertain` actions and `jailbee issue resolve`

A GitHub mutation whose outcome could not be determined for certain — a
dropped connection, a timeout — is journaled `uncertain` rather than
`applied` or left `pending`, and **`jailbee issue apply` refuses to touch
that manifest again until it is resolved.** `jailbee issue resolve [<name>]
[<manifest>] [<action>]` takes exactly one of (on a terminal, whatever you leave out —
container, manifest, action, mode, URL — is asked for; off one it must be given
as arguments and flags, or the command exits 2):

- `--applied --url <github-issue-url> [--issue <n>]` — the human's own
  confirmation, read off GitHub's UI, of what the mutation actually did.
  `--issue` is required (and must agree with the URL's issue number) for a
  `create` action's resolution, since a create's issue number cannot be
  inferred from anywhere else; it is forbidden for every other action kind,
  whose target issue is already known.
- `--retry` — forget the uncertain record; the action becomes `pending`
  again and is attempted again on the next `apply`.

Either way, the manifest's current proposal and journal state is printed
first, then — absent `--yes` — a confirmation naming the exact action
(`Mark applied …` / `Retry …`).

### Automatic journal archival, and `jailbee issue drop --archive-journal`

Each fully applied manifest is cleaned up before the next manifest starts:
its actions are logged to `~/.jailbee/issue-outbox/applied.log` (identifiers
and receipt URLs, never body text), its outbox files are deleted (a shared
`body_file` only when no other pending manifest references it), and its host
journal is archived, in that order. A cleanup failure stops the batch.

If logging or deletion fails while the manifest remains, retrying `apply`
uses the journal to skip already-applied actions and retry cleanup. If
archival fails **after deletion**, the proposal is already gone and the
active journal remains; ordinary `apply` cannot rediscover that deleted
manifest. Inspect the reported error and retained receipts rather than
recreating the proposal and risking duplicate publication.

For abandoning a manifest by hand instead — one a human decided not to
publish — `jailbee issue drop` refuses by default the instant the manifest
has *any* recorded journal progress at all, even fully settled progress, so
a proposal that already changed GitHub is never silently forgotten.
`--archive-journal` opts into dropping it anyway: it still refuses outright
if any of its actions is `uncertain` (resolve that first), but for settled
progress it prints every applied receipt and every action still pending,
deletes the container's outbox files, and only then archives the journal —
keeping the record of what happened while discarding the rest of the
proposal.

### What enforces read-only container access

GitHub enforces the permissions of the token you supply; JailBee does not
make an arbitrary token read-only. Grant only the read permissions listed
[above](#github-cli-gh-inside-containers), and do not expose the host's
publishing credentials through other mounts or environment variables.
The outbox-and-apply workflow then keeps publication on the **host's own,
independently authenticated `gh`**, with preview and approval. Giving a
container write credentials bypasses that boundary, even in strict network
mode.
