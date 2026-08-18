# Manual testing

The unit tests cover the logic; these recipes exercise the real Incus
integration end-to-end. They require a working Incus daemon, a populated
`<repo>/.jailbee/config.yaml`, and (for some) a Wayland session. Substitute your
own repo/container names for the `SampleApp` / `sampleapp` placeholders used
below.

> **Note on the in-container path:** these recipes assume the default
> `container_prefix` (unset → defaults to `repo_root.name`, e.g.
> `SampleApp`). When `container_prefix` is overridden in
> `.jailbee/config.yaml`, the in-container clone path becomes
> `~/<container_prefix>` rather than `~/SampleApp`. Substitute
> accordingly in the recipes below.

Recommended manual sequence:
```bash
jailbee config validate
jailbee doctor
jailbee init
jailbee registry up        # optional
jailbee base build         # 10–15 min, one-time
jailbee new feat/smoke --no-clone --no-autostart   # least-risk smoke test
jailbee ls
jailbee shell feat-smoke
# inside container, when github.enabled is on:
gh pr list             # sanity-check that GH_TOKEN was injected
exit
jailbee destroy feat-smoke --force
```

## `jailbee git fetch / checkout` smoke test

```bash
jailbee new feat/smoke
jailbee shell feat-smoke
# inside container, on branch feat/smoke already:
cd ~/SampleApp
echo "test" > smoke.txt && git add . && git commit -m "smoke commit"
exit

jailbee git fetch feat-smoke       # should report 1 new commit
git log refs/jailbee/feat-smoke/feat/smoke
jailbee git checkout feat-smoke    # creates host branch feat/smoke, ff-applies
jailbee destroy feat-smoke --force
git for-each-ref refs/jailbee/  # should be empty
```

## `jailbee git pull --cleanup` smoke test

`jailbee git pull` merges the container's branch into the container's **base
branch** (recorded as `user.jailbee.base_branch` at `jailbee new` time) by default,
not the host's current HEAD. Use `--into <branch>` to override the target.

If the base branch has diverged from the host's checked-out branch, `jailbee git
pull` stops and tells you to re-run with `--checkout`. `--checkout`
checks out the base branch and merges, leaving host HEAD on it; it refuses if the host working tree is dirty.

```bash
jailbee new feat/cleansmoke
jailbee shell feat-cleansmoke
# inside container, on branch feat/cleansmoke already:
cd ~/SampleApp
echo "cleanup" > cleanup.txt && git add . && git commit -m "cleanup smoke"
exit

# Default: merges feat/cleansmoke into its base (main) — no checkout needed
# when main is already checked out on the host.
jailbee git pull feat-cleansmoke --cleanup
# expect: git's own "Updating ..." / "Merge made by ..." output
# expect: "Merged 'feat/cleansmoke' from container 'feat-cleansmoke' into 'main'."
# expect: "HEAD now at <oid>."
# expect: "Destroyed container 'feat-cleansmoke'."
# (no host branch created, so no branch-delete line)
jailbee ls                          # feat-cleansmoke gone
git for-each-ref refs/jailbee/      # empty
git log --oneline | head -1     # shows the merge commit

# --into: override the merge target to an explicit branch.
jailbee new feat/intosmoke
jailbee shell feat-intosmoke
cd ~/SampleApp && echo "x" > x.txt && git add . && git commit -m "into smoke" && exit
git checkout -b staging
jailbee git pull feat-intosmoke --into staging
# expect: "Merged 'feat/intosmoke' from container 'feat-intosmoke' into 'staging'."
git checkout main
git branch -D staging
jailbee destroy feat-intosmoke --force

# --checkout: base has diverged from current HEAD; let pull handle the switch.
jailbee new feat/checkoutsmoke
jailbee shell feat-checkoutsmoke
cd ~/SampleApp && echo "y" > y.txt && git add . && git commit -m "checkout smoke" && exit
git checkout -b other-work   # host is NOT on the container's base (main)
jailbee git pull feat-checkoutsmoke
# expect: error — base branch 'main' is not checked out; re-run with --checkout
jailbee git pull feat-checkoutsmoke --checkout
# expect: checks out main, merges feat/checkoutsmoke, stays on main
# expect: "Now on 'main'."
git checkout main   # already there — no-op
git branch -D other-work
jailbee destroy feat-checkoutsmoke --force

# --checkout refused on a dirty tree.
jailbee new feat/dirtysmoke
jailbee shell feat-dirtysmoke
cd ~/SampleApp && echo "z" > z.txt && git add . && git commit -m "dirty smoke" && exit
git checkout -b dirty-branch
echo "unstaged" >> README.md    # dirty host tree
jailbee git pull feat-dirtysmoke --checkout
# expect: "Host working tree is dirty. Stash or commit before using --checkout."
git checkout README.md
git checkout main
git branch -D dirty-branch
jailbee destroy feat-dirtysmoke --force

# Interactive variant — answer the prompts:
jailbee new feat/cleansmoke2
jailbee shell feat-cleansmoke2
cd ~/SampleApp && echo x > x.txt && git add . && git commit -m smoke && exit
jailbee git checkout feat-cleansmoke2   # creates host branch feat/cleansmoke2
git checkout main
jailbee git pull feat-cleansmoke2
# expect: prompts "Destroy container 'feat-cleansmoke2'? [y/N]"
#         and (after y) a "fully merged into 'main', so deleting it loses no
#         commits" note followed by "Delete merged local branch 'feat/cleansmoke2'? [y/N]"
git branch | grep cleansmoke2   # gone if you answered y to both
```

## Stacked PRs (`jailbee git retarget`) smoke test

> Host-only. Exercises the chained-PR workflow: PR2's container is based
> on PR1's branch; when PR1 merges, the container is re-pointed at main.
> Chain maintenance is merge-based — never rebase a branch that has
> something stacked on it.

```bash
git checkout main
jailbee new feat/stack-a
jailbee shell feat-stack-a
cd ~/SampleApp && echo a > a.txt && git add . && git commit -m "a" && exit
jailbee git checkout feat-stack-a        # host branch feat/stack-a (→ push, PR1)

jailbee new feat/stack-b feat/stack-a    # B is based on A's branch
jailbee ls                               # feat-stack-b row shows BASE=feat/stack-a

# Review fix lands in A; propagate one link down the chain:
jailbee shell feat-stack-a
cd ~/SampleApp && echo fix > fix.txt && git add . && git commit -m "review fix" && exit
jailbee git checkout feat-stack-a        # FF host branch (→ push, PR1 updates)
jailbee git push feat-stack-b --merge    # fix flows into B (source = base = feat/stack-a)

# "PR1 merges": merge A into main locally, then retarget B.
git checkout main
git merge --no-ff feat/stack-a -m "merge PR1"
jailbee git retarget feat-stack-b main --merge
# expect: "Container 'feat-stack-b' base branch: 'feat/stack-a' → 'main'."
# expect: push summary — 'main' merged into feat/stack-b inside the container
jailbee ls                               # feat-stack-b BASE=main, MERGE=ok, ↑ counts vs main

# Without --merge, a hint is printed instead:
# expect: "Run 'jailbee git push feat-stack-b --merge' to merge 'main' into the container."

# Negative: unknown base branch is refused.
jailbee git retarget feat-stack-b nonexistent 2>&1 | grep "does not exist on host"

# Cleanup
jailbee destroy feat-stack-a --force
jailbee destroy feat-stack-b --force
git branch -D feat/stack-a
git reset --hard HEAD~1              # drop the local "merge PR1" commit
```

## `jailbee git pull` multi-select smoke test

> Host-only. Multi-select picker requires a TTY.

```bash
jailbee new feat/multi-a
jailbee new feat/multi-b
jailbee shell feat-multi-a
cd ~/SampleApp && echo a > a.txt && git add . && git commit -m "a" && exit
jailbee shell feat-multi-b
cd ~/SampleApp && echo b > b.txt && git add . && git commit -m "b" && exit

git checkout main
jailbee git pull
# expect: checkbox picker — space-toggle both rows, Enter
# expect: two merge summaries, both merged into each container's base branch (main)
git log --oneline | head -3   # two merge commits visible

# Fail-fast variant: introduce a conflict on c, leave a clean
jailbee new feat/multi-c
jailbee shell feat-multi-c
cd ~/SampleApp && echo conflict > a.txt && git add . && git commit -m "c-conflict" && exit
git checkout main
jailbee git pull
# Tick both feat-multi-c (first) and feat-multi-a (second).
# expect: feat-multi-c merge fails → "Stopping batch; 1 not attempted: feat-multi-a"
git merge --abort   # host tree is left in merge state
jailbee destroy --all --force
```

## `jailbee new --mount` smoke test

```bash
jailbee new mountsmoke --mount
jailbee shell mountsmoke
# inside container:
ls ~/SampleApp      # should show the host's working tree
touch ~/SampleApp/sentinel.txt
exit
ls SampleApp/sentinel.txt    # exists on host — RW bind worked
jailbee destroy mountsmoke --force
rm SampleApp/sentinel.txt

# Negative test: fetch/checkout/pull should refuse mount-mode containers
jailbee new mountsmoke --mount
jailbee git fetch mountsmoke 2>&1 | grep "mount mode"
jailbee destroy mountsmoke --force
```

## `jailbee new --pr` smoke test

```bash
# Pick a real open PR in the repo origin points at
gh pr list -L 1 --json number,headRefName

# Create the container; the head lands in refs/jailbee/pr/<N>/head, not a branch
jailbee new --pr <N>

# Verify access to the container
jailbee shell <derived-container-name>

# Verify the label and the fetched head; the host's branches are untouched
incus config get <derived-container-name> user.jailbee.pr   # → "<N>"
git -C ~/SampleApp log refs/jailbee/pr/<N>/head -1

# Works with that same branch checked out on the host (the old refs/heads
# target made git refuse: "refusing to fetch into branch ... checked out at")
git -C ~/SampleApp switch <head_ref> && jailbee new --pr <N> --name prsmoke2
jailbee destroy prsmoke2 --force

jailbee destroy <derived-container-name> --force

# Idempotency: second invocation should be a clean reuse or FF
jailbee new --pr <N>
jailbee new --pr <N>
jailbee destroy <derived-container-name> --force
```

This recipe is for host-side manual verification; it cannot be exercised
from inside a JailBee container session.

## `jailbee new --background` smoke test

> Host-only. Verifies detached creation and `jailbee ls` tracking. State is
> stored in the SQLite `background_op` table under
> `${XDG_STATE_HOME:-~/.local/state}/jailbee/`.

```bash
jailbee new feat/bgsmoke --background
# expect: "🌱 'feat-bgsmoke' is being created in the background (pid N) ..."
# the shell returns immediately

jailbee ls
# expect: feat-bgsmoke row with a JOB column showing creating/cloning/autostart
# Re-run a few times to watch the phase advance; the JOB value clears when ready.

