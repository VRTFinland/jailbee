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

## `jailbee restart --background` smoke test

> Host-only. Verifies the detached boot worker + `jailbee ls` JOB tracking.
> State is in the SQLite `background_op` table (`op_kind='boot'`). Use a
> container whose `on_start` steps take long enough to observe.

```bash
jailbee new feat/bootsmoke
jailbee restart feat-bootsmoke --background
# expect: "🔁 'feat-bootsmoke' is restarting in the background (pid N) ..."
jailbee ls          # JOB column: starting, then autostart, then clears
jailbee job ls      # KIND column shows `boot` while it runs

# The attach wait ends at `autostart`, not at completion:
jailbee restart feat-bootsmoke --background
jailbee tmux feat-bootsmoke   # returns as soon as the container is back up

# Second boot over a live one is refused (run both within the same run):
jailbee restart feat-bootsmoke --background
jailbee restart feat-bootsmoke --background 2>&1 | grep -i "already has a background job"

# `start` reuses the same worker without the reboot:
jailbee stop feat-bootsmoke
jailbee start feat-bootsmoke --background
# expect: "🔁 'feat-bootsmoke' is starting in the background (pid N) ..."

# Config default + override:
printf 'boot:\n  background: true\n' >> .jailbee/config.yaml
jailbee restart feat-bootsmoke                  # backgrounds without the flag
jailbee restart feat-bootsmoke --no-background  # forces foreground
# remove the boot: block from .jailbee/config.yaml afterwards

# Failure path: break an on_start step, then
jailbee restart feat-bootsmoke --background
jailbee ls          # JOB=failed; the container itself is up
jailbee job ls      # error message + worker log path
jailbee shell feat-bootsmoke   # warns about the failed job, offers to attach anyway
jailbee job clear feat-bootsmoke
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

# Failure path: an autostart step fails, so the container is created and
# running but the job row is `failed`. The attach reports it and offers to go
# in anyway — no flag needed. Works on shell/tmux/ide/chrome.
jailbee new feat/waitfail --background   # let it fail in an autostart step
jailbee tmux feat-waitfail
# expect: "⚠ background creation of 'feat-waitfail' failed: ...",
#         "  The container itself is up — 'jailbee tmux' can still reach it."
#         "  Once you're done, clear the stale job record: jailbee job clear feat-waitfail"
#         then "Continue anyway? [Y/n]" — Enter drops you into the tmux
#         session with the failed window. Answering `n` exits 1.
# (The job row stays `failed` until you acknowledge it:
#    jailbee job clear feat-waitfail
#  — the container is left alone. `jailbee job ls` shows the recorded error and
#  the worker log path first; `jailbee job log feat-waitfail` prints the log.)

# --force only skips the question, so scripts and the dashboards don't stall.
# A non-interactive stdin (a script, a cron job) is treated the same way.
jailbee tmux feat-waitfail --force        # same warning, no prompt

# From the dashboard, `t`/`s`/`i`/`c` (and the Enter menu) dispatch with
# --force: the JOB column already showed `failed`.
jailbee dashboard                         # highlight feat-waitfail, press t

# Nothing to attach to: when the create failed before `incus init` there is no
# container, so the attach refuses without asking.
jailbee new feat/nosuchbase nonexistent-base --background   # fails before creating
jailbee tmux feat-nosuchbase
# expect: "✗ background creation of 'feat-nosuchbase' failed: ...", the
#          "Nothing was created; clear the job record: jailbee job clear
#          feat-nosuchbase" hint, exit 1, no prompt and no traceback.

# Ctrl-C out of a healthy wait, once the container exists: same offer.
jailbee new feat/waitslow --background
jailbee tmux feat-waitslow                # Ctrl-C while the spinner runs
# expect: "⚠ 'feat-waitslow' is still being created in the background — its
#          setup is unfinished." then "Attach anyway? [y/N]" — defaulting to
#          no, and asked even under --force, because Ctrl-C means cancel.
#          Before the container exists you get the "check `jailbee ls`" exit.

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

> Host-only. Puts the tree on a branch locally (no host<->container transport).

```bash
# Host repo: detach a submodule, then re-align it to the current branch.
git checkout -b feat/align-smoke
git -C <submodule-path> checkout --detach
git submodule status --recursive            # shows detached
jailbee submodule checkout
# expect: per-submodule "✓ feat/align-smoke" lines, then
#         "Submodules aligned to 'feat/align-smoke'."
git -C <submodule-path> branch --show-current   # -> feat/align-smoke

# -b switches the superproject too: one command for the whole tree.
git branch feat/other
jailbee submodule checkout -b feat/other
git branch --show-current                       # -> feat/other  (superproject moved)
git -C <submodule-path> branch --show-current   # -> feat/other

# --submodules-only keeps the superproject put (the pre-1.2 behaviour).
jailbee submodule checkout -b feat/align-smoke --submodules-only
git branch --show-current                       # -> feat/other  (unchanged)
git -C <submodule-path> branch --show-current   # -> feat/align-smoke

# A refused superproject checkout aligns nothing.
jailbee submodule checkout -b no/such/branch    # expect: exit 1, git's own error
git -C <submodule-path> branch --show-current   # -> feat/align-smoke (untouched)

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

## `jailbee submodule pr` smoke test

> Host-only. Requires a superproject with a submodule that has a GitHub remote
> of its own, and `gh auth login` on the host for that remote. This is the one
> part of the feature no unit test can cover: the real push goes to a real
> GitHub submodule remote.

```bash
jailbee new feat-sub
jailbee shell feat-sub
# inside container, inside the submodule:
cd ~/SampleApp/libs/foo
echo "submodule pr smoke" > sub.txt && git add . && git commit -m "feat: submodule pr smoke"
exit

jailbee submodule pr feat-sub
# expect: transport summary, git push output, then
#         "Draft PR #<N> created for 'libs/foo': https://github.com/..."
gh pr view <N> --repo <submodule-org>/<submodule-repo>   # base is the submodule's own default branch
git -C libs/foo log --oneline <base>..<head>             # only the submodule commits, nothing from the superproject
incus config get <prefix>-feat-sub user.jailbee.sub_pr    # → {"libs/foo": {"pr": <N>, ...}}

# Re-run: updates the same PR rather than opening a second one (the recorded
# head name is what protects this).
jailbee shell feat-sub
cd ~/SampleApp/libs/foo && echo more > more.txt && git add . && git commit -m "more" && exit
jailbee submodule pr feat-sub
# expect: "PR #<N> updated — head moved..." — same PR number as above
gh pr list --repo <submodule-org>/<submodule-repo> --head <head>   # exactly one open PR

# --open opens it in the browser without touching anything.
jailbee submodule pr feat-sub --open

jailbee destroy feat-sub --force
```

Two submodules with commits ahead is worth checking too: `jailbee submodule
pr feat-sub` with no `<path>` should list both candidates and exit 2 asking
you to name one.

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
#         a highlighted row, and a footer hint line reading (at minimum)
#         "↑/↓ (j/k) move · Enter menu · Space fold · t tmux · s shell ·
#          r refresh · F2 / S settings · h / ? help · q quit".

# Create activity in another terminal and watch it appear within a few seconds:
jailbee new feat/dashsmoke --background
# expect: the feat-dashsmoke row appears with a JOB phase, then clears when ready.