# Tail the worker log for detail:
ls ~/.local/state/jailbee/logs/
tail -f ~/.local/state/jailbee/logs/<prefix>-feat-bgsmoke-*.log

# When JOB clears, the container is ready:
jailbee shell feat-bgsmoke
exit
jailbee destroy feat-bgsmoke --force
jailbee ls   # row gone, no stale JOB entry

# Failure path: force a failure (e.g. a non-existent base) and confirm it surfaces.
jailbee new feat/bgfail nonexistent-base --background
sleep 5
jailbee ls          # expect JOB=failed for feat-bgfail; container (if any) left intact
jailbee job ls      # expect the recorded error and the worker log path
jailbee job clear feat-bgfail          # acknowledge only — container left alone
jailbee ls          # JOB column empty; container still listed if one was created
jailbee destroy feat-bgfail --force   # also prunes the failed job row
jailbee ls          # clean

# Config default + override:
printf 'new:\n  background: true\n' >> .jailbee/config.yaml
jailbee new feat/bgdefault              # runs in background without the flag
jailbee new feat/bgfg --no-background   # forces foreground
# An explicit attach forces foreground too — no --no-background needed.
# Expect: provisioning output in this terminal, then the tmux session.
jailbee new feat/bgtmux --tmux --no-clone --no-autostart
# (detach with C-b d)
jailbee destroy --all --force
# remove the new: block from .jailbee/config.yaml afterwards

# Negative: an explicit --background plus an explicit attach is a conflict.
jailbee new feat/bgx --background --attach shell 2>&1 | grep -i background
jailbee new feat/bgx --background --tmux 2>&1 | grep -- --tmux
# Negative: the attach flags are mutually exclusive.
jailbee new feat/bgx --tmux --shell 2>&1 | grep -i "mutually exclusive"
```

## `jailbee destroy --background` smoke test

> Host-only. Verifies detached destroy + `jailbee ls` JOB tracking. State is in
> the SQLite `background_op` table (`op_kind='destroy'`).

```bash
jailbee new feat/delsmoke --no-clone --no-autostart
jailbee destroy feat-delsmoke --force --background
# expect: "🗑️  'feat-delsmoke' is being destroyed in the background (pid N) ..."
jailbee ls          # JOB column shows destroying/stopping/deleting until the row clears
jailbee ls          # re-run: feat-delsmoke gone, no stale JOB entry

# Batch:
jailbee new feat/dela --no-clone --no-autostart
jailbee new feat/delb --no-clone --no-autostart
jailbee destroy --all --force --background   # one worker per container
jailbee ls

# Attach guard: a container mid-destroy cannot be attached.
jailbee new feat/delwait --no-clone --no-autostart
jailbee destroy feat-delwait --force --background
jailbee shell feat-delwait 2>&1 | grep -i "being destroyed"

# Config default + override:
printf 'destroy:\n  background: true\n' >> .jailbee/config.yaml
jailbee new feat/delcfg --no-clone --no-autostart
jailbee destroy feat-delcfg --force            # backgrounds without the flag
jailbee new feat/delfg --no-clone --no-autostart
jailbee destroy feat-delfg --force --no-background   # forces foreground
# remove the destroy: block from .jailbee/config.yaml afterwards

# Failure path: a failed destroy leaves the container intact with JOB=failed.
# (Trigger by, e.g., revoking delete perms; container remains, row shows failed,
#  re-running `jailbee destroy <name> --force` clears it. To keep the container and
#  drop only the record: `jailbee job clear <name>`.)
```

## `jailbee tmux`/`shell` wait-for-background smoke test

> Host-only. Verifies attach commands block until a backgrounded
> `jailbee new` finishes, then attach.

```bash
jailbee new feat/waitsmoke --background
# immediately, before the worker finishes:
jailbee tmux feat-waitsmoke
# expect: a "⏳ waiting for 'feat-waitsmoke' — creating…/cloning…/autostart…"
# spinner that advances, then drops you into the tmux session once ready.

# The picker path also shows the in-flight row:
jailbee new feat/waitsmoke2 --background
jailbee shell            # no name → picker lists feat-waitsmoke2 with JOB=cloning/...
# pick it → waits, then opens the shell.

# Failure path: a backgrounded op that fails surfaces in the wait.
jailbee new feat/waitfail nonexistent-base --background
jailbee tmux feat-waitfail
# expect: "background creation of 'feat-waitfail' failed: ..." and exit 1.
# When the container is up (autostart failed), the error is followed by a
# hint naming the command you ran, e.g.:
#   "  Inspect it anyway with:  jailbee tmux feat-waitfail --force"

# --force escape hatch: after an autostart step fails, the container is left
# created and running, so --force attaches anyway to inspect it. (Only the
# `failed` job row was blocking the plain attach.) Works on shell/tmux/ide/chrome.
jailbee new feat/waitforce --background   # let it fail in an autostart step
jailbee tmux feat-waitforce --force
# expect: "⚠ 'feat-waitforce': background creation failed (...) — attaching anyway."
#         then the tmux session with the failed window.
# (The job row stays `failed` until you acknowledge it:
#    jailbee job clear feat-waitforce
#  — the container is left alone. `jailbee job ls` shows the recorded error and
#  the worker log path first; `jailbee job log feat-waitforce` prints the log.)

# --force does not bypass the container's existence: when the create failed
# before `incus init` there is nothing to attach to.
jailbee new feat/nosuchbase nonexistent-base --background   # fails before creating
jailbee tmux feat-nosuchbase --force
# expect: "✗ 'feat-nosuchbase': no such container — there is nothing to attach
#          to.", the creation error that explains it, and the
#          `jailbee job clear feat-nosuchbase` hint. Exit 1, no traceback.

jailbee destroy --all --force
```

## `jailbee new` `.local` share smoke test

```bash
mkdir -p .local
echo 'echo hello from container' > .local/run-me.sh
jailbee new feat/localsmoke --no-autostart
jailbee shell feat-localsmoke
# inside container:
cat ~/SampleApp/.local/run-me.sh        # the host file is visible
echo 'made in container' > ~/SampleApp/.local/back.txt
git -C ~/SampleApp status --porcelain    # .local/ does NOT appear (excluded)
exit
cat .local/back.txt                      # written from the container, on host
jailbee destroy feat-localsmoke --force

# Disabled + mount-mode are inert:
jailbee new feat/localoff --no-autostart   # after setting share_local: false
jailbee shell feat-localoff
ls ~/SampleApp/.local 2>&1 | grep -i "no such" || echo "(present)"
exit
jailbee destroy feat-localoff --force
rm -rf .local
```

## `jailbee net refresh / status / unregister` smoke test

```bash
# First time: jailbee init installs the timer and registers the repo.
# `make install` also runs `jailbee net install`, which re-installs the
# timer without touching profiles/networks — safe to re-run any time.
jailbee init
systemctl --user is-active jailbee-net-refresh.timer    # → active
jailbee net status                                       # shows this repo

# Trigger a manual refresh
jailbee net refresh
jailbee net refresh --json | jq .                       # machine-readable

# Wait 60s and confirm the timer fires automatically
journalctl --user -u jailbee-net-refresh.service -n 5 --no-pager

# Inspect the pool growth over a few cycles (github.com should accrue IPs)
jailbee net status | grep -A1 "github.com"
sleep 70
jailbee net status | grep -A1 "github.com"              # may show more IPs

# Unregister and verify removal
jailbee net unregister
jailbee net status                                       # this repo gone

# Re-register via apply
jailbee apply --no-restart
jailbee net status                                       # back
```

### Offline-mode removal migration

Requires a container created by a pre-removal `jailbee`.

1. On the old version: `jailbee net offline feat-x` and confirm
   `jailbee ls` shows `offline`.
2. Upgrade JailBee, then run `jailbee apply`.
3. Expect a `Migrating myrepo-feat-x off myrepo-net-offline → myrepo-net-strict`
   line, followed by `Deleted stale profile myrepo-net-offline`.
4. `jailbee ls` shows `strict`; `incus profile list | grep net-offline` is empty.
5. `jailbee net loose feat-x` and `jailbee net strict feat-x` both succeed — proving
   the container has a recognised net profile again.

## `jailbee git push` smoke test

> **Host-only:** this recipe cannot be exercised from inside a `jailbee shell`
> session — `jailbee git push` operates between the host repo and a container,
> so a nested `jailbee` does not have a host repo to push from.

> **`--from-local` throughout:** these steps build host commits that only
> exist locally (never pushed to origin). The default `push.push_from:
> origin` would fetch and push `origin/main` instead, so the local host
> commit would not travel. `--from-local` is what makes each step
> demonstrate what it claims. See the recipe below for the origin path.

```bash
git checkout main
jailbee new feat/pushsmoke
jailbee shell feat-pushsmoke
# inside container:
cd ~/SampleApp
echo "container-side" > c.txt && git add . && git commit -m "container commit"
exit

# Host: advance main
echo "host-side" > host.txt && git add . && git commit -m "host commit"

# 1. Transport only (default_source=base → pushes 'main' into the container).
jailbee git push feat-pushsmoke --from-local
jailbee shell feat-pushsmoke
git -C ~/SampleApp log refs/jailbee/host/main -1   # host commit visible
exit

# 2. Merge
jailbee git push feat-pushsmoke --merge --from-local
jailbee shell feat-pushsmoke
cd ~/SampleApp && git log --oneline | head -3   # merge commit + both parents
exit

# 3. Rebase (fresh container so history is linear)
jailbee destroy feat-pushsmoke --force
jailbee new feat/rebasesmoke
jailbee shell feat-rebasesmoke
cd ~/SampleApp && echo "x" > x.txt && git add . && git commit -m "feature commit" && exit

jailbee git push feat-rebasesmoke --rebase --from-local
jailbee shell feat-rebasesmoke
cd ~/SampleApp && git log --oneline
exit

# 4. Conflict
jailbee destroy feat-rebasesmoke --force
jailbee new feat/conflict
jailbee shell feat-conflict
cd ~/SampleApp && echo "CONFLICTING" > host.txt && git add . && git commit -m "conflict setup" && exit
jailbee git push feat-conflict --merge --from-local
# expect: conflict message, hint pointing to 'jailbee shell feat-conflict'
jailbee shell feat-conflict
cd ~/SampleApp && git status | grep -i "unmerged"   # merge state present
git merge --abort && exit

# 5. Dirty tree refused
jailbee shell feat-conflict
cd ~/SampleApp && echo "dirty" >> README.md && exit
jailbee git push feat-conflict --merge --from-local
# expect: "Container working tree is dirty. ..."

# 6. Mount-mode refused
jailbee new mount-push --mount
jailbee git push mount-push 2>&1 | grep "mount mode"
jailbee destroy mount-push --force
jailbee destroy feat-conflict --force
```

## `jailbee git push` source-ref smoke test

> **Host-only.** Verifies that a push carries the *fetched* upstream tip,
> not a host branch that only moves on `git pull` — and that local-only
> commits are reported rather than silently skipped. Needs a real origin
> whose default branch has moved since the host last pulled.

```bash
# Setup: local main deliberately behind origin/main.
git checkout main
git fetch origin
git rev-parse main origin/main    # expect: different SHAs (main behind)

jailbee new feat/refsmoke
git rev-list --count main..origin/main   # N commits the local branch lacks

# 1. Default: fetches + pushes origin/main.
jailbee git push feat-refsmoke --plain
# expect: "Pushed 'main' (refs/remotes/origin/main) ... as refs/jailbee/host/main"
jailbee shell feat-refsmoke
git -C ~/SampleApp rev-parse refs/jailbee/host/main   # == host's origin/main
exit

# 2. --from-local sends the (older) local branch instead.
jailbee git push feat-refsmoke --plain --from-local
# expect: "(refs/heads/main)" in the summary, no fetch line

# 3. Local-only commits are reported, not dropped.
echo "unpushed" > u.txt && git add . && git commit -m "host-only commit"
jailbee git push feat-refsmoke --plain
# expect: pushed refs/remotes/origin/main + a warning naming the
#         host-only commit count and suggesting --from-local
git reset --hard HEAD~1

# 4. Base anchor follows origin (jailbee ls AHEAD stays honest).
jailbee ls   # AHEAD counted against the freshly-pushed base
# NOTE: step 2 pushed the older local main over refs/jailbee/base/main, so
# re-run step 1 before trusting these numbers.

# 5. An unfetchable source degrades to the local ref, not a failure.
git branch local-only-branch
jailbee git push feat-refsmoke --plain --from local-only-branch
# expect: "(refs/heads/local-only-branch)" pushed, and NO fetch warning —
#         the failed fetch did not affect what travelled
git branch -d local-only-branch
jailbee git push feat-refsmoke --plain --from nowhere-at-all 2>&1 | tail -2
# expect: "does not exist on host" (neither refs/heads nor origin)

# 6. Offline host: the origin ref travels but is flagged as possibly stale.
#    (Disconnect the network, then:)
jailbee git push feat-refsmoke --plain
# expect: origin/main pushed + a warning quoting git's fetch error

jailbee destroy feat-refsmoke --force
```

## `jailbee git push --force` smoke test

> **Host-only.** `--force` hard-resets the container's current branch +
> working tree to the pushed ref, discarding container-only commits. It
> is single-container only, refuses a dirty tree / divergent branch, and
> is flag-only (never a configured `push.default_action`). Discarded
> commits remain in the container reflog until gc.

```bash
git checkout main
jailbee new feat/forcesmoke
jailbee shell feat-forcesmoke
# inside container: add a commit that --force will discard
cd ~/SampleApp
echo "doomed" > doomed.txt && git add . && git commit -m "container-only commit"
exit

# Host: advance main so the reset target differs from the container tip.
echo "host-side" > host.txt && git add . && git commit -m "host commit"

# 1. Force-reset: container's 'main' is replaced by host 'main'.
jailbee git push feat-forcesmoke --from main --force
# expect: push summary, "Reset 'main' to 'main' inside container 'feat-forcesmoke'."
# expect: "Container HEAD now at <oid>."
# expect: "⚠ discarded 1 container-only commit(s) (was <oid>)."
jailbee shell feat-forcesmoke
cd ~/SampleApp && git log --oneline | head -2   # host commit on top, doomed.txt gone
ls doomed.txt 2>&1 | grep -i "no such"          # working tree matches host
git reflog | grep "container-only commit"        # discarded commit recoverable
exit

# 2. Idempotent re-run: already in sync -> no discard warning.
jailbee git push feat-forcesmoke --from main --force
# expect: "Reset 'main' to 'main' ..." and "HEAD now at ..." with NO discard line.

# 3. Divergent-branch refusal: container on a different branch than source.
jailbee shell feat-forcesmoke
cd ~/SampleApp && git checkout -b feat/elsewhere && exit
jailbee git push feat-forcesmoke --from main --force 2>&1 | grep "only replaces the same"

# 4. No-name refusal (flag-only, not in the picker).
jailbee git push --force 2>&1 | grep "explicit container name"

# 5. Mutually exclusive with --merge/--rebase/--plain.
jailbee git push feat-forcesmoke --force --merge 2>&1 | grep "mutually exclusive"

# 6. Dirty tree refused.
jailbee shell feat-forcesmoke
cd ~/SampleApp && git checkout main && echo dirty >> README.md && exit
jailbee git push feat-forcesmoke --from main --force 2>&1 | grep -i "dirty"

# 7. Mount-mode refused.
jailbee new mount-force --mount
jailbee git push mount-force --force 2>&1 | grep "mount mode"
jailbee destroy mount-force --force

# 8. Submodules: a --force that moves a submodule pointer leaves the
#    container's submodule working tree matching the pushed ref (not stale).
# (Advance a submodule pointer on host main, commit, then:)
jailbee git push feat-forcesmoke --from main --force
jailbee shell feat-forcesmoke
cd ~/SampleApp && git submodule status --recursive   # at host's submodule commit, clean
exit

jailbee destroy feat-forcesmoke --force
git reset --hard HEAD~1   # drop the local "host commit"
```

## `jailbee git push` interactive picker + config defaults smoke test

> Host-only. Requires a TTY (won't work from inside `jailbee shell`).

`push.default_source` now defaults to **`base`** — the container's base branch
(e.g. `main`) is pushed into the container without prompting. Set it to `ask`
to get the interactive source picker; when `ask` is active and a single
non-PR container is targeted, the picker offers the container base branch as
the first/default choice. `--from`/`--current`/`--pr` always override.

```bash
git checkout main
jailbee new feat/picker-a
jailbee new feat/picker-b

# 1. Out-of-the-box: default_source=base, default_action=ask.
#    With no args: container picker → action picker only (source = base, no prompt).
jailbee git push
# pick: feat-picker-a → action picker: "merge"
# expect: "Merged 'main' (base branch) into feat-picker-a ..."

# 2. --current overrides the source.
jailbee git checkout -b feat/host-current
jailbee git push feat-picker-a --current --merge
# expect: no prompts; merge runs with feat/host-current as source

# 3. default_source=ask: picker shows base branch first for a single container.
cat >> ~/.config/jailbee/global.yaml <<'YAML'
push:
  default_action: merge
  default_source: ask
YAML
jailbee git push feat-picker-b
# expect: source picker — first choice is "main (base branch)", then current, etc.
# pick: main → "Merged 'main' into feat-picker-b ..."

# 4. Per-repo override takes precedence.
cat >> .jailbee/config.yaml <<'YAML'
push:
  default_action: rebase
  default_source: base
YAML
jailbee git push feat-picker-b
# expect: no prompts; "Rebased 'feat-picker-b' onto 'main' ..."

# 5. Cleanup.
jailbee destroy feat-picker-a --force
jailbee destroy feat-picker-b --force
git checkout main && git branch -D feat/host-current
# Edit ~/.config/jailbee/global.yaml and .jailbee/config.yaml to remove the push: blocks.
```

## `jailbee git push` multi-select smoke test

> Host-only. Multi-select picker requires a TTY.

```bash
git checkout main
jailbee new feat/push-multi-a
jailbee new feat/push-multi-b
jailbee new feat/push-multi-c
# Make c's tree dirty so its push fails
jailbee shell feat-push-multi-c
cd ~/SampleApp && echo dirty >> README.md && exit

jailbee git push
# expect: checkbox picker — space-toggle a, b, c, Enter
# expect: one source prompt (e.g. main), one action prompt (merge)
# expect: per-container ✓ for a, b; ✗ for c (dirty)
# expect: Summary block at the end with "2 succeeded, 1 failed"

jailbee destroy --all --force
```

## `jailbee git push --pr` smoke test

> Host-only. Refreshes a PR container with commits the PR author pushed
> after the container was created.

```bash
# Create a container from an open PR
gh pr list -L 1 --json number,headRefName
jailbee new --pr <N>
DERIVED=<derived-container-name>

# (Simulate upstream movement: have a new commit land on the PR on GitHub.)

# Refresh + push the PR head into the container, fast-forwarding its branch
jailbee git push $DERIVED --pr
# expect: "PR #<N> '<head_ref>' refreshed (<old>..<new>)." then the push summary

# Interactive: source picker offers the PR head first/default
jailbee git push $DERIVED          # with push.default_source=ask
# expect: first choice "<head_ref> (PR #<N> head — refresh from GitHub)"

# No-arg flow with a single PR container also offers it
jailbee git push
# pick the PR container → source menu shows the PR head first

# Negative: --pr on a non-PR container is refused
jailbee new feat/notapr --no-clone --no-autostart
jailbee git push feat-notapr --pr 2>&1 | grep "not created from a PR"
jailbee destroy feat-notapr --force
jailbee destroy $DERIVED --force
```

## `jailbee pr` smoke test

> Host-only. Requires `gh auth login` on the host and push access to origin.
> SSH pushes may need a YubiKey touch. `jailbee pr` creates a draft PR when the
> container has none yet; when one already exists (the container is its
> author), re-running pushes new commits and updates the head, optionally
> touching the description/state via `--description`/`--title`/`--body`/
> `--ready`/`--draft`.

```bash
git checkout main
jailbee new feat/prsmoke
jailbee shell feat-prsmoke
# inside container:
cd ~/SampleApp
echo "pr smoke" > pr.txt && git add . && git commit -m "feat: pr smoke"
exit

jailbee pr feat-prsmoke
# expect: fetch summary, git push output, then
#         "Draft PR #<N> created for 'feat/prsmoke': https://github.com/..."
gh pr view <N>                       # draft, base main, title "feat: pr smoke"
incus config get <prefix>-feat-prsmoke user.jailbee.pr         # → "<N>"
incus config get <prefix>-feat-prsmoke user.jailbee.pr_author  # → "1"

# Idempotent re-run: new commits are pushed and the PR head moves; the
# description is left untouched by default.
jailbee shell feat-prsmoke
cd ~/SampleApp && echo more > more.txt && git add . && git commit -m "more" && exit
jailbee pr feat-prsmoke
# expect: "PR #<N> updated — head moved; description unchanged. https://github.com/..."

# Since the container now counts as a PR container, the refresh path works:
jailbee git push feat-prsmoke --pr

# --description: regenerate + apply the PR body with Claude (requires claude.enabled).
jailbee pr feat-prsmoke --description
# expect: spinner "Regenerating PR description with Claude…", then
#         "PR #<N> updated — head moved, description refreshed. https://github.com/..."
gh pr view <N>                       # body reflects the diff, applied via `gh pr edit`