# Navigate + act:
#  ↑/↓ (or j/k) to move the highlight (spans repos; repo headers are cursor
#       stops now, not skipped — see the folding recipe below)
#  Enter -> action menu (tmux/shell/ide/chrome/restart/stop/destroy when Running;
#           start/destroy when Stopped). It opens inline BELOW the table —
#           expect the container rows to stay on screen and keep refreshing
#           behind it. ↑/↓ move the menu cursor, Esc/q close it without acting.
#           Pick "Open shell" -> lands in the container; exit -> returns to the
#           dashboard, which refreshes.
#           On an orphan (view-only) row, Enter opens nothing and prints a
#           yellow note in the panel footer for ~2.5s instead of going silent.
#  t -> attaches tmux for the highlighted container without the menu; exit ->
#       back to the dashboard. s = shell, i = IDE, c = Chrome, p = open PR,
#       P = create/update the PR, u = update from base, d = show the diff.
#       On a row that does not offer the action (Stopped container, IDE/Chrome
#       disabled in that repo's config, no PR, orphan row) expect NO dispatch
#       and a yellow footer note naming the key and the reason.

# Workflow entries (the menu block above tmux/shell). Make a commit inside the
# container first, so there is something for git pull and git diff to show:
jailbee shell feat-dashsmoke -- bash -lc 'cd ~/*/ && echo x >> README.md && git commit -am wip'
#  d (or the "Show diff (git diff)" entry) -> the diff opens in $PAGER
#     (less -R) WITH colour, not as a plain scroll-past dump; q in less returns
#     to the dashboard with the table intact.
#  u ("Update from base (git push)") -> in a repo with push.default_action:
#     ask, the CLI's own merge/rebase picker appears (the TUI hands over the
#     real terminal); after it finishes, expect a
#     "── press Enter to return to the dashboard ──" line and the output still
#     readable until you press Enter.
#  "Send commits to host (git pull)" -> same pause behaviour. On a container
#     with nothing ahead of its base, expect the entry to be ABSENT from the
#     menu (and `git pull` to have no quick key at all — by design).
#  "Job log" -> present only while a job row exists; on a live background job
#     it follows the worker log (Ctrl-C ends it), on a finished one it prints
#     once. Not bound to a quick key.
#  A --mount container offers none of pr/git push/git pull/git diff (no clone
#     of its own): jailbee new feat/mnt --mount, then Enter on that row.
#  h (or ?) -> keybinding help below the table; h again or Esc closes it.
#              Pressing h with the action menu open swaps the menu for help.
#  r -> forces an immediate full refresh (incl. git status)
#  q -> quits (closes the action menu or help first, if open)
#  Ctrl-C -> always quits, restoring the terminal, even with an overlay open

# Folding a repo group:
#  Space on a repo header -> the header collapses to "▸ <prefix> (N)" and its
#     container rows disappear; Space again (▾) unfolds and the rows return.
#  Space on any container row inside a group -> same fold/unfold, no need to
#     move the cursor up to the header first.
#  Enter on a header -> same effect as Space (mirrors the Qt card view's
#     clickable header); confirm it does NOT open the action menu.
#  Fold a group, then jailbee new inside it in another terminal -> the new
#     container's row stays hidden until you unfold; the header's count goes
#     up regardless.
#  Fold every group -> confirm the panel title still reports the total
#     container count plus an "N folded" note, and the cursor still has
#     somewhere to land (the headers).

# The settings overlay (F2 or S):
jailbee dashboard
#  F2 -> opens a panel below the table: "Fields" and "Repos" tabs, ↑/↓ move,
#     Space toggles, Tab switches tabs, Esc closes. Changes apply and persist
#     immediately -- there is no OK/Cancel; watch the live table update
#     behind the panel as you toggle a column.
#  S -> opens the same overlay. Terminals disagree on what F2 sends (some
#     send nothing usable over SSH/tmux); if F2 does nothing on your setup,
#     S is the reliable fallback -- try both once so you know which works
#     for your terminal.
#  On the Fields tab, toggle a column off then back on -> the table's header
#     row and every container row gain/lose that column immediately.
#  Toggle "pr" on with no PR container present -> column does not appear in
#     the table; the overlay row for it carries a dim
#     "(shown only when it applies)" note explaining why, rather than
#     looking like a bug.
#  Try to turn off the last enabled column -> refused (the checkbox stays
#     checked); there is no such thing as a table with zero columns.
#  Switch to the Repos tab (Tab) -> toggle a repo's fold state from here too;
#     confirm it matches what Space does from the live table.
#  A field vocabulary this long does not fit under a normal terminal height:
#     confirm the panel shows only a window of rows around the cursor (not
#     all ~20+ fields at once), with a dim "↑ N more" / "↓ N more" line when
#     rows are hidden above/below, and that moving to the very last field
#     scrolls it into view rather than losing it off the bottom.
#  Esc -> closes the overlay, back to the plain table.

# TUI and GUI settings are independent:
#  With the TUI dashboard open, toggle a column or fold a repo in its
#     overlay. Then, in another terminal, run `jailbee gui` and check
#     View ▸ Columns and the card view's fold state: neither reflects what
#     you just did in the TUI. Change something in the GUI, close it,
#     reopen the TUI dashboard (or press F2 again) -- the TUI's own state is
#     still exactly what you left it. Each front-end has its own row in
#     state.sqlite's view_prefs table.

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

# The shared store is pruned by Claude's own updater, so a version another
# container is pinned to can disappear under it. /etc/profile.d/jailbee-claude.sh
# heals that at login — simulate it by pinning to a version that doesn't exist:
jailbee shell feat-claude-a
ln -sfn ~/.local/share/claude/versions/0.0.0 ~/.local/bin/claude
exit
jailbee shell feat-claude-a    # a fresh login shell repairs the launcher
readlink ~/.local/bin/claude   # newest version in the store again, not 0.0.0
claude --version
exit

jailbee destroy --all --force
# Remove the claude.auto_update block from .jailbee/config.yaml afterwards.
```

## Shared Claude credential groups (`claude_credentials`) smoke test

Several repos on one host can share one Claude Code login by pointing
`claude_credentials` in `~/.config/jailbee/global.yaml` at the same group
name — see `.local/superpowers/specs/2026-08-27-claude-shared-credentials-design.md`
for the design. The mechanism rests entirely on **undocumented observations
of Claude Code 2.1.247**: that `CLAUDE_SECURESTORAGE_CONFIG_DIR` resolves the
credential directory independently of `CLAUDE_CONFIG_DIR`, and that the OAuth
refresh lock (`.oauth_refresh.lock`) is created inside that same directory.
Nothing here is read from public documentation, and none of it is guaranteed
to survive a future Claude Code release. The checks below are what stand in
for a contract until it breaks.

Needs a real Incus daemon and real Claude Code logins. Run the steps **in
this order** — the happy path looks identical whether or not step 1's repair
actually worked, so running it first would mask the one finding most likely to
regress silently.

Two repos, `SampleApp` and `SampleApp2` (any second checkout with its own
`container_prefix` will do), both with
`agents.claude.enabled: true`. Point both at the same group in
`~/.config/jailbee/global.yaml`:

```yaml
claude_credentials:
  group: worktest