# --ready / --draft: toggle PR state independently of pushing commits.
jailbee pr feat-prsmoke --ready
# expect: "... updated — head moved; description unchanged. (marked ready) https://github.com/..."
gh pr view <N> --json isDraft        # false
jailbee pr feat-prsmoke --draft
# expect: "... (marked draft) https://github.com/..."
gh pr view <N> --json isDraft        # true

# AI-generated title/body on CREATE (requires claude.enabled):
jailbee new feat/aismoke
jailbee shell feat-aismoke
cd ~/SampleApp && echo ai > ai.txt && git add . && git commit -m "wip" && exit
jailbee pr feat-aismoke
# expect: spinner "Generating PR title/description with Claude in 'feat-aismoke'…"
#         then a Claude-written title/body on the PR (not the placeholder).
gh pr view <N>                      # title/body reflect the diff, not "wip"

# Fallback: if Claude errors/times out, expect the warning
#   "Claude PR-text generation failed; using a placeholder. Edit the PR
#    later with `jailbee pr --description`."
# and the PR still gets created with placeholder text.
jailbee destroy feat-aismoke --force

# Opt out per-invocation on create (fresh container, no existing PR):
jailbee new feat/noaismoke
jailbee shell feat-noaismoke
cd ~/SampleApp && echo x > x.txt && git add . && git commit -m "wip no-ai" && exit
jailbee pr feat-noaismoke --no-ai  # uses commit subject + placeholder
jailbee destroy feat-noaismoke --force
# Opt out permanently: set claude.ai_pr_description: false in config.

# Adoption: a `jailbee new --pr` (review) container no longer refuses `jailbee pr`
# outright — it asks whether to push the container's commits to that PR's
# head, then remembers the answer. Reuse PR #<N> from feat-prsmoke above: the
# point is a container jailbee did NOT open the PR from (no user.jailbee.pr_author),
# on a PR you do have push access to.
#
# Destroy feat-prsmoke FIRST: `jailbee new --pr` derives the container name from
# the PR head ref ('feat/prsmoke' → 'feat-prsmoke'), so it would collide.
# PR #<N> and the origin branch stay; only the container goes.
jailbee destroy feat-prsmoke --force
jailbee new --pr <N>                     # → container 'feat-prsmoke' again
jailbee shell feat-prsmoke
cd ~/SampleApp && echo adopt > adopt.txt && git add . && git commit -m "adopt" && exit
incus config get <prefix>-feat-prsmoke user.jailbee.pr_author   # → "" (not jailbee's own PR)

# No TTY and no --yes: errors out and names --yes — it does NOT push silently.
echo "" | jailbee pr feat-prsmoke 2>&1 | grep -- "--yes"

# Interactive, DECLINED: answer "n" at the prompt. Nothing is pushed and
# nothing is recorded, so the next run asks again.
jailbee pr feat-prsmoke
# expect: "Container 'feat-prsmoke' was created from PR #<N> by @<author>
#          (OPEN); head '<head>' → base '<base>'."
#         "Push this container's commits to PR #<N>'s head '<head>'? [y/N]" → n
#         then "Aborted." and NO push output.
incus config get <prefix>-feat-prsmoke user.jailbee.pr_adopted  # → "" (still unset)

# Interactive, ACCEPTED: answer "y". The same summary line, then the push.
jailbee pr feat-prsmoke
# expect: the summary line, prompt → y, fetch summary, git push output,
#         "PR #<N> updated — head moved; description unchanged. https://..."
incus config get <prefix>-feat-prsmoke user.jailbee.pr_adopted  # → "1"
incus config get <prefix>-feat-prsmoke user.jailbee.pr_branch   # → PR #<N>'s head branch

# `--yes` skips the prompt (this is the scripted path) — and still prints the
# "was created from PR #<N> by @<author>" line BEFORE pushing.
jailbee shell feat-prsmoke
cd ~/SampleApp && echo again > again.txt && git add . && git commit -m "again" && exit
jailbee pr feat-prsmoke --yes
# expect: the summary line, then "PR #<N> updated — head moved; ...", no prompt.

# Re-run without --yes: adoption is recorded, so no adoption prompt. The PR is
# still not jailbee's own, so it also never offers to rewrite the author's
# description — even on a TTY with claude.enabled.
jailbee shell feat-prsmoke
cd ~/SampleApp && echo third > third.txt && git add . && git commit -m "third" && exit
jailbee pr feat-prsmoke
# expect: "PR #<N> updated — head moved; description unchanged. https://..."
#         NO "Push this container's commits…?" and NO "Update the PR
#         description with Claude?" prompt.
# An explicit request still works:
jailbee pr feat-prsmoke --title "adopted title"   # expect: "title updated"

# `--as` is refused on any container that already has a PR (exit 2) — the PR's
# head is fixed, so a different name would leave the PR untouched.
jailbee pr feat-prsmoke --as some/other-name 2>&1 | grep -- "--as cannot be combined"

# `--force` on a PR jailbee did not create asks a second time, naming the head.
jailbee shell feat-prsmoke
cd ~/SampleApp && git commit --amend -m "third (amended)" && exit
echo "" | jailbee pr feat-prsmoke --force 2>&1 | grep -- "--yes"   # no TTY → error
jailbee pr feat-prsmoke --force
# expect: "--force will overwrite PR #<N>'s head '<head>' …" then
#         "Force-push over PR #<N>'s head '<head>'? [y/N]" → y
#         "PR #<N> updated — head force-pushed (--force-with-lease) …"

jailbee destroy feat-prsmoke --force

# Cleanup (closes nothing on GitHub — close/delete the smoke PR manually).
git push origin --delete feat/prsmoke   # requires explicit user approval

# AI-generated head branch name (requires claude.enabled + claude.ai_pr_branch):
jailbee new dev-7           # generic container/branch name
jailbee shell dev-7
cd ~/SampleApp && echo x > x.txt && git add . && git commit -m "wip" && exit
jailbee pr dev-7
# expect (TTY): a proposed head name (e.g. "user/...") you can accept/edit,
#         then "Draft PR #N created for 'user/...'".
# If a local 'dev-7' branch exists (from jailbee git checkout), expect:
#   "Renamed local branch 'dev-7' → 'user/...' to match the PR head."
incus config get <prefix>-dev-7 user.jailbee.pr_branch   # → the chosen name
gh pr view <N> --json headRefName                     # → the chosen name

# Re-run reuses the stored name (no new AI, no new branch):
jailbee shell dev-7 && cd ~/SampleApp && echo y > y.txt && git add . && git commit -m more && exit
jailbee pr dev-7
# expect: "PR #N updated — head moved; ...", head still 'user/...'.

# …and --as is refused on the re-run too (the PR head is fixed), even though
# jailbee itself opened this PR:
jailbee pr dev-7 --as user/other 2>&1 | grep -- "--as cannot be combined"   # exit 2

# --as overrides the name; --no-ai keeps the container branch:
jailbee new dev-8 && jailbee pr dev-8 --as user/manual-name
jailbee destroy dev-8 --force

# --force: rebase in the container, then update the PR head safely.
jailbee shell dev-7 && cd ~/SampleApp && git rebase -i HEAD~2 && exit  # rewrite history
jailbee pr dev-7          # expect: non-ff rejection + hint pointing at --force
jailbee pr dev-7 --force  # expect: "head force-pushed (--force-with-lease)"
jailbee pr --force 2>&1 | grep "explicit container name"   # picker refused

jailbee destroy dev-7 --force
```

## Remote-git retry smoke test

Covers `retry.with_remote_retry` at all three call sites. Each needs a
*recoverable* remote failure, so the trick is to break the remote reachably and
then fix it while the prompt waits.

### `jailbee pr` — a failed origin push

1. In a container with commits to publish, break the host's push credentials
   without breaking the container fetch. With an SSH origin, the simplest way is
   to point the agent at nothing:
   `SSH_AUTH_SOCK=/nonexistent jailbee pr <name>`
2. Expect the container fetch to succeed and print its refs, then git's own
   authentication error, then:
   `Retry pushing '<branch>' to origin? [y/N]:`
3. **Do not answer yet.** Confirm the prompt appears *before* any `--force` /
   `--as` advice — that hint must only show after a decline.
4. Answer `n`. Expect the full `SyncError` with the hint, exit 1, and no
   `gh pr create`. Nothing should mention any specific authentication device.
5. Re-run and answer `y` at the prompt after restoring credentials in another
   shell (`ssh-add -l` should list a key). Expect the push to succeed on the
   second attempt and the PR to be created with the description generated
   *before* the first failure — no second Claude run, so the whole command
   should finish within seconds of the retry.
6. Verify the container fetch did **not** run twice: step 5's output should show
   the "Fetched N commits" summary once.

### `jailbee new --pr` — a failed PR-head fetch

1. `jailbee new --pr <N>` with the host offline (or `git remote set-url origin` to a
   bogus host, then restore it).
2. Expect `✗ Fetching PR #<N>'s head failed: ...` followed by
   `Retry fetching PR #<N>'s head? [y/N]:`
3. Restore the remote, answer `y`, and expect creation to continue normally.
4. Separately, create a local branch of the head's name that diverges from the
   PR head and re-run. Expect success: the fetch targets
   `refs/jailbee/pr/<N>/head`, so a diverging (or checked-out) branch of that name
   is irrelevant, and the container is built from the PR head regardless.

### `jailbee new` — a failed origin-mode autofetch

1. With `new.autofetch: true` (the default) and a branch that exists only on
   origin, run `jailbee new <branch>` while offline.
2. Expect `✗ Fetching origin/<branch> failed: ...` then
   `Retry fetching origin/<branch>? [y/N]:`
3. Answer `n`: expect the existing "autofetch of 'origin/<branch>' failed"
   error and **no container created** (`incus list` unchanged).
4. Re-run, restore the network, answer `y`: expect creation to proceed.

### Non-interactive paths must never prompt

1. `JAILBEE_NONINTERACTIVE=1 SSH_AUTH_SOCK=/nonexistent jailbee pr <name>` — expect the
   old behaviour: failure, hint, exit 1, no prompt.
2. `jailbee new --background <branch>` with a broken origin — expect the job to fail
   and be recorded by `jailbee job ls`, not to hang waiting on stdin.

## `jailbee git` top-level alias smoke test

> Host-only. Every `jailbee git <sub>` command has a top-level alias
> (`jailbee fetch`/`checkout`/`pull`/`retarget`/`diff`/`push`).
> The aliases are **hidden** from `jailbee --help` — the top-level list stays
> short — but stay invocable, and the canonical forms are listed under
> `jailbee git --help`. `jailbee pr` is the mirror-image exception: it is the
> visible, canonical top-level command, and `jailbee git pr` is its hidden
> alias — so it does NOT show up under `jailbee git --help`. Also verifies
> `jailbee git merge` and `jailbee git create-pr` no longer exist.

```bash
# Aliases are HIDDEN from the top-level help (no "Alias for" rows).
uv run jailbee --help | grep -c "Alias for"          # -> 0

# Canonical subcommands are listed under the git group.
uv run jailbee git --help | grep -E "fetch|checkout|pull|retarget|diff|push"

# `pr` is the exception: visible at the top level, hidden under `jailbee git`.
uv run jailbee --help | grep " pr "        # expect: listed as a normal top-level command
uv run jailbee git --help | grep " pr "    # expect: no match — hidden alias
uv run jailbee git pr --help | grep "jailbee pr --help"   # "See `jailbee pr --help`."

# Each hidden alias is still reachable; its --help points at the canonical form.
uv run jailbee checkout --help | grep "jailbee git checkout"   # "Alias for `jailbee git checkout`."
uv run jailbee fetch --help    | grep "jailbee git fetch"
uv run jailbee retarget --help | grep "jailbee git retarget"
uv run jailbee diff --help     | grep "jailbee git diff"
# (The full docstring with Examples lives on the canonical form:)
uv run jailbee git pull --help | grep "Examples:"

# The old commands are gone.
uv run jailbee git merge 2>&1 | grep -i "no such command"
uv run jailbee git create-pr 2>&1 | grep -i "no such command"

# Functional smoke: alias and canonical produce identical behaviour.
git checkout main
jailbee new feat/alias-smoke
jailbee shell feat-alias-smoke
cd ~/SampleApp && echo "alias" > a.txt && git add . && git commit -m "alias smoke" && exit

jailbee pull feat-alias-smoke --ff   # `jailbee pull` == `jailbee git pull`
jailbee diff feat-alias-smoke --stat # `jailbee diff` == `jailbee git diff`
jailbee destroy feat-alias-smoke --force
```

## `jailbee submodule` support smoke test

> Host-only. Requires the host repo to have at least one initialized
> submodule (`git submodule update --init --recursive` already run on host).

```bash
# 1. jailbee new brings submodules along (offline), each on the CONTAINER branch.
jailbee new feat/submod --no-autostart
jailbee shell feat-submod
cd ~/SampleApp
git submodule status --recursive   # all submodules present + checked out
git -C <submodule-path> branch --show-current   # -> feat/submod (the container branch)
# Diff a submodule against its default branch / base anchor:
git -C <submodule-path> diff <default>                 # e.g. master/main
git -C <submodule-path> diff refs/jailbee/base/<base>       # jailbee's fetch-proof anchor
exit

# 2. Bump a submodule pointer in the container, then pull.
jailbee shell feat-submod
cd ~/SampleApp/<submodule-path>
git fetch && git checkout <some-other-commit>
cd ~/SampleApp && git add <submodule-path> && git commit -m "bump submodule"
exit
git checkout main
jailbee pull feat-submod
git submodule status --recursive   # host submodule now at the new commit
# After jailbee checkout/pull on the host, submodules are on the superproject branch too:
git -C <submodule-path> branch --show-current   # -> <host branch>, not detached

# 3. Author a NEW submodule commit in the container, then pull (round-trip).
jailbee new feat/submod2 --no-autostart
jailbee shell feat-submod2
cd ~/SampleApp/<submodule-path>
echo x > new.txt && git add new.txt && git commit -m "new submodule commit"
cd ~/SampleApp && git add <submodule-path> && git commit -m "point at new submodule commit"
exit
git checkout main
jailbee pull feat-submod2          # transports the new submodule objects to host
git submodule update --init --recursive   # succeeds offline (objects present)

# 4. Reverse direction via push.
git checkout main
# (advance a submodule pointer on host, commit)
jailbee new feat/submod3
jailbee git push feat-submod3 --merge
jailbee shell feat-submod3
cd ~/SampleApp && git submodule status --recursive   # container updated
exit

# 5. Hard-fail path: host submodule not initialized.
git submodule deinit -f <submodule-path>
jailbee new feat/submodfail --no-autostart 2>&1 | grep "not initialized"
git submodule update --init <submodule-path>   # restore

# 6. Opt out.
printf 'new:\n  submodules: false\n' >> .jailbee/config.yaml
jailbee new feat/nosubmod --no-autostart
jailbee shell feat-nosubmod
ls ~/SampleApp/<submodule-path>   # empty (not initialized)
exit
jailbee destroy --all --force
# remove the new: block from .jailbee/config.yaml afterwards

# 7. Gitlink conflict auto-resolution: both sides move the same submodule.
git checkout main
jailbee new feat/submod-conflict
jailbee shell feat-submod-conflict
cd ~/SampleApp/<submodule-path>
echo container > c.txt && git add c.txt && git commit -m "container submodule commit"
cd ~/SampleApp && git add <submodule-path> && git commit -m "container: bump submodule"
exit
# advance the SAME submodule on the host to a different commit
cd <submodule-path> && echo host > h.txt && git add h.txt && git commit -m "host submodule commit"
cd .. && git add <submodule-path> && git commit -m "host: bump submodule"
jailbee pull feat-submod-conflict
# expect: submodule auto-merged, superproject merge committed automatically,
#         "auto-merged (1):  ✓ <submodule-path>" — no manual gitlink fix needed
git submodule status --recursive   # submodule on a real merge commit
git -C <submodule-path> branch --show-current   # -> main (the superproject branch), not detached

# If a submodule has a genuine CONTENT conflict (same file edited both sides),
# expect a non-zero exit, that submodule under "in merge state — resolve these"
# with git's CONFLICT lines, and the superproject left in merge state.
jailbee destroy feat-submod-conflict --force

# 7b. NESTED gitlink conflict (requires a submodule that has its own submodule).
# Both sides move the inner submodule, so BOTH gitlink levels conflict.
git checkout main
jailbee new feat/nested-conflict
jailbee shell feat-nested-conflict
cd ~/SampleApp/<submodule-path>/<inner-path>
echo ctr > c.txt && git add c.txt && git commit -m "container inner commit"
cd .. && git add <inner-path> && git commit -m "container: bump inner"
cd ~/SampleApp && git add <submodule-path> && git commit -m "container: bump submodule"
exit
cd <submodule-path>/<inner-path> && echo hst > h.txt && git add h.txt && git commit -m "host inner commit"
cd .. && git add <inner-path> && git commit -m "host: bump inner"
cd .. && git add <submodule-path> && git commit -m "host: bump submodule"
jailbee pull feat-nested-conflict
# expect BOTH levels resolved in one pass and the merge committed:
#   auto-merged (2):
#     ✓ <submodule-path>/<inner-path>
#     ✓ <submodule-path>
git -C <submodule-path> log -1 --format=%p          # two parents (a real merge)
git -C <submodule-path>/<inner-path> log -1 --format=%p   # two parents as well
jailbee destroy feat-nested-conflict --force

# 8. jailbee ls / jailbee diff reflect submodule changes.
jailbee new feat/submod-vis --no-autostart
jailbee shell feat-submod-vis
cd ~/SampleApp/<submodule-path>
printf 'a\nb\nc\n' >> some_tracked_file && git add . && git commit -m "sub edit"
cd ~/SampleApp && git add <submodule-path> && git commit -m "bump submodule"
exit
jailbee ls            # feat-submod-vis AHEAD ± reflects the +3 from inside the submodule
jailbee diff feat-submod-vis   # shows the submodule's file hunk inline
# Working-tree (uncommitted) submodule changes show in the WT column too:
jailbee shell feat-submod-vis
cd ~/SampleApp/<submodule-path> && printf 'd\ne\n' >> some_tracked_file && exit
jailbee ls            # feat-submod-vis WT now reflects the +2 uncommitted submodule lines
jailbee destroy feat-submod-vis --force
```

## Per-submodule change reporting smoke test

> Host-only. Requires the host repo to have at least one initialized
> submodule. Tests the three new surfaces: `jailbee ls` sub-rows,
> `jailbee diff --stat` grouping, and the `jailbee pull` submodule report block.

```bash
# Setup: a container with one committed submodule bump (and one uncommitted).
git checkout main
jailbee new feat/submod-report --no-autostart
jailbee shell feat-submod-report
cd ~/SampleApp/<submodule-path>
echo x > x.txt && git add x.txt && git commit -m "sub-commit A"
echo y > y.txt && git add y.txt && git commit -m "sub-commit B"
cd ~/SampleApp && git add <submodule-path> && git commit -m "bump submodule (2 commits)"
# Also leave an uncommitted change in the submodule working tree:
cd ~/SampleApp/<submodule-path> && printf 'line1\nline2\n' >> x.txt
exit

# 1. jailbee ls sub-rows (default: shown when .gitmodules exists).
jailbee ls
# expect: feat-submod-report row followed by:
#   └ <submodule-path>   +N -N (ahead)   +2 -0 (wt)
# Clean submodules never appear as sub-rows.

# --no-submodules collapses to aggregate-only (no sub-rows).
jailbee ls --no-submodules
# expect: feat-submod-report row only, no └ sub-rows

# 2. jailbee diff --stat: grouped per superproject / submodule.
jailbee diff feat-submod-report --stat
# expect sections like:
#   === superproject ===
#   <superproject files>
#   === <submodule-path> ===
#   x.txt  | 2 ++
#   y.txt  | 1 +
#   2 files changed, 3 insertions(+)

# Plain jailbee diff (no --stat) is unchanged — still inlines via --submodule=diff.
jailbee diff feat-submod-report
# expect: inline submodule diff hunks (no === sections)

# 3. jailbee pull: Submodules report block at the END.

# Success path (pointer move).
git checkout main
jailbee pull feat-submod-report --into main --no-cleanup
# expect: git's own merge output, then at the end:
#   ── Submodules ──────────────────────────────────────────────
#   <submodule-path>   <old>..<new> (2 commits, +N -N)

# Conflict + auto-merge path.
jailbee new feat/submod-conflict2 --no-autostart
jailbee shell feat-submod-conflict2
cd ~/SampleApp/<submodule-path>
echo ctr > c.txt && git add c.txt && git commit -m "container sub-commit"
cd ~/SampleApp && git add <submodule-path> && git commit -m "container: bump submodule"
exit
# Advance the SAME submodule on the host to a different commit.
cd <submodule-path> && echo hst > h.txt && git add h.txt && git commit -m "host sub-commit"
cd .. && git add <submodule-path> && git commit -m "host: bump submodule"
git checkout main
jailbee pull feat-submod-conflict2 --into main --no-cleanup
# expect end of output:
#   ── Submodules ──────────────────────────────────────────────
#   auto-merged (1):
#     ✓ <submodule-path>

# Unresolvable content conflict path: if a submodule has the same file
# edited on both sides, jailbee pull exits non-zero and groups the outcome:
#   ── Submodules ──────────────────────────────────────────────
#   in merge state — resolve these (1):
#     ✗ <submodule-path>  file conflicts
#         CONFLICT (content): Merge conflict in <file>
#   superproject left in merge state
# A submodule left dirty instead lands under "skipped, not touched" — it was
# never touched, so it needs a stash/commit and a re-run, not a merge commit.

# Cleanup.
jailbee destroy feat-submod-report --force
jailbee destroy feat-submod-conflict2 --force
git reset --hard HEAD~1   # drop the local "host: bump submodule" commit
```