```

1. **`jailbee new` before `jailbee apply` does not log the container out.**
   This is Finding 2 of the 2026-08-27 review: the `jailbee new` repair for
   `CLAUDE_SECURESTORAGE_CONFIG_DIR` used to fire unconditionally once the key
   was absent, which — in a repo that had `claude_credentials` added but never
   `apply`ed — wrote the env key to `<prefix>-base` while `<prefix>-binds`
   still had no `claude-creds` device, so Claude Code resolved an unmounted
   directory and reported "Not logged in" in every container of the repo. The
   fix makes the repair wait for the device.

   Starting from a repo that has **never** run `jailbee apply` since
   `claude_credentials` was set (a fresh `jailbee init` followed by editing
   `global.yaml`, or reuse a repo that predates this feature and add the key
   now):

   ```bash
   cd SampleApp
   incus profile show SampleApp-binds | grep -c claude-creds
   # expect: 0 — apply has not run yet
   jailbee new feat/creds-before-apply
   jailbee shell feat-creds-before-apply
   # inside: claude -p "hi"
   ```

   Expected: whatever this container's login state was before (logged in, or
   asking for `/login`) — **not** a "Not logged in" for a container that was
   previously authenticated via `<shared_dir>/claude`. That mount is
   unaffected either way; only the new `.claude-creds` mount is missing.

   ```bash
   exit
   jailbee apply
   incus profile show SampleApp-binds | grep claude-creds
   # expect: the device, now present
   jailbee shell feat-creds-before-apply
   # inside: claude -p "hi"
   # expect: logged in as the group's account (or /login if the group has
   #         no credential yet — see step 3)
   exit
   ```

2. **Concurrent rotation from two different repos is mutually excluded.**
   The one check that could invalidate the whole design. The primary lock
   (`.oauth_refresh.lock`) lives inside the shared group directory and is
   taken by every container that mounts it; the *legacy* sibling lock
   (`${realpath(dir)}.lock`) is a sibling path outside the shared directory
   and is therefore per-container, not shared. Exclusion depends entirely on
   both sides taking the primary lock.

   ```bash
   jailbee shell <a SampleApp container>
   # inside: start a long-running agentic prompt, don't wait for it to finish
   # from another terminal, while it's still running:
   jailbee shell <a SampleApp2 container>
   # inside, concurrently: another long-running agentic prompt
   ```

   Watch `<xdg_data_home>/jailbee/claude-credentials/worktest/` on the host
   for `.oauth_refresh.lock` and `.credentials.json` while both are running
   (`watch -n1 'ls -la ...; stat .credentials.json'`). Expected: no corrupted
   `.credentials.json` (valid JSON throughout, no truncation), and neither
   container gets logged out. A torn write, or one side rotating the other's
   token out from under it, is a real finding — it means the primary-lock
   assumption is wrong and the design needs re-examining, not a workaround.

3. **The happy path, both directions.**

   Join with an existing login — the credential should *move*, not be
   copied, and the container should stay authenticated:

   ```bash
   # before joining: SampleApp2 has its own login, group dir is empty
   ls <shared_dir(SampleApp2)>/claude/.credentials.json    # exists
   ls <xdg_data_home>/jailbee/claude-credentials/worktest/ # empty or absent
   # add claude_credentials to global.yaml for SampleApp2, then:
   jailbee apply
   ls <shared_dir(SampleApp2)>/claude/.credentials.json    # gone
   ls <xdg_data_home>/jailbee/claude-credentials/worktest/.credentials.json
   # expect: present — the file moved, not copied
   jailbee shell <a SampleApp2 container>
   # inside: claude -p "hi" — expect still logged in, no re-auth
   exit
   ```

   Leave, by removing the repo from `claude_credentials` (or setting it to
   `null` under `repos:`) and re-running `jailbee apply`:

   ```bash
   jailbee apply
   incus profile show SampleApp2-binds | grep -c claude-creds
   # expect: 0 — device gone
   incus profile show SampleApp2-base | grep -c CLAUDE_SECURESTORAGE_CONFIG_DIR
   # expect: 0 — env key gone
   jailbee shell <a SampleApp2 container>
   # inside: claude -p "hi"
   # expect: "Not logged in" — there is nothing to fall back to (see spec §8);
   #         one /login here re-establishes this repo's own credential
   exit
   ```

4. **A group rename only takes effect on `jailbee apply`.** The
   container-side path (`~/.claude-creds`) is a constant; only the *source*
   of the mount changes when the group name changes, and only `jailbee apply`
   rewrites that source.

   ```bash
   # rename the group in global.yaml, e.g. worktest -> worktest2, but do NOT
   # run `jailbee apply` yet
   jailbee new feat/creds-after-rename
   incus profile show SampleApp-binds | grep claude-creds
   # expect: source still names the OLD group directory (.../worktest/)
   jailbee doctor
   # expect: reports the NEW group name (worktest2) as this repo's resolved
   #         group — doctor and the actual mount disagree until `apply` runs
   jailbee apply
   incus profile show SampleApp-binds | grep claude-creds
   # expect: source now names .../worktest2/
   jailbee destroy feat-creds-after-rename --force
   ```

5. **The container's dev user can read the host `0700` group directory.**
   Unlike every other shared-mount subdirectory, the group directory lives
   outside `shared_dir` and is created `0700` on the host (deliberately: it
   holds a live credential). This is reasoned from the existing `raw.idmap
   uid<->uid` mapping on `<prefix>-base` — the same mechanism every other
   host mount relies on — but has never been measured for a directory outside
   `shared_dir` specifically.

   ```bash
   ls -la <xdg_data_home>/jailbee/claude-credentials/worktest/
   # note the host owner/mode (0700, host user)
   jailbee shell <a SampleApp container>
   # inside:
   ls -la ~/.claude-creds/
   cat ~/.claude-creds/.credentials.json > /dev/null && echo "readable"
   # expect: readable and writable as the container's dev user, same as any
   #         other idmapped mount — no permission denied
   exit
   ```

6. **Two credentials: the prompt, all three answers.** The case every host
   that adopts a group after already logging in per-repo hits on the second
   `jailbee apply` — this used to be a hard refusal thrown from the middle of
   `run_apply`, leaving the profiles unwritten. Needs two *different* logins
   to be worth running: the point is watching which account each container
   reports afterwards, and two `/login`s to the same account are
   indistinguishable here.

   ```bash
   # SampleApp is grouped and applied (its login is now the group's).
   # Give SampleApp2 its own, different login, then point it at the group:
   ls <shared_dir(SampleApp2)>/claude/.credentials.json               # exists
   ls <xdg_data_home>/jailbee/claude-credentials/worktest/.credentials.json  # exists
   cd SampleApp2
   jailbee apply
   ```

   Expected: a warning naming both paths, a hint printing the runnable
   `claude_credentials.repos` block, then a three-row picker.

   * **cancel** (or Ctrl-C) → exit 1 with the original refusal text; both
     files still present and byte-identical. Verify with `md5sum` on both
     before and after — "changes nothing" is the claim most worth checking.
   * **the group's login** → `<shared_dir(SampleApp2)>/claude/.credentials.json`
     is gone, the group's file is unchanged (same `md5sum` as before), and a
     `jailbee shell` in a SampleApp2 container reports the *group's* account.
   * **this repo's login** → the group file now has SampleApp2's old
     `md5sum`, SampleApp2's copy is gone, and a container in **SampleApp**
     (the other member) reports SampleApp2's account after a restart. That
     re-points every member repo, which is the answer worth being sure about.

   Finally, the no-TTY path, which must refuse rather than block:

   ```bash
   # restore a credential on both sides first, then:
   jailbee apply < /dev/null
   # expect: exit 1 with the refusal, no prompt rendered, both files intact
   ```

Afterwards, clean up: `jailbee destroy` the containers created above, remove
`claude_credentials` from `global.yaml` if it was added only for this test,
`jailbee apply` in each affected repo, and (if step 3's group directory was a
throwaway) delete `<xdg_data_home>/jailbee/claude-credentials/worktest*/` by
hand — jailbee never deletes a group directory automatically.

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

### Workflow commands in the Qt dashboard

The point of these checks is that nothing opens a terminal emulator and no
output disappears. Use a container with a commit of its own (see the
`git diff`/`git pull` recipes above).

1. Right-click a running container → **Show diff (git diff)**. Expect a JailBee
   window (not a terminal) with the diff in a monospace font, `exited 0` on its
   status line, and a working **Copy** button. The dashboard behind it keeps
   refreshing.
2. Open a second output window while the first is up (e.g. **Job log** on a
   container mid-`jailbee new`). Both should stay usable — the windows are
   non-modal.
3. On that live job, press **Stop**: the status line must say `stopped`, not
   `exited 0`, and `pgrep -f "jailbee job log"` must find nothing afterwards.
   Closing the window with the command still running must do the same.
4. **Update from base (git push)** in a repo whose `push.default_action` is
   `ask` → a dialog asks merge/rebase/plain first, then the output window shows
   the push. **Cancel** in the dialog must dispatch nothing at all. In a repo
   that pins `push.default_action`, expect no dialog.
5. **Send commits to host (git pull)** → a confirmation naming the host branch
   the merge lands on; declining dispatches nothing.
6. **Create/update PR** → a dialog for draft/ready, description regeneration
   and the existing-PR-head confirmation, then the output window (AI generation
   takes a while — expect `running…` for a bit, not a frozen window).
7. A `--mount` container offers none of these entries; a stopped one offers
   only `start`/`destroy` (plus `Open PR` when it has a PR).

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

- `bind=instance` is Incus's name; `bind=container` and `bind=guest` are both
  accepted as LXD aliases and work — the device was accepted and the
  instance's listener reached the host service exactly as with `instance` —
  but the daemon stores whichever string it was given, so reads must treat
  all three as the same thing.
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
  - **a port already taken on the side that must listen** → one of two strings,
    for the one same cause. Usually `Failed to listen on 127.0.0.1:5037:
    listen tcp 127.0.0.1:5037: bind: address already in use`; sometimes
    `Failed to receive fd from listener process: Failed to receive file
    descriptor via abstract unix socket`, when the forkproxy handshake is what
    notices first. Both were produced against one occupied port in a single
    session (2026-08-19), the first reproducibly, so **handling only one of
    them is a bug** — see `ports._LISTEN_FAILURE_MARKERS`.

    Neither string says *whose* port it is, and the address is no help:
    `Failed to listen on 127.0.0.1:5037` names the **listen** side, which for
    `bind=instance` is inside the instance. Only the forward's direction
    disambiguates, which is why `ports._translate` takes a `direction`. Reading
    that address as the host's is what made `to-container` answer a
    container-side collision with "Host port 5037 is already in use — pick
    another with `--host-port N`": advice that cannot help, since a
    `to-container` forward's host end is a *connect* target where a listener is
    exactly what is wanted.
  - missing protocol prefix → `Unknown protocol type "127.0.0.1"`
  - `udp:` listen with a `tcp:` connect → `Proxying from udp to non-udp
    protocol is not supported`
- An out-of-range port (`70000`) passes validation and fails only at device
  start, so range checking belongs on JailBee's side.
- IPv6 endpoints are stored byte-identically, brackets included. Adding a device
  with `listen='tcp:[::1]:5099' connect='tcp:127.0.0.1:5037' bind=instance` and
  reading back with `incus config device get` returned both values unchanged —
  no normalisation of the bracket form. This matters because `ports._props_differ`
  compares the rendered strings verbatim to detect drift; had Incus normalised,
  every `jailbee apply` would see permanent drift and replace the device on every
  run. The forward itself worked: `wget -qO- http://[::1]:5099/` from inside the
  instance reached the host-side service.

### What is not verified yet

Running JailBee itself inside a container was open until 2026-08-25, when the
[`.claude.json` relocation
findings](#findings-nested-rig-2026-08-25) answered half of it:
**`jailbee init` does work at nesting depth two.** It creates the shared-dir
tree, runs the shared-state migrations, and writes both the `<prefix>-base` and
`<prefix>-binds` profiles into the nested daemon — then stops at
`incus network get incusbr0` failing, because nothing created a bridge. A
subsequent `jailbee apply` fails at the missing `<prefix>-net-strict` profile.
Create the bridge first (`incus network create incusbr0`) and `jailbee init`
goes all the way: ACL, `jailbee-loose`, and both net profiles. `jailbee base
build` publishes a golden image at this depth too, and `jailbee new` produces a
working container — given the two prerequisites in [Prerequisites for a nested
`jailbee new`](#prerequisites-for-a-nested-jailbee-new). What still does not
work here is `jailbee apply`, which regenerates `<prefix>-base` with the
`dri-*` render-node devices that nesting rejects.

The `jailbee port` recipe below needs none of it — proxy devices are
per-instance and want no profile, bridge or ACL — but anything that reconciles
the *whole repo* does: against a daemon that was never `jailbee init`ed,
`jailbee doctor` fails every profile/ACL/network/shared-dir check with "run
`jailbee init`" (the checks above it — `incus binary`, `container_user`,
`upstream remote` — pass, so a red `doctor` here says nothing about the
install), and `jailbee apply` exits at `jailbee-registry-mirror container not
found` long before its port-forward loop. Verified 2026-08-19.

What is known about the rest:

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

## `jailbee port` against the nested rig (exercising the real commands)

The rig above talks to the nested daemon with raw `incus config device add`.
This recipe exercises **`jailbee port`** itself against the same daemon:
`Incus()` shells out to the `incus` CLI, and inside this container that CLI
resolves to the nested daemon, so `jailbee port to-container`/`ls`/`rm` are
exactly as real here as they are on the host.

Bring the nested daemon up as above (only `sudo systemctl start
incus.service` needs root — once that grant is in place, `incus` and
`jailbee` both reach the nested daemon as the `dev` user; if a shell
predates the grant, reopen `jailbee shell` rather than reaching for `sudo`
on the commands below). Launch the instance named to this repo's
`container_prefix` convention (`<container_prefix>-<name>`) so `jailbee`
can resolve it by its short name:

```bash
incus launch images:alpine/edge <prefix>-probe

# host side (this container, from jailbee's point of view): a service to reach
python3 -m http.server 5037 --bind 127.0.0.1 &

jailbee port to-container 5037 probe
# expect: "... connecting to 127.0.0.1:5037 inside the container now reaches
#          the host's 127.0.0.1:5037 (port-tc-tcp-5037)"
jailbee port ls probe
# expect: one row — HANDLE port-tc-tcp-5037, DIRECTION to-container, SOURCE ad-hoc

incus exec <prefix>-probe -- wget -qO- http://127.0.0.1:5037/
# expect: the host service's response (the http.server's directory listing)

jailbee port rm 5037 probe
jailbee port ls probe
# expect: No port forwards.
```

`jailbee port ls` **with no container argument** lists the whole repo, and that
path goes through `lifecycle.list_containers`, which selects on the
`<prefix>-base` *profile* — not on the name prefix. A bare `images:alpine/edge`
instance therefore shows nothing there however it is named, which is correct
rather than a bug. Give it an empty stand-in profile to exercise the repo-wide
path (and `jailbee ls`):

```bash
incus profile create <prefix>-base
incus profile add <prefix>-probe <prefix>-base
jailbee port ls          # expect: the probe's rows, titled "Port forwards for <prefix>"
```

The other direction, with real traffic — a service *inside* the instance
reachable on the host. This is the half `host_ports` cannot declare, so the
ad-hoc command is the only way to it:

```bash
# a service inside the instance
incus exec <prefix>-probe -- sh -c \
    'nohup sh -c "while true; do printf \"HTTP/1.0 200 OK\r\n\r\nOK\n\" | nc -l -p 8080 -s 127.0.0.1; done" >/dev/null 2>&1 &'

jailbee port to-host 8080 probe --host-port 18080
# expect: "... connecting to 127.0.0.1:18080 on the host now reaches
#          127.0.0.1:8080 inside the container (port-th-tcp-8080)"
curl -s http://127.0.0.1:18080/
# expect: OK

jailbee port to-host 8081 probe --host-port auto
# expect: an OS-chosen high port in the success line; nothing listens on
#         container 8081, and the device is still added — see the finding above
jailbee port rm port-th-tcp-8081 probe
```

Negative case — the container-side port is already taken, which is the
scenario JailBee's translation exists for. Note the host's own
`http.server` is still on 5037, so this is exactly the case that used to be
misdiagnosed as a host-port clash:

```bash
# Occupy port 5037 *inside* the instance first (its own listener, adb-style).
incus exec <prefix>-probe -- sh -c 'nc -l -p 5037 -s 127.0.0.1 >/dev/null 2>&1 &'

jailbee port to-container 5037 probe
# expect JailBee's translated message, e.g.:
#   "Could not open port 5037 inside <prefix>-probe — something is already
#    listening on port 5037 inside the container. Stop it, or forward to a
#    different container port."
# NOT "Host port 5037 is already in use ... pick another with --host-port N",
# and NOT either of Incus's two raw strings for this (see the finding above).

incus exec <prefix>-probe -- pkill nc
jailbee port to-container 5037 probe   # now succeeds
jailbee port rm 5037 probe
```

The mirror image, to keep the two directions honest: with the host's 5037 taken
and nothing listening inside the instance, `to-host` must blame the *host*.

```bash
jailbee port to-host 9000 probe --host-port 5037
# expect: "Host port 5037 is already in use on the host. Pick another with
#          `--host-port N`, or `--host-port auto`."
```

### The `host_ports` (declarative) half

`jailbee apply` is the shipped entry point for this, and it cannot run here (it
exits at the registry-mirror check — see "What is not verified yet" above). Its
port work is one prefetched `list_forwards` plus a `reconcile_config_ports` per
container, with `PortError` collected instead of raised, so driving that shape
directly covers everything the wrapper does not:

```bash
# Put entries in .jailbee/config.yaml first, e.g.
#   host_ports:
#     - name: adb
#       port: 5037
python3 - <<'PY'
from jailbee.config import load_config
from jailbee.incus import Incus
from jailbee.paths import find_repo_config
from jailbee.ports import PortError, list_forwards, reconcile_config_ports

cfg, incus = load_config(find_repo_config()), Incus()
names = ["<prefix>-probe", "<prefix>-probe2"]
prefetched = list_forwards(incus, names)
for name in names:
    try:
        r = reconcile_config_ports(cfg, incus, name, forwards=prefetched.get(name, []))
    except PortError as e:
        print(name, "FAILED:", e)      # apply collects this into port_failures
    else:
        print(name, "+", r.added, "~", r.replaced, "-", r.removed)
PY
```

Worth checking here, all confirmed 2026-08-19:

- a second run reports nothing changed — including for an entry with IPv6
  addresses, which is what the byte-identity finding above predicts
- editing an entry replaces its device, deleting one removes it, and ad-hoc
  (`port-tc-*`/`port-th-*`) plus hand-made devices are left alone throughout
- `jailbee port rm adb` resolves a `host_ports` entry by name, and the next
  reconcile puts it straight back
- one container refusing a device does not stop the sweep: the other still
  reconciles

Teardown:

```bash
kill %1   # stop the python http.server
incus delete <prefix>-probe --force
incus profile delete <prefix>-base
sudo systemctl stop incus.service
```

## `.claude.json` relocation smoke test

Claude Code's global config moved from a file-level bind mount
(`<shared_dir>/claude.json` → `~/.claude.json`) to living inside the existing
`claude` directory mount (`<shared_dir>/claude/.claude.json` → `~/.claude`),
via `CLAUDE_CONFIG_DIR=$HOME/.claude` exported from the golden image. Unit
tests cover the relocate/seed helpers
(`init_command._relocate_claude_json` / `_seed_claude_json`) and the
`install.sh` env export in isolation — they mock the filesystem and never run
a real Claude Code. Only a real login proves the *actual CLI* writes to the
new path. This is the gate; there is no other verification of that claim.

Requires `claude.enabled: true`, a real Incus daemon, and network access for
`jailbee base build` + Claude Code's own login flow. Run the checks in order
and write down each command's real output next to it — a plausible
sounding "should work" is not evidence, and several of these steps look
identical whether they passed or silently no-opped.

> **Mostly verified already — see [Findings](#findings-nested-rig-2026-08-25)
> at the end of this section.** On 2026-08-25 the whole pipeline (`init` →
> `base build` → `new`) was run at nesting depth two from inside a JailBee
> container, and Steps 1, 2, 4, 5 and the mechanism half of Step 6 all passed,
> alongside a direct probe of the real `claude` binary. What no rig can supply
> is a genuine Claude Code login, so "the CLI resumes rather than re-onboards"
> is still an inference from verified parts rather than an observation. Run the
> steps below on the host anyway if you want that last link.

### 1. Upgrade path on a repo that already has state

Start from a container built **before** this change, with a non-trivial
`<shared_dir>/claude.json` (i.e. Claude Code has been logged in at least
once). If convenient, leave a `claude` session running inside that old
container in a `jailbee tmux <name>` pane — reused by Step 5 below.

```bash
ls -la <shared_dir>/claude.json
# expect: a regular file, not a symlink; clearly bigger than the 3-byte
# "{}\n" seed (a real config, from an actual login)
sha256sum <shared_dir>/claude.json

jailbee base build      # rebuilds the golden image with the CLAUDE_CONFIG_DIR export
jailbee apply            # runs _relocate_claude_json then _seed_claude_json, updates <prefix>-binds

ls -la <shared_dir>/claude.json
# expect: "No such file or directory" — relocated, not copied
ls -la <shared_dir>/claude/.claude.json
sha256sum <shared_dir>/claude/.claude.json
# expect: identical hash to the sha256sum taken above, before `jailbee apply`

incus profile show <prefix>-binds | grep -c claude-json
# expect: 0 — the file-level device is gone from the profile
```

### 1b. Upgrade path with `jailbee new` *before* `jailbee apply`

The migration also runs from `jailbee new`, for a repo whose profiles are
still the pre-upgrade ones — otherwise the fresh container onboards into
`<shared_dir>/claude/.claude.json` and the real pre-move file is orphaned for
good. `jailbee new` therefore retires the `shared-claude-json` device from
`<prefix>-binds` before moving the file: without that the profile would keep
pointing at a source that no longer exists, and Incus refuses every
subsequent `start` / `profile assign` for the repo (nothing on this path
rewrites the profile again, so it would not self-heal).

Same starting point as Step 1 — a repo with a real `<shared_dir>/claude.json`
and profiles written by the previous release — but **skip `jailbee apply`**:

```bash
sha256sum <shared_dir>/claude.json
incus profile show <prefix>-binds | grep -c claude-json
# expect: 1 — the pre-upgrade profile still declares the device

jailbee base build
jailbee new upgrade-probe        # no `jailbee apply` in between

sha256sum <shared_dir>/claude/.claude.json
# expect: identical hash to the one above — moved, not re-seeded
incus profile show <prefix>-binds | grep -c claude-json
# expect: 0 — retired by `jailbee new`

# The point of the fix: a start after the move must still work.
jailbee stop upgrade-probe
jailbee start upgrade-probe
# expect: starts, not `Missing source path ... for disk "shared-claude-json"`
```

### 1c. `jailbee new` with neither `base build` nor `apply` run

The half that is easy to miss: retiring the device removes the only thing
that made `.claude.json` shared, so `jailbee new` must also put
`CLAUDE_CONFIG_DIR` on `<prefix>-base` — otherwise Claude Code resolves the
container-local `$HOME/.claude.json` and onboards from scratch in every
container, and in the repo's existing ones too (the profile rewrite drops the
device from their expanded config as well).

Same starting point as Step 1b, but skip **both** upgrade actions — this is
the state a user is in immediately after upgrading the tool:

```bash
incus profile show <prefix>-base | grep -c CLAUDE_CONFIG_DIR
# expect: 0 — the pre-upgrade base profile

jailbee new upgrade-probe2       # no `jailbee base build`, no `jailbee apply`

incus profile show <prefix>-base | grep CLAUDE_CONFIG_DIR
# expect: environment.CLAUDE_CONFIG_DIR: /home/dev/.claude

jailbee shell upgrade-probe2
echo $CLAUDE_CONFIG_DIR
# expect: /home/dev/.claude — the image predates the /etc/profile.d export,
# so this can only be the base profile `jailbee new` just repaired
ls -la ~/.claude/.claude.json
# expect: the relocated file, mounted from <shared_dir>/claude
claude
# expect: resumes the existing login — no onboarding, no /login prompt
exit
```

An already-migrated repo (the device retired by an earlier `jailbee new`,
before this repair existed) is healed by the same run: `jailbee new`, or
`jailbee apply`, whichever comes first.

### 2. Fresh repo

```bash
# in a repo whose <shared_dir> does not exist yet
jailbee init
cat <shared_dir>/claude/.claude.json
# expect: {}
ls <shared_dir>/claude.json
# expect: "No such file or directory" — never created
```

### 3. The variable actually relocates the file

In a container built from the image produced by Step 1's `jailbee base
build`:

```bash
jailbee new feat/claudereloc --no-clone --no-autostart
jailbee shell feat-claudereloc
echo $CLAUDE_CONFIG_DIR
# expect: /home/dev/.claude — from BOTH the base profile's
# `environment.CLAUDE_CONFIG_DIR` (every `incus exec`, this container's own
# path too) and `/etc/profile.d/jailbee-env.sh` (this is a login shell).
# Step 6 below isolates the profile source alone, on a container whose image
# predates the `/etc/profile.d` export.
claude
# complete onboarding (fresh shared state) or resume (if state was carried
# over), then exit the session
exit

ls -la <shared_dir>/claude/.claude.json
# expect: mtime just updated by the run above
ls <shared_dir>/claude.json 2>&1
# expect: "No such file or directory" — Claude Code never wrote back to the
# old path
```

### 4. State survives across containers of the repo

```bash
jailbee new feat/claudereloc2 --no-clone --no-autostart
jailbee shell feat-claudereloc2
claude
# expect: no onboarding prompt
/mcp
# expect: the same MCP servers configured from the Step 3 container, if any
exit
```

### 5. Live device removal

Uses the old container from Step 1 (`<prefix>-<old-name>` below), with its
`claude` session still running from before `jailbee apply` ran in that step.
Note this only proves the *already-running* session survives — it holds its
config in memory, so it looks fine whether or not a fresh start would work.
Step 6 below is the check that actually distinguishes a pass from a failure.

```bash
incus config show <prefix>-<old-name> --expanded | grep -A2 claude-json
# expect: no output — the device is gone from the running container's
# expanded config, not just from the profile source
```

In the pane where `claude` has been running since before Step 1's `jailbee
apply`, type something and confirm it still replies — expect it to still be
responsive, with no crash, no restart and no session drop.

### 6. Fresh `claude` in the old-image container, right after `jailbee apply`

Same old container as Step 5 (`<prefix>-<old-name>`) — its *image* was never
rebuilt, so it does not have the `/etc/profile.d/jailbee-env.sh` export. This
is the positive check finding 1's fix makes possible: `jailbee apply` already
put `CLAUDE_CONFIG_DIR` on the `<prefix>-base` profile, and Incus injects
`environment.*` into every `incus exec` regardless of image contents or shell
type — no rebuild, no re-create needed.

```bash
jailbee shell <old-name>
echo $CLAUDE_CONFIG_DIR
# expect: /home/dev/.claude — this container's image predates the
# /etc/profile.d export, so this value can only be coming from the
# <prefix>-base profile Step 1's `jailbee apply` just rewrote
claude
# expect: resumes the session relocated in Step 1 — no onboarding prompt.
# This is the check that would have failed before finding 1's fix: the old
# image's `claude` would have resolved $HOME/.claude.json (unmounted,
# container-local) and re-onboarded from scratch.
exit
```

Teardown:

```bash
jailbee destroy feat-claudereloc --force
jailbee destroy feat-claudereloc2 --force
```

### Findings (nested rig, 2026-08-25)

Run from inside a JailBee container against the nested Incus daemon
(`sudo systemctl start incus.service`, see [Nested Incus probe
rig](#nested-incus-probe-rig-verifying-device-behaviour-from-inside-a-container)),
plus one probe of the real `claude` binary that needs no daemon at all.

**The `CLAUDE_CONFIG_DIR` claim holds — Claude Code 2.1.245, real binary.**
Two runs of `claude -p "hi"` under `env -i` with an isolated `HOME`, so
neither could touch this container's own state:

| | Files created |
|---|---|
| no `CLAUDE_CONFIG_DIR` | `$HOME/.claude.json` and `$HOME/.claude/` |
| `CLAUDE_CONFIG_DIR=$HOME/.claude` | `$HOME/.claude/.claude.json`, and **no** `$HOME/.claude.json` |

Both runs exited at `Not logged in · Please run /login`, which is far enough:
the config file is written before the auth check. This is the claim the whole
change rests on, and it was previously supported only by reading claude-swap's
`paths.py`.

**The base-profile env var reaches non-login shells.** A profile carrying only
`environment.CLAUDE_CONFIG_DIR: /home/dev/.claude`, added to a plain
`images:alpine/edge` instance:

```
incus exec probe1 -- env | grep -i claude
# observed: CLAUDE_CONFIG_DIR=/home/dev/.claude
incus exec probe1 -- sh -c 'echo "[$CLAUDE_CONFIG_DIR]"'
# observed: [/home/dev/.claude]        <- the `gui.py` bash -c case
incus config set probe1 environment.CLAUDE_CONFIG_DIR=/custom/override
incus exec probe1 -- env | grep -i claude
# observed: CLAUDE_CONFIG_DIR=/custom/override   <- instance config wins
```

No shell profile is sourced by `incus exec`, so this is the direct evidence
that the export in `/etc/profile.d/jailbee-env.sh` is a fallback, not the
mechanism — an IDE launched under `bash -c` gets the variable anyway.

**Steps 1 and 2 pass end to end, against a real daemon.** `jailbee init` does
work at nesting depth two (this was listed as an open question under the
nested-rig section until now), far enough to write both profiles:

- A repo seeded with a legacy `<shared_dir>/claude.json` → `jailbee init`
  printed `✓ Moved …/claude.json → …/claude/.claude.json`, the destination's
  content was byte-identical to the source's, and the old path was gone.
- The stored `<prefix>-binds` profile contained exactly `shared-claude`,
  `shared-claude-install` and `shared-ssh` — **no `shared-claude-json`**.
- The stored `<prefix>-base` profile contained
  `environment.CLAUDE_CONFIG_DIR: /home/dev/.claude`.
- A fresh repo with no legacy file → `<shared_dir>/claude/.claude.json` seeded
  as `{}`, and no `claude.json` at the old path.

**The never-overwrite branch behaves and says so.** Re-creating a legacy
`claude.json` alongside an existing destination and re-running produced a
warning naming both paths and telling the user to merge by hand, and left
both files untouched. This is the branch that would silently orphan a user's
login if it ever moved a file over live state.

`jailbee init` stops after the profiles with `incus network get incusbr0`
failing — the nested daemon has no bridge — and `jailbee apply` then fails at
the missing `<prefix>-net-strict` profile. Neither touches the claude paths,
which run earlier; a nested rig simply cannot complete a repo's network setup.

**The whole pipeline runs at nesting depth two.** With `incus network create
incusbr0` done first, `jailbee init` completes — ACL, `jailbee-loose`, both net
profiles — and `jailbee base build` publishes a golden image (~870 MiB, a full
apt provision two levels deep). `jailbee new` then works, after two rig-only
prerequisites; see [Prerequisites for a nested `jailbee
new`](#prerequisites-for-a-nested-jailbee-new) below.

**Steps 4 and 6 pass in real containers built from a real golden image.** In a
`jailbee new` container:

```
incus exec <c> -- env | grep CLAUDE_CONFIG          # non-login: base profile
# observed: CLAUDE_CONFIG_DIR=/home/dev/.claude
incus exec <c> -- su - dev -c 'echo $CLAUDE_CONFIG_DIR'   # login: profile.d
# observed: /home/dev/.claude
incus exec <c> -- cat /home/dev/.claude/.claude.json
# observed: the migrated content, byte-identical
incus exec <c> -- ls -la /home/dev/.claude.json
# observed: No such file or directory
```

Both routes to the variable work independently — which is the point of having
two. Cross-container (Step 4): a second `jailbee new` container read the same
file, and a write from the first was immediately visible in the second.

**Step 5, and one artifact worth knowing about.** Hot-removing the file device
from the `<prefix>-binds` profile detaches the mount from the *running*
container and leaves it otherwise healthy, shared dir intact. But it leaves
behind a stub:

```
incus exec <c> -- ls -la /home/dev/.claude.json
# observed: ---------- 1 root root 0 ... /home/dev/.claude.json
```

A zero-byte, mode-000, root-owned file, and it persists. It is inert while
`CLAUDE_CONFIG_DIR` is set — nothing reads `$HOME/.claude.json` any more — but
it is a concrete reason the CHANGELOG tells users to re-create containers after
upgrading rather than only running `jailbee apply`.

`jailbee apply` itself cannot finish in the nested rig: it regenerates
`<prefix>-base` with the `dri-*` devices, which nesting rejects. The device
removal above was therefore done directly, which is the same operation apply's
profile rewrite performs.

#### Prerequisites for a nested `jailbee new`

Two host-container settings, both rig-only — neither is a JailBee bug:

1. **`root:<uid>:1` in `/etc/subuid` and `/etc/subgid`.** JailBee's `raw.idmap`
   identity-maps the host user's UID into the container so bind mounts stay
   readable. At depth two, root's subuid range does not cover that UID and the
   container refuses to start with `newuidmap: uid range [N-N+1) -> [N-N+1) not
   allowed`. Append the line for your own `id -u`, then restart `incus.service`.
2. **Remove the `dri-*` devices from `<prefix>-base`.** A nested container
   rejects them with `The "mode" property may not be set when adding a device
   to a nested container`. `profiles._host_render_nodes()` adds one per host
   render node unconditionally, so they come back on every `jailbee apply`.

**Still needs the host:** the one thing no rig can supply — a real Claude Code
login, proving the CLI resumes an existing session at the new path rather than
re-onboarding. Every mechanism that outcome depends on is verified above; only
the end-to-end observation is missing. **Do not** copy a login into a rig
container to fake it: two live copies of one credential rotate each other out,
which is the failure that removed host-seeding in the first place.

## Host gpg-agent survives a container boot smoke test

Needs a host with `gpg.enabled: true` and a live agent — ideally a smartcard,
since the failure mode is invisible without one. Before this fix, the *first*
`jb restart` was enough to kill the host's agent for the rest of the session.

```bash
# Host baseline — note the agent PID and, with a card, the keys it offers.
systemctl --user status gpg-agent.service | head -3
pgrep -a gpg-agent
ssh-add -l                            # cardno:... keys listed

jb restart <container>

# The device must be read-only. Without `readonly: "true"` here, the
# container can still unlink the host's sockets.
incus config device show <container> | grep -A4 'gpg-socket\|pulse-socket'

# The host's agent must be the *same process* as before the restart, and
# still offering the card's keys. A different PID (or none) means the
# container's socket units clobbered the sockets again.
pgrep -a gpg-agent
ssh-add -l
gpg-connect-agent 'SCD SERIALNO' /bye  # S SERIALNO D276...  OK

# Nothing may be listening on the shared sockets from inside the container.
incus exec <container> -- pgrep -a gpg-agent   # no output

# Forwarding still works through the read-only mount (needs a card touch).
jb exec <container> -- ssh -T git@github.com
jb exec <container> -- git -C ~/<repo> commit -S --allow-empty -m 'signing smoke'
```

After a `jb base build`, also confirm the masking half landed — the units
should report `masked`, and the container's user session should carry no
failed units:

```bash
incus exec <container> -- systemctl --user --machine=dev@ is-enabled \
    gpg-agent.socket dirmngr.socket pulseaudio.socket
incus exec <container> -- systemctl --user --machine=dev@ --failed
```

Audio is the other half of the same fix: with `pulse-socket` read-only,
`jb chrome` (or any GUI app) must still play sound from inside the container,
and the host's own audio must survive the container's boot.

## `jailbee claude` account pool smoke test

Needs two Claude accounts. Everything below runs on the host.

1. `jailbee claude ls` — the row for this repo's holder is `live` and bold,
   names the account in use, and its `GROUP` and `USED BY` cells match the
   repo's group and the repos/containers reading it. Every other credential
   group on the host is a row too; `STATE` is `empty` for one holding no
   login.
2. `jailbee claude park` — that row becomes `empty`, a new `parked` row
   appears, and `ls <holder>/.credentials.json` is gone.
3. In a container of that holder, run `claude` and `/login` as the **second**
   account. Back on the host, `jailbee claude ls` shows the holder `live`
   again with the new account, the first still `parked`.
4. **The hot-reload gate.** Leave an interactive `claude` running in a
   container. On the host, `jailbee claude use <first account>`. Ask the
   session a question **without restarting it**: it must answer. Then check
   `/status` — a lagging account name there is expected and harmless; a
   login prompt is not, and would mean the mtime hot-reload (spec §5.1) does
   not hold on this Claude Code version.
5. **The identity gate.** In each member repo's `<shared_dir>/claude/.claude.json`,
   `oauthAccount` must be absent right after the switch and repopulated with
   the *new* account's email after the next container run.
6. **The concurrency gate.** With two containers of *different* repos in one
   group both running Claude, switch on the host. Neither container may end
   up on the old account, and no `.oauth_refresh.lock` may be left behind
   (`ls -a <holder>`).
7. `jailbee claude rm <parked account>` — confirms, then the row is gone.
8. **The group-visibility gate** (what `claude ls` was rebuilt for). Move one
   container into a group no repo resolves to:
   `jailbee claude group use <fresh-group> <container>`, then on the host
   `jailbee claude ls`. That group must be a row of its own, `USED BY` must
   name **that container** (not a count, since no repo resolves to the
   group), and the row must be `empty` until a `/login` in the container or a
   `jailbee claude use -g <fresh-group>` fills it. `jailbee claude ls -g
   <fresh-group>` must narrow to that row plus the parked store, and
   `jailbee claude group` must point at `jailbee claude ls` rather than
   printing a list of group names.
9. **The degradation gate.** With the Incus daemon stopped
   (`sudo systemctl stop incus`), `jailbee claude ls` must still print the
   table, with `containers ?` in `USED BY` and a warning — not an error.

## Cache pool smoke test (`pool.py`, `pooled_caches`)

`src/jailbee/pool.py`'s unit tests mock `subprocess`/Incus throughout, except
`test_seed_really_hardlinks_link_paths_and_really_copies_the_rest` in
`tests/test_pool.py`, which runs a real `rsync` against a `tmp_path` fixture
(skipped if `rsync` isn't installed) and proves hardlinking in isolation.
None of that proves the three things the feature actually exists for: that
Gradle stops blocking on `Waiting to acquire ... lock` with two containers
up, that migrating a real multi-gigabyte cache into `slot-0` behaves, and
that Incus accepts the `<cache>-slot` disk device on a container that's
already running. Needs a real Incus daemon and a repo with `golden.stacks:
{java: true}` (or an equivalent `shared_caches`/`pooled_caches` setup).

### 1. Real hardlinks across slots

```bash
jb new hardlink-test
jb new hardlink-test-2
# Run (or repeat) a Gradle build in each container first, so both slots
# actually hold artifacts — a build against an empty cache has nothing to
# hardlink and this check would pass vacuously.

find <shared_dir>/caches/gradle/slots -name '*.jar' -links +1 | head
# expect: non-empty — a jar with link count > 1 is the same inode in more
# than one slot, i.e. actually shared rather than copied.

jb pool ls gradle
# expect: per-slot SIZE values that, summed, considerably overstate the
# real footprint (each slot recounts every hardlinked jar); the printed
# "total on disk (deduplicated)" footer is the number that matters — it
# counts each inode once (see `pool.unique_bytes`) and should be close to
# the size of one warm ~/.gradle, not N times that.
```

### 2. A second container's Gradle build no longer waits on the first

This is the actual bug this feature fixes — reproduce it once against a
repo predating the change (or with `pooled_caches: {gradle: false}`) before
confirming the fix, or a passing run proves nothing:

```bash
# Baseline (pooling off): with a real ~/.gradle shared mount, start a build
# with a long-held daemon/lock in one container...
jb exec repro-a -- bash -c 'cd <repo> && ./gradlew build --no-daemon &
                             sleep 2 && ./gradlew help' # or any two overlapping invocations
# ...and a concurrent build in a second container of the same repo.
jb exec repro-b -- ./gradlew build
# expect (pooling off): the second build's output includes
# "Waiting to acquire ... lock" and it stalls until the first releases it.

# Now with pooling on (the default) for the same repo. `jailbee apply` only
# creates the pool layout on disk — a pooled cache attaches when a container
# next boots, so restart both to actually pick up a slot:
jb apply
jb restart repro-a
jb restart repro-b
jb pool ls gradle
# expect: one slot per running container, no two containers sharing a slot

jb exec repro-a -- ./gradlew build &
jb exec repro-b -- ./gradlew build
wait
# expect: both complete without ever printing "Waiting to acquire ... lock" —
# each container's Gradle daemon holds a lock on its own private slot.
```

### 3. Slot-0 migration on a multi-gigabyte cache

Unit tests exercise `ensure_pool_dirs`'s migration against tiny synthetic
directories; they can't show that moving a real, multi-gigabyte `~/.gradle`
behaves — in particular that `shutil.move` (used because the pool root and
the destination slot are on the same filesystem) is a rename and not a
slow, space-doubling copy, and that nothing partially moves on failure.

```bash
# A repo with a real, multi-GB pre-pooling cache — either an existing repo
# that's been building for a while, or seed one:
du -sh <shared_dir>/caches/gradle       # note the size; should be several GB

jailbee apply
# expect: near-instant even for a multi-GB cache (same-filesystem rename,
# not a copy) — time it if the size is large enough to notice a copy.
# Console should print "Migrated the existing gradle cache into .../slot-0"

du -sh <shared_dir>/caches/gradle/slots/slot-0
# expect: same size as the pre-migration figure above — nothing lost

ls <shared_dir>/caches/gradle
# expect: only slots/, by-container/, .lock — no loose cache content left
# directly under the pool root

jailbee doctor
# expect: no "pool roots not migrated" line for gradle
```

### 4. Incus accepts the pool device on an already-running container

`boot_container` calls `allocate_startup` — which calls `incus config
device add` — **before** it issues the actual `incus restart`, so the add
happens against a container Incus still considers Running, not one that's
already stopped. This ordering is the one part of the mechanism no mock can
validate, since the unit tests fake the Incus client entirely.

```bash
# A container created before gradle pooling was turned on for this repo
# (or with pooled_caches: {gradle: false} at creation time), left running:
incus list <prefix>-live-attach-test --format csv -c s
# expect: RUNNING
jb pool ls gradle
# expect: no slot for live-attach-test yet

jb apply                    # picks up the config change, creates the pool layout
jb restart live-attach-test # boot_container: allocate_startup runs while
                             # the container is still Running, *then* restarts it

incus config device show <prefix>-live-attach-test | grep -A3 gradle-slot
# expect: a "disk" device named gradle-slot, source pointing at a real
# slots/slot-N directory — added without Incus rejecting the live device add

jb exec live-attach-test -- ls ~/.gradle
# expect: the slot's contents visible inside the container after the restart
```

### 5. What a Running container sees between `apply` and its restart — OPEN QUESTION

`jailbee apply` drops the pooled cache's bind mount from the binds profile
(pooled caches are attached per container instead) and, in the same run,
`ensure_pool_dirs` renames the old loose cache content into
`slots/slot-0`. Nothing in this checkout settles what a container that was
already Running at that moment then sees, because it depends on whether
Incus hot-unplugs a disk device dropped from a profile of a running
container. Both answers are bad, which is why `apply` tells the user to
restart rather than making a claim:

- if Incus **does** hot-unplug, the container loses `~/.gradle` immediately
  and a build running in it starts failing;
- if Incus **does not**, the container keeps a mount pointing at the pool
  *root*, whose contents were just moved into `slots/slot-0` — so a live
  build writes fresh loose content straight into the pool root, and the
  next `ensure_pool_dirs` refuses it as "both pool slots and loose cache
  content".

Record what you actually observe here.

```bash
# A container of a repo whose gradle cache is not yet pooled, left Running,
# with `pooled_caches` about to turn pooling on (or a pre-pooling install):
incus list <prefix>-apply-race-test --format csv -c s
# expect: RUNNING

jb apply                     # DECLINE the restart prompt when it asks
# expect: the "a pooled cache attaches when a container next boots" hint

incus config device show <prefix>-apply-race-test | grep shared-gradle
# record: is the old profile-level shared mount still on the container?
jb exec apply-race-test -- ls ~/.gradle
# record: contents, empty, or an error?

# Then check whether the pool root got polluted behind apply's back:
ls <shared_dir>/caches/gradle
# expect (if it did): loose entries alongside `slots`/`by-container`, and
# `jailbee doctor` reporting "pool roots not migrated: gradle"

jb restart apply-race-test   # the documented fix, either way
jb pool ls gradle
# expect: a slot allocated to apply-race-test
```