## `jailbee submodule checkout` smoke test

> Host-only. Aligns submodules to a branch locally (no host<->container transport).

```bash
# Host repo: detach a submodule, then re-align it to the current branch.
git checkout -b feat/align-smoke
git -C <submodule-path> checkout --detach
git submodule status --recursive            # shows detached
jailbee submodule checkout
# expect: per-submodule "✓ feat/align-smoke" lines, then
#         "Submodules aligned to 'feat/align-smoke'."
git -C <submodule-path> branch --show-current   # -> feat/align-smoke

# Explicit branch override.
jailbee submodule checkout -b feat/other
git -C <submodule-path> branch --show-current   # -> feat/other

# Container target (aligns the container's submodules to its branch).
jailbee new feat/submod-align
jailbee submodule checkout feat-submod-align
# expect: "✓ feat/submod-align" per submodule
jailbee destroy feat-submod-align --force

# Detached-HEAD host without -b is refused.
git checkout --detach
jailbee submodule checkout 2>&1 | grep "detached HEAD"
git checkout main && git branch -D feat/align-smoke
```

## `jailbee new` clone source (`new.clone_from` / `new.autofetch`) smoke test

```bash
# Default: clone_from=origin, autofetch=true. New container is based on
# the *upstream* tip, not the host's possibly-stale local default branch.
git fetch origin            # ensure origin/main is up to date locally
git -C ~/SampleApp reset --hard HEAD~5   # simulate stale host main
jailbee new feat/freshmain --no-autostart
jailbee shell feat-freshmain
git -C ~/SampleApp log --oneline -1  # should match origin/main tip, not local HEAD~5
exit
jailbee destroy feat-freshmain --force

# Disable autofetch — clone uses whatever the host already has cached
# as origin/<default>. Useful offline or when ACLs block fetch.
cat >> .jailbee/config.yaml <<'YAML'
new:
  autofetch: false
YAML
jailbee new feat/noautofetch --no-autostart
jailbee destroy feat-noautofetch --force

# Switch to local mode — classic behaviour (host's local refs/heads).
sed -i 's/autofetch: false/clone_from: local/' .jailbee/config.yaml
jailbee new feat/localmode --no-autostart
jailbee destroy feat-localmode --force

# Restore defaults (delete the `new:` block from .jailbee/config.yaml)
```

## Branch-sourced autostart + privilege gate smoke test

> Host-only. The unit suite mocks every git read here, so this is the only
> place the real ref resolution, the baseline read and the background
> pre-flight run against an actual repo.

```bash
# 1. The ordinary case that must NOT ask anything: a checkout older than
#    origin/<default>. Commit a loose autostart step on the default branch,
#    push it, then rewind the checkout so it lags.
cat >> .jailbee/config.yaml <<'YAML'
autostart:
  on_start:
    - name: warmup
      run: echo warm
      network: loose
YAML
git commit -qam "add a loose warmup step" && git push
git reset --hard HEAD~1        # checkout now lags origin/<default>
jailbee new feat/lagging-checkout --background
# expect: it detaches immediately. The log (jailbee job log / the printed path)
# shows the `autostart config comes from …` diff — the container runs the
# branch's steps — and NO `widens privileges` block and no question, because
# the loose step is already in the reviewed baseline.
jailbee ls                         # JOB empties as it finishes; container comes up
jailbee destroy feat-lagging-checkout --force

# 2. A branch that widens beyond the baseline, from your own repo: warns,
#    proceeds, no question. (Push a branch whose step adds `network: loose`
#    where origin/<default> has none — e.g. revert step 1 on the default
#    branch first, keeping it on the feature branch.)
jailbee new feat/own-widening --background
# expect: `branch autostart widens privileges beyond refs/remotes/origin/<default>`
#         with `⚠ network access 'loose' in: …`, then it detaches — no prompt.
jailbee destroy feat-own-widening --force

# 3. A step attaching a host mount always asks — even from your own repo.
#    Commit `mounts: [<an optional_mounts key>]` on a branch and push it.
jailbee new feat/mount-widening --background
# expect: the ⚠ block with `attaches host mount(s): …` and the question
#         "Provision with the branch's widened privileges? [y/N]" IN THIS
#         TERMINAL, before anything detaches.
#   answer n → "✗ Aborted: … Nothing was created."; `jailbee ls` shows no row and
#              `jailbee job ls` no job. Nothing to clean up.
#   answer y → it detaches; the worker does not ask again.
jailbee new feat/mount-widening --background --yes   # accepts up front, no question
jailbee destroy feat-mount-widening --force

# 4. No terminal to ask on: the answer is not silently "no".
jailbee new feat/mount-widening2 --background < /dev/null
# expect: exit 2 with "…needs confirmation — and there is no terminal to ask
#         on. Re-run with --yes…". Nothing created.

# 5. A FORK PR is the one case where a loose step DOES ask.
jailbee new --pr <N> --background   # N from a fork, adding a loose autostart step
# expect: the question, in this terminal.
jailbee new --pr <M> --background   # M an INTERNAL PR with the same kind of step
# expect: no question — its head is a branch in your own origin, so it is
#         treated exactly like `jailbee new <that-branch>`.
# `--no-autostart` skips the read entirely (no diff, no question) — the
# least-risk way to build a review container from a fork.
```

## `jailbee destroy --all` and interactive picker smoke test

```bash
# Set up a couple of containers
jailbee new feat/destroysmoke-a --no-clone --no-autostart
jailbee new feat/destroysmoke-b --no-clone --no-autostart
jailbee new feat/destroysmoke-c --no-clone --no-autostart
jailbee ls

# 1. Interactive checkbox (no args) — tick a and c, leave b
jailbee destroy
# expect: questionary checkbox list with all three rows
# expect after confirming: "Destroyed: feat-destroysmoke-a", "Destroyed: feat-destroysmoke-c"
jailbee ls   # only feat-destroysmoke-b remains

# 2. --all with confirmation — answer "y"
jailbee destroy --all
# expect: "Destroy 1 container(s) (feat-destroysmoke-b)? [y/N]"
jailbee ls   # empty (or just the "(no containers found)" line)

# 3. --all on an empty repo
jailbee destroy --all
# expect: "No containers to destroy."

# 4. --all --force on a fresh batch — no prompt
jailbee new feat/destroysmoke-d --no-clone --no-autostart
jailbee new feat/destroysmoke-e --no-clone --no-autostart
jailbee destroy --all --force
jailbee ls   # empty

# 5. Mutex error
jailbee destroy feat-x --all
# expect: exit 2, "--all and a container name are mutually exclusive"

# 6. Non-TTY guard (run from a script or pipe)
echo "" | jailbee destroy
# expect: exit 1, "no container name given; pass a name, use --all, ..."
```

## `jailbee ls` git-status columns + `jailbee git diff` smoke test

AHEAD ± and ↑ are measured against the container's **base branch**
(`user.jailbee.base_branch`, recorded at `jailbee new` time), not the host's
checked-out HEAD. The comparison is `<base>...HEAD` (live merge-base,
i.e. "PR view"), where `<base>` resolves in order:
`refs/jailbee/base/<base>` (jailbee-managed; seeded at `jailbee new`, advanced by
`jailbee pull`/`jailbee push` and not clobbered by an in-container `git fetch`) →
`refs/remotes/origin/<base>` (legacy fallback) → `refs/heads/<base>` →
`origin/<default>`. `jailbee git diff` uses the same base resolution so its
output stays consistent with what AHEAD shows.

The **MERGE** column shows a best-effort conflict indicator against the base:
- blank / `ok` — container branch merges cleanly into its base branch
- `conflict` — would conflict
- `?` — base ref not found in the host repo
- `—` — container is stopped or in mount mode (no git access)

```bash
# 1. Verify columns appear and update as the container's git state changes.
jailbee new feat/lsstat --no-autostart
jailbee ls
# expect: feat-lsstat row shows BASE=main, WT=clean, AHEAD ±=clean, ↑=0, MERGE=ok

jailbee shell feat-lsstat
cd ~/SampleApp
echo dirty > dirty.txt && git add dirty.txt
exit
jailbee ls
# expect: WT shows "+1 -0" (or similar), AHEAD ± still clean, ↑=0

jailbee shell feat-lsstat
cd ~/SampleApp
git commit -m "smoke commit"
exit
jailbee ls
# expect: WT=clean, AHEAD ±="+1 -0", ↑=1, MERGE=ok

# 2. MERGE=conflict: create a divergence between the container and its base.
# Advance main on the host with a change that conflicts with the container.
echo "conflict-from-host" > dirty.txt
git add dirty.txt
git commit -m "host conflict"
jailbee ls
# expect: feat-lsstat MERGE=conflict (same file modified on both sides)

# Undo the host commit to restore a clean baseline for remaining steps.
git revert HEAD --no-edit

# 3. jailbee git diff variants
jailbee git diff feat-lsstat                  # default: patch for the smoke commit (vs base)
jailbee git diff feat-lsstat --stat           # shortstat summary only
jailbee git diff feat-lsstat --wt             # empty (nothing uncommitted)
jailbee git diff feat-lsstat --all            # full patch (committed only since WT clean)

# 4. Edge cases: stopped + mount-mode show "—" in all four git columns.
jailbee stop feat-lsstat
jailbee ls
# expect: feat-lsstat row WT/AHEAD ±/↑/MERGE all "—"

jailbee new mountsmoke --mount
jailbee ls
# expect: mountsmoke row WT/AHEAD ±/↑/MERGE all "—"; BASE=main
jailbee git diff mountsmoke 2>&1 | grep "mount mode"

# 5. Legacy containers (created before this feature)
# A container without the user.jailbee.base_branch label shows BASE="—".
# Verify by manually unsetting the label:
incus config unset feat-lsstat user.jailbee.base_branch
jailbee start feat-lsstat
jailbee ls
# expect: feat-lsstat row shows BASE="—", MERGE="?"

jailbee destroy feat-lsstat --force
jailbee destroy mountsmoke --force

# 6. Picker shows the same fields
jailbee new feat/pickersmoke --no-autostart
jailbee destroy
# expect: questionary checkbox row contains BASE=main, WT, AHEAD ±, ↑, MERGE
# (Ctrl+C to cancel)
jailbee destroy feat-pickersmoke --force
```

## `jailbee dashboard` smoke test

> Host-only. Requires a TTY (raw-mode input + alternate screen). Shows
> every jailbee-managed container across all registered repos plus the cwd
> repo, grouped by repo.

```bash
# From any repo (or none): launch the live view.
jailbee dashboard
# expect: alternate-screen TUI, one section per repo with its containers,
#         a highlighted row, and a footer "↑/↓ move · Enter actions · r refresh · q quit".

# Create activity in another terminal and watch it appear within a few seconds:
jailbee new feat/dashsmoke --background
# expect: the feat-dashsmoke row appears with a JOB phase, then clears when ready.

# Navigate + act:
#  ↑/↓ (or j/k) to move the highlight (skips repo headers, spans repos)
#  Enter -> action menu (tmux/shell/ide/chrome/restart/stop/destroy when Running;
#           start/destroy when Stopped). It opens inline BELOW the table —
#           expect the container rows to stay on screen and keep refreshing
#           behind it. ↑/↓ move the menu cursor, Esc/q close it without acting.
#           Pick "Open shell" -> lands in the container; exit -> returns to the
#           dashboard, which refreshes.
#           On an orphan (view-only) row, Enter opens nothing and prints a
#           yellow note in the panel footer for ~2.5s instead of going silent.
#  t -> attaches tmux for the highlighted container without the menu; exit ->
#       back to the dashboard. s = shell, i = IDE, c = Chrome, p = open PR.
#       On a row that does not offer the action (Stopped container, IDE/Chrome
#       disabled in that repo's config, no PR, orphan row) expect NO dispatch
#       and a yellow footer note naming the key and the reason.
#  h (or ?) -> keybinding help below the table; h again or Esc closes it.
#              Pressing h with the action menu open swaps the menu for help.
#  r -> forces an immediate full refresh (incl. git status)
#  q -> quits (closes the action menu or help first, if open)
#  Ctrl-C -> always quits, restoring the terminal, even with an overlay open

# Two-tier refresh: base state (state/ip/op) updates every ~3s; git columns
# (WT/AHEAD/↑/MERGE) update every ~10s. Tune with -i / --git-interval, or
# drop git entirely:
jailbee dashboard --no-git -i 2

# Orphans: a jailbee-managed container whose repo isn't registered and isn't the
# cwd repo shows under its prefix as "(orphan — no config)" and is view-only
# (Enter reports it's view-only).

# Cleanup:
jailbee destroy feat-dashsmoke --force

# Non-TTY guard:
echo "" | jailbee dashboard
# expect: exit 1, "jailbee dashboard requires an interactive terminal."
```

## `refs/jailbee/base` AHEAD-after-pull smoke test

```bash
git checkout main
jailbee new feat/baseref
jailbee shell feat-baseref
cd ~/SampleApp && echo x > x.txt && git add . && git commit -m "baseref smoke" && exit
jailbee ls            # feat-baseref AHEAD ±/↑ show the new commit (↑=1)

jailbee pull feat-baseref --into main --no-cleanup   # keep container; merges into base 'main'
jailbee ls            # feat-baseref now ↑=0, AHEAD ±=clean (integrated into host main)

# A later in-container fetch must NOT re-inflate the number.
jailbee shell feat-baseref
cd ~/SampleApp && git fetch origin && exit
jailbee ls            # still ↑=0 — refs/jailbee/base/main is untouched by git fetch

jailbee destroy feat-baseref --force
```

## Claude auto-update smoke test

> Requires `claude.enabled: true` (e.g. in `~/.config/jailbee/global.yaml`)
> and network access to `downloads.claude.ai`.

```bash
# Fresh shared store: first container full-installs Claude into
# <shared_dir>/claude-install (shared across all containers).
rm -rf <shared_dir>/claude-install/*    # simulate empty store (optional)
jailbee new feat/claude-a
jailbee shell feat-claude-a
claude --version        # latest release, not a golden-baked version
ls ~/.local/share/claude/versions
exit

# Second container reuses the shared store (no re-download) and, with
# auto_update on (default), advances to any newer release.
jailbee new feat/claude-b
jailbee shell feat-claude-b
readlink ~/.local/bin/claude   # points into the shared versions dir
exit

# auto_update=false leaves an existing install untouched but still
# installs when missing.
cat >> .jailbee/config.yaml <<'YAML'
claude:
  auto_update: false
YAML
jailbee new feat/claude-c     # links to existing shared version, no `claude update`

jailbee destroy --all --force
# Remove the claude.auto_update block from .jailbee/config.yaml afterwards.
```

## GUI dashboard (`jailbee gui`)

Requires a real Incus daemon, at least one JailBee container, and PySide6
(`uv tool install -e '.[gui]'`).

1. `jailbee gui` (or `jailbee dashboard --gui`) — the command detaches to the
   background and returns immediately (prints "Launched jailbee dashboard GUI
   in the background…"); a window then appears listing containers grouped
   by repo, refreshing live. If no window appears, check
   `/tmp/jailbee-gui.log` for the failure (e.g. missing PySide6 platform
   plugins). Use `jailbee gui --foreground` to run attached instead, so
   errors surface directly in this shell.
2. Right-click a running container → the action menu (tmux/shell/ide/chrome/
   restart/stop/destroy). Left-click to select; the selection survives a
   refresh.
3. Choose **Open shell** → a host terminal opens running `jailbee shell <name>`.
   (If none opens, install a terminal emulator or set `$JAILBEE_TERMINAL`.)
4. Choose **Launch Chrome** / **Launch IDE** → the app appears on your desktop.
5. Choose **Destroy** → confirm the dialog; the row disappears on the next
   refresh.
6. Stop a container from the CLI in another shell; confirm the GUI reflects it
   within `--interval` seconds.
7. **View** menu → switch **Table** ↔ **Cards**. Cards should re-wrap columns
   as you resize the window (one column when narrow, several when wide);
   right-click actions and selection should work identically in both.
8. Resize a Table column, switch layout via the **View** menu, adjust the
   **Refresh** menu's cadence (or pause it), then close the window and
   relaunch `jailbee gui`. Confirm the layout, column widths/order, and refresh
   cadence/paused state came back — but the window's size/position did not
   (that's left to the window manager).
9. Relaunch with `jailbee gui --interval 7`: the explicit flag should win over
   whatever cadence was persisted in step 8.

### Clearing a failed job from the dashboards

1. Make a background create fail (see the `feat-bgfail` recipe above).
2. `jailbee dashboard` → highlight the container → `Enter`.
   Expect **Clear failed job** as the *first* menu entry. Choose it.
   Expect the JOB column to go empty on the next refresh; the container
   stays running.
3. `jailbee gui` → the container's card shows a red `failed` pill in its header
   (both Compact and Grid styles); hover it for the recorded error.
   Right-click the card → **Clear failed job**. Expect the pill to vanish
   on the next refresh.
4. `jailbee job clear <name>` on a container whose worker is still alive must
   refuse with a "still running" message and leave the row in place.

## Verifying `list_containers` reads config from the list payload

`list_containers()` reads each container's `user.jailbee.*` keys from the
`incus list --format json` `config` block rather than per-key
`incus config get` calls. This assumes the running Incus daemon inlines the
instance-local `user.*` keys into the list payload (verified 2026-07-20).
Re-run the check after an Incus upgrade:

    bash .local/verify-list-config-keys.sh

PASS means the refactor is safe on that daemon. FAIL means some set key is
missing from the list JSON — restore `incus.config_get` for the affected
keys in `list_containers`.

## Auto-target confirmation smoke test

Exercises `confirm.auto_target` (default on): the plan-and-confirm block that
`push`/`pull`/`checkout` show when JailBee — not the user — picks the container.
Requires a real Incus daemon and exactly one running container for the repo.

```bash
# 1. Auto-selected push is confirmed, and the block names the real branches.
jailbee push
# expect: "Push  host ──▶ container", source = the container's base branch
#         (origin/<base>), target = the container's current branch, then [Y/n].

# 2. Declining aborts before anything reaches the container or a host branch.
jailbee exec <name> -- git rev-parse HEAD     # note the OID (runs in the container's clone)
jailbee push                                  # answer "n"
# expect: exit != 0, and the container HEAD is unchanged. Note: the host's
#         own origin/<source> remote-tracking ref may have moved regardless
#         of the answer — push runs its "git fetch origin <source>" before
#         the prompt so the block can show the real tip. That's the only
#         side effect of declining; no container state, no host branch, no
#         working tree changes.

# 3. The flag skips the prompt.
jailbee push --no-confirm
# expect: no block, no prompt, the push runs.

# 4. Pull and checkout behave the same, in the other direction.
jailbee pull        # expect "Pull  container ──▶ host" + [Y/n]
jailbee git checkout  # expect "Checkout  container ──▶ host" + [Y/n]

# 5. Two containers -> picker, no confirmation.
jailbee new feat-second
jailbee push
# expect: the multi-select picker, and no plan block afterwards.

# 6. Config opt-out.
#    Add to .jailbee/config.yaml:  confirm: { auto_target: false }
jailbee push
# expect: no block (back to the pre-feature behaviour). Remove the key after.

# 7. Push off a TTY never reaches the confirmation at all (unlike pull/checkout).
jailbee push < /dev/null
# expect: exit 1, "No container name given. Pass a name, or run
#         interactively in a TTY for the container picker." — no plan block,
#         because push requires an explicit name before it ever lists
#         containers when stdin isn't a TTY.
```

## LOCAL diff and the destroy guard

Both features depend on real object presence in the container's and host's
git object stores, so they need a real Incus daemon to exercise honestly.

`LOCAL ±`/`L↑` (`local_diff`/`local_count`) compare the container's HEAD to
the host's **currently checked-out branch** — not the container's pinned
base branch, which is what `AHEAD ±`/`↑` already shows. Both are off by
default; request them with `--fields` or an `ls: {fields: [...]}` config
block.

**A note on `?` before you try to force it:** a clone-mode container is
cloned with `git clone --shared` against the read-only `host-source` bind
mount, which stays attached for the container's whole life. Because of
that, the container can read *any* object the host repo currently holds —
including one the host committed after the container was created — the
moment it needs to, with no fetch involved. In practice this means the
everyday "host moved on since the container was cloned" case still resolves
to a real number, not `?`. The reliable way to force `?` is a container
with **no git repository to probe at all** (step 3 below) — there is
nothing for the probe to check object presence against, on either side.

```bash
# 1. Commit inside a container without pulling it to the host. With the
#    host on the same branch the container was cloned from, the probe
#    resolves the direction from inside the container in one round-trip.
git checkout main
jailbee new feat/localdiffsmoke
jailbee shell feat-localdiffsmoke
cd ~/SampleApp && echo x > x.txt && git add . && git commit -m "container-only commit" && exit

jailbee ls --fields name,local_diff,local_count
# expect: feat-localdiffsmoke row shows real numbers, e.g. LOCAL ±="+1 -0", L↑=1
# (the container already holds the host's current HEAD as an object from
# its own clone, so the container-side probe answers directly)

jailbee destroy feat-localdiffsmoke
# expect: "Destroy container 'feat-localdiffsmoke'?" [y/N] -> y, then a risk
# summary "feat-localdiffsmoke: 1 commits not on the host" and the second
# confirmation "Destroying loses this. Continue? [y/N]" — decline (n) to
# keep the container for step 2

# 2. Pull the work to the host (this fetches the container's branch into
#    refs/jailbee/feat-localdiffsmoke/feat/localdiffsmoke before merging, which is
#    what makes the commit "on the host" from here on) — the same destroy
#    then has nothing to warn about.
jailbee git pull feat-localdiffsmoke --no-cleanup
jailbee ls --fields name,local_diff,local_count
# expect: LOCAL ±=clean, L↑=0 (the container's HEAD is now an ancestor of
# the host's new merge commit, which the container can also read live)
jailbee destroy feat-localdiffsmoke
# expect: "Destroy container 'feat-localdiffsmoke'?" [y/N] -> y, then straight
# to "Destroyed: feat-localdiffsmoke" — no risk summary, no second prompt (the
# commit is reachable from the host now)

# 3. Force "?" deliberately, and exercise the guard's "could not inspect"
#    branch at the same time: a container with no git repo at all still
#    gets probed (it's running, not mount-mode), but the probe finds no
#    .git to check — every field, including LOCAL, comes back "?".
jailbee new noclonesmoke --no-clone --no-autostart
jailbee ls --fields name,wt,ahead_diff,local_diff,local_count
# expect: noclonesmoke row shows "?" in every git column shown, LOCAL ± and
# L↑ included — nothing was measured, and that is rendered as unknown, not
# "clean"

jailbee destroy noclonesmoke
# expect, in order: "Destroy container 'noclonesmoke'?" [y/N] -> y, then
# "noclonesmoke: could not inspect the container" and the second
# confirmation "Destroying loses this. Continue? [y/N]" — the guard treats
# an all-unmeasured probe on a running container as a real risk it cannot
# rule out, the same as a genuine probe failure would read; decline, then:
jailbee destroy noclonesmoke --force

# 4. --force skips both prompts and the probe entirely.
jailbee new feat/forcesmoke2 --no-clone --no-autostart
jailbee destroy feat-forcesmoke2 --force
# expect: no confirmation, no risk summary, immediate "Destroyed: ..."

# 5. A container that was never probed at all — the normal state for a
#    STOPPED container — gets a note but no extra prompt: there is nothing
#    measured to call a risk, and the guard never invents one.
jailbee new feat/stopsmoke --no-autostart
jailbee stop feat-stopsmoke
jailbee destroy feat-stopsmoke
# expect: "Destroy container 'feat-stopsmoke'?" [y/N] -> y, then
# "git status unknown for: feat-stopsmoke" and straight to "Destroyed: ..."
# — no second confirmation

# 6. jailbee git pull's post-merge cleanup runs the identical guard — it only
#    fetches the container's committed history, so it never sees
#    uncommitted work on its own; the guard is what catches that here.
jailbee new feat/pullguard
jailbee shell feat-pullguard
cd ~/SampleApp && echo z > z.txt && git add . && git commit -m "pull-guard commit" && exit
jailbee shell feat-pullguard
cd ~/SampleApp && echo "not committed" >> z.txt && exit   # leave uncommitted work
git checkout main
jailbee git pull feat-pullguard
# expect (after the merge output): "Destroy container 'feat-pullguard'?
# [y/N]" -> y, then a risk summary "feat-pullguard: working tree +1 -0"
# (exact numbers may vary) and "Destroying loses this. Continue? [y/N]"
# decline both prompts to keep the container; verify it is still present:
jailbee ls | grep feat-pullguard

# `destroy_container: always` is this call's own --force equivalent and
# skips the guard outright (uncommitted work would be silently discarded):
cat >> .jailbee/config.yaml <<'YAML'
pull:
  destroy_container: always
YAML
jailbee git pull feat-pullguard
# expect: no prompt at all; the container is destroyed regardless of the
# uncommitted change from above
# remove the pull: block from .jailbee/config.yaml afterwards
```

## Registry mirror recovery smoke test

Simulate a provisioning run that died after `apt-get install` but before the
Quadlet unit landed, then confirm `jailbee registry up` repairs it in place.

```bash
jailbee registry up                    # start from a healthy mirror
# if no mirror exists yet on this host, this first call takes the full
# create-and-provision path (multi-minute: apt-get install podman etc.) —
# run it once and confirm it succeeds before continuing below
# run inside the mirror:
incus exec jailbee-registry-mirror -- rm /etc/containers/systemd/jailbee-registry-proxy.container
incus exec jailbee-registry-mirror -- systemctl stop jailbee-registry-proxy.service
jailbee registry status
# expect: degraded

jailbee registry up
# expect: reinstall starts immediately (no 60s wait)
# expect: "Registry mirror running on jailbee-registry-mirror.incus:3128"
jailbee registry status
# expect: running
```

Confirm the per-repo upstream list survived the repair — this is what the
`test -f` guard in install.sh protects:

```bash
incus exec jailbee-registry-mirror -- cat /etc/jailbee-registry-proxy.env
# expect: REGISTRIES= still lists the repo's extra_registries, not an empty value
```

Then the escape hatch, which must preserve the CA (user containers trust it):

```bash
sha256sum ~/.local/share/jailbee/registry/ca/ca.crt
jailbee registry up --recreate
# expect: "Recreating jailbee-registry-mirror from scratch; ..." then a full provision
sha256sum ~/.local/share/jailbee/registry/ca/ca.crt
# expect: identical hash
jailbee registry status
# expect: running
```

## Nested Incus probe rig (verifying device behaviour from inside a container)

Every recipe above needs the host's daemon. This one does not: it brings up a
second Incus daemon *inside* a JailBee container, so questions of the form
"does Incus really accept these device properties, and what does it say when
it doesn't?" can be answered without leaving the container. The unit suite is
fully mocked by design, so this rig is the only place those answers come from.

It works because JailBee's base profile already sets `security.nesting: true`
(`profiles.base_profile_yaml`). `.jailbee/install.d/75-incus.sh` bakes the
daemon into this repo's golden image with its units disabled, a `default` dir
pool already created, root's subuid range capped so instances start without
per-instance tuning, and the `dev` user in `incus-admin` so `incus` — and
therefore `jailbee` — works without `sudo`. Two commands from a fresh
container:

```bash
sudo systemctl start incus.service
incus launch images:alpine/edge probe1
incus list
# expect: RUNNING, no IPv4/IPv6 beyond loopback (nothing here creates a NIC)
```

Starting the unit needs root; everything after it does not. If `incus` reports
"You don't have the needed permissions to talk to the incus daemon", the shell
predates the `incus-admin` grant — group membership is per-session, so reopen
`jailbee shell`.

A `cgroup2_devices ... Failed to load bpf program` line in the instance log is
expected under nesting and harmless.

With that up, a proxy device round-trip takes two commands. The "host" side is
the JailBee container itself:

```bash
# host side: something to reach
python3 -m http.server 5037 --bind 127.0.0.1 &

# instance listens, traffic lands on the host's service (the adb-style case)
incus config device add probe1 probe-fwd proxy \
    listen=tcp:127.0.0.1:5037 connect=tcp:127.0.0.1:5037 bind=instance
incus exec probe1 -- wget -qO- http://127.0.0.1:5037/
# expect: the host service's response

# the other direction: host listens, traffic lands on the instance's service
incus exec probe1 -- sh -c \
    'nohup sh -c "while true; do printf \"HTTP/1.0 200 OK\r\n\r\nOK\n\" | nc -l -p 8080 -s 127.0.0.1; done" >/dev/null 2>&1 &'
incus config device add probe1 probe-pub proxy \
    listen=tcp:127.0.0.1:18080 connect=tcp:127.0.0.1:8080 bind=host
curl -s http://127.0.0.1:18080/
# expect: OK
```

### Findings (Incus 6.0.5, 2026-08-18)

Kept here because they are what the mocked tests are written against.

- `bind=instance` is Incus's name; `bind=container` is accepted as an alias
  from LXD and works, but the daemon stores whichever string it was given, so
  reads must treat both as the same thing.
- `incus list --format json` returns each instance's `devices` and
  `expanded_devices` maps, so reading forwards back needs no new wrapper
  method — `Incus.list_containers()` already carries them.
- Adding a device to a **stopped** instance succeeds and starts working on the
  next boot; devices survive restarts.
- `nat=true` is unusable for this purpose: instance-bound proxies are refused
  outright (`Only host-bound proxies can use NAT`) and host-bound ones require
  the connect address to be one of the instance's static IPs.
- A forward whose target has nothing listening is added without complaint —
  connections are refused at connect time, not at add time. So no pre-flight
  check on the target service is needed, or possible.
- Conflicts and typos, verbatim:
  - duplicate device name → `The device already exists`
  - host port taken → `Failed to listen on 127.0.0.1:5037: ... bind: address already in use`
  - port already bound *inside* the instance → `Failed to receive fd from
    listener process: Failed to receive file descriptor via abstract unix
    socket`, which names neither the port nor the cause and needs translating
    before a user sees it
  - missing protocol prefix → `Unknown protocol type "127.0.0.1"`
  - `udp:` listen with a `tcp:` connect → `Proxying from udp to non-udp
    protocol is not supported`
- An out-of-range port (`70000`) passes validation and fails only at device
  start, so range checking belongs on JailBee's side.

### What is not verified yet

Running JailBee itself inside a container (`jailbee init`, `jailbee new`) is a
separate question and remains open. What is known:

- A nested bridge comes up: `incus network create incusbr0` succeeds, dnsmasq
  serves the range, nft masquerade rules are installed, and an instance on it
  reaches the bridge gateway.
- A DHCP lease did **not** arrive in a hotplugged instance (`udhcpc` hung);
  static addressing on the same bridge worked, so this is a client/hotplug
  detail rather than a broken bridge.
- Egress *out* of the nested bridge was never actually tested: the container
  it was tried in was in `strict` mode with an empty allowlist at the time, so
  it had no egress of its own to forward. Re-test from a `loose` container
  before drawing any conclusion.
- Untouched beyond that: whether network ACLs enforce correctly two levels
  deep, and whether `jailbee base build` can publish an image from inside a
  container (that needs a full apt provision at nesting depth two).

Tear the rig down when done; the pool is a plain directory under
`/var/lib/incus`:

```bash
incus delete probe1 --force
sudo systemctl stop incus.service
```
