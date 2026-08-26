---
name: jailbee-usage
description: Use when running or explaining day-to-day `jailbee` (`jb`) commands against an already-set-up repo — creating/entering/destroying branch containers, the host↔container git bridge (`jailbee git push`/`pull`/`fetch`/`checkout`/`diff`), network modes (`jailbee net strict|loose`), egress overrides (`jailbee net egress ls|add|rm|export`, short alias `jailbee egress`), port forwarding (`jailbee port ls`/`to-container`/`to-host`/`rm`), `jailbee dashboard`, snapshots, mounts, `jailbee ide`/`jailbee chrome`, background ops, reviewing PRs with `jailbee new --pr`, and opening/updating PRs with `jailbee pr`/`jailbee submodule pr`. Trigger on "how do I use jailbee", "jailbee new/shell/git/net/port/dashboard", "how do I use gie", "gie new/shell/git/net/port/dashboard" (`gie` was jailbee's pre-1.0 command name, removed in 1.1.0 — users may still say it out of habit), "spin up a container for this branch", "push/pull/merge the container branch", "switch the container to loose/strict", "allow this container to reach X", "add a host to the allowlist", "why can't the container reach X", "forward a port into/out of the container", "expose adb inside the container", "review this PR in a container", "open a PR for a submodule", "publish this submodule's commits as a PR", "luo kontti tälle branchille", "vie/tuo muutokset kontista", "välitä portti konttiin", "salli kontille pääsy hostiin", "lisää host sallittujen listalle", "avaa PR alimoduulille", "vie alimoduulin muutokset PR:ksi". For first-time repo configuration instead (writing `.jailbee/config.yaml`, `install.d/` snippets, golden-image tailoring) use the jailbee-repo-setup skill.
---

# Using JailBee day-to-day

`jailbee` (short form `jb`) runs many isolated dev environments in parallel
on one Linux host. Each environment is an **Incus system container** cloned from a
prebuilt "golden image", carrying its own backend, frontend, Docker daemon, IDE,
and browser — no port/name/schema collisions between branches.

`gie` is the pre-1.0 name of this tool. The `gie` command was **removed in
1.1.0** and no longer exists — always use `jailbee` (or `jb`). If a user types
`gie`, tell them the command is now `jailbee`; if a repo still keeps its config
in `.gie/`, that directory is still read (deprecated, removed in 2.0.0) and the
fix is `git mv .gie .jailbee`.

This skill is for **using** an already-configured repo (one that has
`.jailbee/config.yaml` and where `jailbee init` + `jailbee base build` have already run). If
the repo isn't set up yet, or you need to change `.jailbee/config.yaml`, use the
**jailbee-repo-setup** skill instead.

Goal: answer almost any "how do I do X with JailBee" question and run the right
command without falling back to `jailbee --help` for every step. The full flag-level
reference lives in [`references/commands.md`](references/commands.md) — read it
when you need an exact flag or an edge case this page doesn't cover.

## Mental model (read this first — it explains every command)

Four ideas make the whole tool make sense:

1. **One container per branch.** `jailbee new feat/foo` provisions a container,
   clones the repo into it, and runs the repo's autostart steps. Slashes in the
   branch become dashes in the **container name**: `feat/foo` → container
   `feat-foo`. Incus resources are prefixed per-repo (`<container_prefix>-feat-foo`),
   so two repos coexist; you can always pass the **short** name (`feat-foo`) and
   JailBee resolves it.

2. **Clone mode vs mount mode.** Default is *clone*: the host repo is
   `git clone --shared`'d into `~/<container_prefix>` inside the container (e.g.
   `~/SampleApp`), giving the container its own working tree isolated from the
   host. *Mount* mode (`jailbee new <name> --mount`) bind-mounts the host working
   tree RW into the container instead — host and container share one tree. The
   git bridge below works only in clone mode (mount mode shares the tree, so
   there's nothing to bridge).

3. **The git bridge: the container is a git remote.** Because the container has
   its own clone, commits move between host and container over a tiny bridge
   instead of GitHub. `jailbee git fetch/checkout/pull` pull commits *from* the
   container; `jailbee git push` sends commits *to* the container. Each container
   records the **base branch** it was forked from (`user.jailbee.base_branch`, set at
   `jailbee new` time) — pulls merge into that base by default, and `jailbee ls`/`jailbee git
   diff` measure "how far ahead" against it.

4. **Network modes are a safety boundary.** Containers run **strict** by default:
   a kernel egress allowlist blocks everything except what `.jailbee/config.yaml`
   lists. **`github.com` is deliberately NOT allowed in strict mode** — so an
   unattended agent can't surprise-push. To push/fetch/use `gh`, switch to
   **loose** (full NAT) for the operation, then it auto-reverts.

## The daily loop

```bash
jailbee new feat/foo            # create container off the default branch, autostart everything
jailbee ls                      # see what's running + each container's git status
jailbee shell feat-foo          # drop into a shell (lands in ~/<repo>, the clone)
#   ... work, commit inside the container ...
jailbee git pull feat-foo       # merge the container's branch back into its base (e.g. main)
jailbee destroy feat-foo --force
```

`jailbee dashboard` is the live, cross-repo version of `jailbee ls` — an
auto-refreshing TUI where you navigate containers and press Enter to act
(shell/ide/chrome/restart/stop/destroy, plus the workflow verbs — including
"Refresh from PR head" on a review container). Reach for it when juggling several
containers; reach for `jailbee ls` for a one-shot snapshot or scripting (`-o json`).
`jailbee gui` (== `jailbee dashboard --gui`) opens the same dashboard as a graphical Qt
window instead of a terminal TUI. Its **View** menu switches between a wide
Table layout and a width-adaptive Cards layout (the default on a fresh
install), and within Cards, between a denser **Compact** style and a
**Grid** style; per-repo card groups are collapsible (click the group
header). The chosen layout, card style, collapsed repo groups, table column
widths/order, and refresh cadence / paused state persist across sessions
(window size/position do not).

## Creating containers — `jailbee new`

`jailbee new` figures out what to clone from whether `<branch>` already exists
upstream and whether you pass a `<base>`:

| Invocation | Result |
|---|---|
| `jailbee new feat/x` | branch doesn't exist → clone default branch, `checkout -b feat/x`; base = default branch |
| `jailbee new feat/x` | branch exists → clone it as-is (review/test an existing branch); base = default branch |
| `jailbee new feat/x feat/base` | branch doesn't exist → fork `feat/x` off `feat/base` |
| `jailbee new feat/x feat/base` | branch exists → clone it as-is with **base** `feat/base` (asks first; `-y` skips) |
| `jailbee new feat/x --current` | same, with the host's currently checked-out branch as the base |
| `jailbee new --current` | use the host's current branch as the work branch |
| `jailbee new mybox --mount` | mount mode — positional is the **container name**, not a branch; host tree bind-mounted RW |
| `jailbee new --pr 1234` | review PR #1234 (fetches the PR head, see "Reviewing a PR") |

The `<base>` positional always names the container's **base branch** — the
anchor `jailbee ls` AHEAD/MERGE and `jailbee git pull` use. Forking off it is merely
what happens when the work branch does not exist yet. So the way to put an
existing branch on the right base is `jailbee new <existing-branch> <base>`; use
`jailbee git retarget` only to change a base after the fact.

Missing refs fail fast with the exact `git fetch` to run — JailBee does **not**
silently auto-fetch a base you don't have. A base that exists only as
`origin/<base>` is fine. By default it clones the *upstream* tip
(`origin/<default>`), not your possibly-stale local branch
(`new.clone_from`/`new.autofetch` control this).

In clone mode, `jailbee new` reads **only** the `autostart` section from the
target branch's committed `.jailbee/config.yaml` at the exact commit it clones —
every other config key still comes from your checkout. A deviation prints a
diff naming the ref or commit it read.

Privileges are checked separately, against the repo's reviewed baseline
(`refs/remotes/<upstream-remote>/<default_branch>`) rather than your
checkout — so a
checkout lagging the default branch is not treated as an escalation. A step
attaching an `optional_mounts` entry the baseline's same-named step doesn't
**always** asks for confirmation (defaulting to no; declining creates nothing).
A step widening network access from `strict` to `loose` asks only for an
untrusted head — a `--pr` whose head lives in a **fork**; an internal PR is a
branch in your own origin, so it warns and proceeds like any other branch.
`--yes`/`-y` accepts up front, on top of its existing job of skipping the
"branch already exists" prompt above. With `--background` the question is asked
*before* the run detaches, in your terminal; the answer is pinned to the commit
you were shown, and if the branch moves in between the worker aborts naming
that instead of provisioning an unseen config.

No committed branch config, or one that doesn't validate, falls back silently
(or with a warning) to your checkout's autostart. `--mount`/`--no-clone` are
unaffected, `--no-autostart` skips the read entirely (no steps run), and `jailbee
start`/`restart`/`apply` never read the branch — only creation does.
See [Configuration](../../config.md#where-does-the-autostart-config-come-from)
for the full diff format.

Useful flags: `--no-autostart` (skip the repo's autostart steps — fastest, lowest
risk), `--no-clone` (bare container, no repo), `--name` (override the derived
container name), `--memory`/`--cpu`/`--net` (one-off resource/network overrides),
`--background`/`-b` (provision detached, see below), `--tmux`/`--shell` (attach
to tmux / a shell once it's up; forces foreground).

Submodules come along automatically (offline) and round-trip through pull/push;
the host repo's submodules must be initialised first or `jailbee new` hard-fails with
the fix.

## The git bridge — moving commits host ↔ container

This is the subtlest part; get the direction right and everything else follows.
All of these refuse on **mount-mode** containers (they share the tree — just use
git on the host). Top-level aliases exist: `jailbee pull`/`push`/`diff` ==
`jailbee git pull`/`push`/`diff`. (There is no `jailbee git merge` — it was replaced by
`jailbee git pull`.)

**Container → host (pulling the container's work back):**

- `jailbee git fetch <name>` — fetch the container's branch into
  `refs/jailbee/<short>/<branch>` on the host. Pure transport; touches no working branch.
- `jailbee git checkout <name>` — fetch, then fast-forward (or create) the matching
  host branch. Refuses on divergence and points you at `jailbee git pull`.
  - `--as <branch>` — land it on a differently named host branch (the default is
    the container's branch, or its PR head when the container has one).
  - `-b <branch>` — read a different branch **from the container**; it never
    renames the host branch. Same meaning on `fetch`/`pull`/`push`.
- `jailbee git pull <name>` — fetch, then **merge the container's branch into its base
  branch** (`user.jailbee.base_branch`, e.g. `main`), creating a merge commit. This is
  the usual "I'm done, integrate it" command.
  - `--into <branch>` — merge into a different host branch instead of the base.
  - `--current` — merge into the host's currently checked-out branch instead of the
    base (mirror of `jailbee git push --current`); mutually exclusive with `--into`, and
    errors if the host is in detached HEAD.
  - `--ff` — fast-forward only; refuse if histories diverged.
  - `--checkout` — if the base branch isn't currently checked out, check it out,
    merge, then restore the original branch (refuses on a dirty host tree).
  - `--cleanup` / `--no-cleanup` — force or skip the post-merge destroy-container +
    delete-branch steps (otherwise governed by the `pull:` config block, which can
    `prompt`/`always`/`never` each step).
  - **No name + a TTY** → multi-select picker; pulls each selected container in
    order and stops at the first failure.

**Host → container (sending host commits in):**

- `jailbee git push <name>` — send a host branch into the container's clone. Source and
  action come from flags, from configured defaults (`push.default_source` /
  `push.default_action`), or are asked interactively when those are `ask`.
  - `--merge` / `--rebase` / `--plain` — after transport, merge/rebase the pushed
    ref into the container's branch, or just transport it (`--plain`). Refuses on a
    dirty container tree; conflicts leave the container mid-merge/rebase — resolve
    inside `jailbee shell <name>`.
  - `--from <branch>` (default: host default branch) / `--current` (host's current
    branch).
  - `--pr` (PR containers only) — re-fetch the PR head from GitHub first, pulling in
    commits the author pushed since the container was created.
  - `--from-local` / `--from-origin` / `--fetch`/`--no-fetch` — which *copy* of the
    source branch to send. By default JailBee fetches and pushes
    `refs/remotes/origin/<source>`, because a local `refs/heads/<base>` only
    advances on `git pull` and is therefore stale right after a plain
    `git fetch`. `--from-local` sends the host's local branch as-is (use it when
    the host has unpushed commits); `--current` always resolves locally, and
    `--pr` bypasses the choice entirely (it pushes `refs/jailbee/pr/<N>/head`, so
    `--from-local`/`--from-origin` are rejected with it).
    Configured by `push.push_from` / `push.autofetch`.
  - **No name + a TTY** → multi-select picker; source/action chosen once, applied to
    all, failures don't stop the batch (summary at the end).

**Inspecting the difference:**

- `jailbee git diff <name>` — by default the commits `jailbee git pull` would bring (3-dot
  diff against the base branch). `--wt` working-tree only, `--all` both, `--stat`
  for a summary.

`jailbee ls` surfaces the same picture per container without a diff: **BASE** (base
branch), **WT** (uncommitted changes), **AHEAD ±**/**↑** (commits ahead of base),
**MERGE** (`ok`/`conflict`/`?`/`—` — would the branch merge cleanly into base).
Stopped and mount-mode containers show `—` in the git columns.

Two more git-status columns exist but are **off by default** (opt in with
`--fields` or the `ls:` config block — see [Configuration](../../config.md#ls--dashboard--remembered-columns)):
**LOCAL ±** (`local_diff`) and **L↑** (`local_count`) — the diff between the
container's HEAD and the *host's currently checked-out branch*, as opposed to
AHEAD's pinned base. They show `?` when neither side happens to hold the
other's commit as an object — the probe never fetches or pushes to force an
answer out of a listing command, so `?` just means "run `jailbee git pull`
first," which puts the container's tip on the host and resolves it.

The destroy guard's "commits not on the host" check (below) depends on the
container's HEAD sha and whether any remote-tracking ref contains it.
Neither is a `jailbee ls` column, but both appear in the `git_status` field's
JSON payload (`jailbee ls -o json`, keys `head_sha` / `remote_contained`) for
scripting.

**Two more bridge commands:**

- `jailbee git retarget <name> <new-base> [--merge]` — re-point a container at a
  different base branch (rewrites `user.jailbee.base_branch`; `pull`/`push`/`ls`
  follow it). The stacked-PR tool: when a parent PR merges to `main`, retarget
  its dependent container from the parent branch onto `main`.
- `jailbee submodule checkout [<name>] [-b <branch>] [--submodules-only]` — put the
  tree on one branch, superproject and submodules, when they land on a detached
  HEAD after clone/push/pull. No name → the host repo; a name → that container.
  On the host, `-b <branch>` checks that branch out in the superproject first and
  then aligns the submodules to it, so jumping the whole tree back to `master` is
  one command; `--submodules-only` keeps the superproject where it is. A
  container's branch is its identity, so `-b` never switches it.

**Recipe — merging several containers through one.** Three features built in
parallel become one branch without resolving anything on the host, which is the
one place with no tests, no lint gate and no agent:

```bash
jailbee git checkout feat-a          # host HEAD → feat/a, taken from its container
jailbee git push feat-c --current    # feat/a into container c, merged into its branch
#   conflict? resolve inside container c, run the gates there, commit the merge
jailbee git checkout feat-b          # repeat per feature
jailbee git push feat-c --current
git checkout main
jailbee git pull feat-c --current    # all three land on main at once
```

`--current` is load-bearing: `push`'s default source is the container's *base*
branch, so without it you would send `main` into c. The action must be a merge
or rebase — `plain` transports the ref without applying it, so no conflict ever
appears. Containers a and b survive the last pull and are destroyed by hand.
Full version with the cleanup rules: [Git bridge](../../git-bridge.md#merging-several-containers-through-one).

## Network modes — `jailbee net`

```bash
jailbee net loose feat-foo             # full NAT — needed for git push / git fetch / gh
#   ... push or fetch over the network ...
# auto-reverts to the previous mode after ~5 min (loose_auto_revert)
jailbee net loose feat-foo --for 2h    # pick the TTL for this switch only
jailbee net loose feat-foo --no-revert # stay loose until switched manually
jailbee net strict feat-foo            # back to the egress allowlist now
```

`loose` auto-reverts to the previous mode after a TTL (default 5 min, see
`loose_auto_revert` config). Per switch, `--for <dur>` overrides that default
(`30s`, `45m`, `4h` — max 24h; `--for never` = `--no-revert`, and the two flags
are mutually exclusive). With neither flag JailBee **asks interactively** — only on
a TTY, with `JAILBEE_NONINTERACTIVE` unset and the policy enabled; anywhere else the
configured `after` applies silently. In a script, pass `--for` or `--no-revert`
rather than relying on either behaviour. A disabled policy schedules nothing and
never asks, but an explicit `--for` is still honoured.
`jailbee ls` shows the remaining TTL while any container is loose. The
**github.com strict gate is intentional** — don't add `github.com` to the strict
allowlist to "fix" a failing push; switch to loose for the op instead. `jailbee net
refresh` re-resolves the allowlist hostnames (useful after a CDN rotates IPs);
`jailbee net status` shows the refresh timer + per-repo pools.

## Egress overrides — `jailbee net egress`

For a host a container needs that isn't in `.jailbee/config.yaml`'s
`egress_allow`, and doesn't belong there (a one-off, not something the
whole team needs), add it without touching the config:

```bash
jailbee net egress add pypi.org feat-foo   # this container only
jailbee net egress add pypi.org --repo     # every container of this repo, this host
jailbee net egress ls feat-foo             # what applies, and where each entry came from
jailbee net egress rm pypi.org feat-foo    # undo it
```

Three facts that matter when explaining this:

- **Additive only.** An override can widen the strict-mode allowlist, never
  narrow it — `config.yaml` is always the floor. `rm` refuses an entry that
  exists only in `config.yaml`, pointing at the file instead.
- **Container scope is the default**, and dies with the container (stored
  in its own label). `--repo` is the wider, explicit opt-in: every
  container of the repo, on this host only.
- **Host-local, not committed** either way — never shared with the team,
  never seen by a reviewer or CI. That's also the risk: see
  [docs/security.md](../../security.md#egress-overrides) before suggesting
  one as a substitute for adding to `config.yaml`. If a host turns out to
  be needed permanently, `jailbee net egress export` prints a paste-over
  replacement for the config's `egress_allow:` key.

Also available as the short root alias `jailbee egress add|rm|ls|export`.
Full flag reference: [references/commands.md](references/commands.md#egress-overrides--jailbee-net-egress).

## Port forwarding — `jailbee port`

A forward is an Incus proxy device wired directly into (or out of) the
container's network namespace — it bypasses the network ACL by construction,
so it works identically in **both** `strict` and `loose`, and doesn't need
`net loose` to set up or to use. `jailbee net status` prints an active
forward's own section ("Port forwards: N on M container(s) — the network ACL
does not see these"), separate from the allowlist/TTL info above.

The single rule that makes every invocation unambiguous: **the positional
argument is always the container-side port**, in both verbs, and
`--host-port` always names the host side. There is no `HOST:CONTAINER` colon
syntax anywhere.

The verb names the side a service becomes **available** on — not which end
opens the TCP connection. Those are opposite ends of the same forward, in
both directions:

- `jailbee port to-container PORT [NAME] [--host-port N] [--proto tcp|udp]`
  — a **host** service becomes reachable **inside** the container. The
  container listens on PORT; Incus's proxy connects out to
  `--host-port`/host on the host. This is the adb case: the host runs the
  adb server on `127.0.0.1:5037`, `jailbee port to-container 5037 <name>`
  makes plain `adb devices` work inside the container, and the *container*
  is the one that opens the outward connection even though the command name
  says "to-container".
- `jailbee port to-host PORT [NAME] [--host-port N|auto] [--proto tcp|udp]`
  — the mirror: a **container** service becomes reachable **on the host**.
  The host listens (on PORT, unless `--host-port` says otherwise);
  `--host-port auto` asks Incus/the OS for a free host port and prints the
  one it picked. The *host* opens the connection inward, even though the
  command name says "to-host".
- `jailbee port ls [NAME]` — list forwards; with no NAME, every container of
  the repo. It lists **every** proxy device on the container, including one
  added by hand with plain `incus config device add` — that one shows up
  with source `other` rather than `config`/`ad-hoc`.
- `jailbee port rm HANDLE [NAME]` — HANDLE is a device name, a `host_ports`
  config entry's `name`, or a container-side port number (rejected as
  ambiguous if more than one forward uses that port).

Config-declared forwards (the `host_ports:` block in `.jailbee/config.yaml`
— see the jailbee-repo-setup skill) are always the to-container direction
and apply to every container of the repo. `jailbee port to-host` has no
config equivalent by design — it's per-container and ad hoc, because a host
listener is a machine-wide resource that containers of the same repo would
otherwise fight over.

## Other day-to-day commands

- **Shell / run:** `jailbee shell <name>` (interactive, lands in the clone),
  `jailbee tmux <name>` (attach the autostart tmux session), `jailbee exec <name> -- <cmd>`
  (one-off, e.g. `jailbee exec feat-foo -- pnpm test`). If `<name>` is omitted where a
  TTY exists, you get a picker.
- **Lifecycle:** `jailbee start|stop|restart <name>`; `start`/`restart` re-run
  autostart. `jailbee destroy <name> --force`, or `jailbee destroy --all` (whole repo,
  one confirmation), or `jailbee destroy` with no args for an interactive checkbox.
  Add `--background`/`-b` to detach. Before the usual confirmation, JailBee
  assesses what destroying would discard — a dirty working tree, a changed
  submodule, or commits that exist on neither the host nor a remote — and, if
  anything is at risk, shows a one-line summary per container and a second
  confirmation defaulting to **No**. Nothing fires it for work already pulled
  to the host or pushed anywhere. Three outcomes: something at risk → the
  summary and the second confirmation; a **running** container whose git
  status could not be read at all → treated the same way, with the reason
  "could not inspect the container" (unknown is never presented as safety);
  a container that was never probed to begin with — the normal state for a
  **stopped** one — just gets a note, no extra prompt (mount mode is exempt:
  its working tree is the host's and survives the destroy). An unmeasurable
  commit count (`AHEAD ↑` = `?`) still warns when the container's HEAD is on
  neither the host nor a remote-tracking ref. `--force` skips both
  confirmations and the assessment itself, on the single-name, `--all` and
  interactive-picker paths alike. `jailbee git pull`'s post-merge
  cleanup destroy runs the identical guard (its `always` cleanup policy is
  its own `--force`-equivalent bypass); the Qt dashboard (`jailbee gui`) shows
  the same summary in its own dialog instead, because its destroy runs as a
  detached, `--force`-appended background process that cannot answer a
  terminal prompt.
- **GUI:** `jailbee ide <name>` (JetBrains; `--app webstorm` to override), `jailbee chrome
  <name> [URL]`. Both require the matching `jetbrains`/`chrome` blocks enabled
  (usually in `~/.config/jailbee/global.yaml`). One JetBrains IDE runs at a time
  (shared profile); Chrome is per-container (`jailbee chrome-pool ls`/`prune`).
- **Snapshots:** `jailbee snapshot create <name> <tag>` / `restore <name> <tag>` /
  `ls` / `delete` — cheap save/rollback of a container's state.
- **Optional mounts:** `jailbee mount <kind> <name>` / `jailbee unmount <kind> <name>` to
  attach/detach an `optional_mounts` entry (e.g. `aws`) on a live container.
- **Housekeeping:** `jailbee disk-usage`, `jailbee prune` (stopped containers >30 days),
  `jailbee doctor` (host + repo diagnostics), `jailbee apply` (re-push config — profiles,
  ACL, /etc/hosts, dockerd proxy — after editing `.jailbee/config.yaml`; idempotent).
- **Background jobs:** `jailbee job ls [--all-repos]` (in-flight/failed jobs with
  phase, pid, age, error, log path), `jailbee job log <name> [--follow]` (print or
  follow the worker log), `jailbee job clear [<name>] [--all]` (acknowledge a dead
  job; refuses one whose worker is still alive). See "Background operations"
  below.

## Shell completion

`jailbee setup` installs the completion scripts for both `jailbee` and `jb`
(once per machine; restart the shell after). Beyond commands/flags, TAB
dynamically completes:

- container names, on every command that takes one
- branch names, on `jailbee new` and `jailbee retarget`
- snapshot tags, on `jailbee snapshot restore`/`delete` (not `create` — that tag
  doesn't exist yet)
- fixed values, for `--format`/`--layer`/`--attach`/`--user`

Needs `.jailbee/config.yaml` in the cwd, like every other command; elsewhere it
offers nothing rather than erroring.

## Background operations

`jailbee new` and `jailbee destroy` block until done (minutes for `new`). Add
`--background`/`-b` to detach and get the shell back immediately:

```bash
jailbee new feat/foo --background      # returns at once; track with `jailbee ls`
```

`jailbee ls` shows a **JOB** column with the live phase (`creating` → `cloning` →
`autostart`, or `destroying` / `failed`); it clears when the container is ready.
A failed background job leaves the container intact for inspection (`jailbee shell`,
then destroy). `jailbee shell`/`tmux` on an in-flight container **wait** for it to
finish, then attach. Make it the default with `new.background: true` /
`destroy.background: true` in config; `--no-background` forces one-off foreground.
`--attach shell`/`--attach tmux`, `--tmux`, `--shell` force foreground
(overriding `new.background`) and conflict with an explicit `--background`;
`--attach none` / `--no-attach` don't force foreground and combine fine with it.

A `failed` job is a database record, not a container state — the container
(if one exists) is left running untouched. `jailbee job ls` shows the recorded
error and worker log path; `jailbee job clear <name>` is how the record is
acknowledged (the dashboards expose the same action as "Clear failed job").

## Reviewing a pull request

```bash
jailbee new --pr 1234            # fetch PR #1234's head, create a container on it
jailbee shell <derived-name>     # review/test
jailbee git push <derived-name> --pr --rebase   # pull in commits the author pushed since
jailbee pr <derived-name>        # push your own commits to PR #1234's head (asks once)
jailbee destroy <derived-name> --force
```

Requires the `gh` CLI authenticated on the host (`gh auth login`). Fork PRs work
for *review*; `jailbee pr` refuses to publish to a fork PR's head. The PR number is
stored as `user.jailbee.pr` on the container.

Neither step needs `jailbee net loose`: `jailbee git push --pr` fetches the PR head on the
**host** and moves it in over the bridge, and `jailbee pr` publishes host-side too.
The first `jailbee pr` asks for confirmation and records it (`user.jailbee.pr_adopted`);
`--yes` skips the prompt for non-interactive use.

`git push --pr` needs the container named explicitly (it reads the container's
own `user.jailbee.pr` label, so there is nothing for a picker to offer), but the
action flag is optional: without it the merge/rebase/plain choice follows
`push.default_action`, which is `ask` by default — a prompt on a TTY, an error
off one. Both dashboards carry it as **"Refresh from PR head"**, shown only on a
review container (a PR JailBee opened from the container's own branch has its
head downstream of the container, so refreshing could only be a no-op).

`jailbee new --pr` never touches your branches: the head is fetched into JailBee's own
`refs/jailbee/pr/<N>/head` and the container's clone is checked out at that exact
commit. So reviewing your own PR works with its branch checked out on the host
(git refuses to fetch into a checked-out branch), and a stale or diverging local
branch of the same name cannot leak into the container.

`jailbee new --pr` fetches two things: the PR head, and the PR's base branch into
`origin/<baseRefName>`. The base fetch is what makes `jailbee ls` AHEAD (`±`/`↑`)
match GitHub's own diff — the container's base anchor is seeded from that ref,
and a stale tip predating the PR's branch point turns the three-dot diff's merge
base into that old commit, folding every base-branch commit made since into the
PR's numbers. `--no-fetch` skips both.

## Publishing a PR — `jailbee pr`

The "I'm done, ship it" companion to `jailbee git pull`. `jailbee pr <name>` publishes the
container's branch to GitHub and opens (or updates) a PR:

```bash
jailbee pr feat-foo            # first run: open a DRAFT PR; later runs: push new commits to it
jailbee pr feat-foo --ready    # open (or flip) it ready-for-review
jailbee pr feat-foo --web      # …and open it in the browser
```

Key point: it publishes **host-side** — JailBee fetches the container's branch to the
host, then fast-forward pushes it under the **host's** `gh` credentials. So,
unlike `jailbee git push --pr`, you do **not** need `jailbee net loose` on the container
for `jailbee pr`; you need `gh` authenticated on the host.

On a container created with `jailbee new --pr N`, `jailbee pr` does not open a second PR:
it asks once whether to push the container's commits to PR #N's head branch,
records the answer (`user.jailbee.pr_adopted`), and updates that PR from then on.
`--yes` skips the prompt and fork PRs are refused outright. Because the PR is
not JailBee's own, two extra guards apply on **every** run: `--force` asks again
before overwriting that head (`--yes` skips), and the interactive "regenerate
the description?" offer is suppressed, so the PR author's text is never replaced
unless you pass `--description`/`--title`/`--body`.

The same applies when the container was **not** made from a PR but its branch
already has one (`jailbee new <existing-branch>` on a branch you already opened a PR
for). `jailbee pr` checks GitHub for a PR on the container's branch before opening
anything and offers to push to it — `Push this container's commits to PR #77
instead of opening a new one? [Y/n]` — recording the same `pr` / `pr_branch` /
`pr_adopted` labels, and *not* `pr_author`, so the guards above stay on.
Declining exits without publishing; `--yes` skips the question. A closed/merged
PR or a fork PR falls through to opening a new PR (with a printed reason), and
`--as` skips the check entirely. Without it the AI-proposed head branch name
would publish the work under a new branch and open a duplicate PR.

`--as` is rejected (exit 2) on **any** container that already has a PR — its
head is fixed, so a different branch name would leave the PR untouched.

When `claude.enabled` and `claude.ai_pr_description` are on (both default), a new
PR's **title and body are written by the container's Claude CLI**, and
`claude.ai_pr_branch` proposes a convention-following head branch name (confirmed
interactively). Opt out with `--no-ai`, or override per field with
`--title`/`--body`/`--as`. Updating an existing PR leaves the description alone
unless you pass `--description` (regenerate with Claude), `--title`/`--body`, or
accept the prompt — which is offered only for a PR JailBee itself created.
`--force` force-pushes (with lease) a rebased/amended branch.

The generation reads the branch's commits and cumulative diff, plus
`.github/pull_request_template.md`, the spec or issue the branch implements, and
`CONTRIBUTING.md` / `CLAUDE.md` / `AGENTS.md`. It runs on `claude.ai_pr_model`
(default `sonnet`; `null` inherits the container's default model). A repo can
state its own PR conventions in `claude.pr_prompt` — those instructions outrank
JailBee's generic guidance about the title and body.

It is explicitly told **not** to run the project's tests, build, linters or
installers, and to describe how the change was tested from the commits and the
CI config instead — the run has a fixed budget while a test suite's cost belongs
to the repository. `claude.ai_pr_timeout` (default 600 s) bounds the whole run;
on expiry `jailbee pr` warns and falls back to a placeholder title/body, which
you can replace later with `jailbee pr --description`. Raise the timeout for a
large tree, or when `claude.pr_prompt` asks for slower work.

## Publishing a submodule PR — `jailbee submodule pr`

The counterpart of `jailbee pr` for work done **inside a submodule**. A
submodule is its own GitHub repository, so it needs its own PR — `jailbee pr`
only ever publishes the superproject branch. One PR per run; the two commands
don't depend on each other.

```bash
jailbee submodule pr feat-foo              # auto-target, draft PR
jailbee submodule pr feat-foo libs/foo     # explicit submodule (path is top-relative)
jailbee submodule pr feat-foo --ready      # mark ready for review
jailbee submodule pr feat-foo --open       # just open it in the browser
```

Without a path, the submodule with commits ahead of its own base is targeted
automatically; several ahead lists them and asks you to name one (two
submodules are two repositories and two PRs). None ahead is reported as a
plain fact, not an error.

The key thing to know: the signal is the submodule's **own** base anchor
(pinned when the container was created), not the superproject's gitlink diff
`jailbee ls` shows. So if you've committed inside the submodule but haven't
yet committed the gitlink bump in the superproject, `jailbee submodule pr`
still sees exactly the commits to publish — `jailbee ls`'s AHEAD column would
read zero for the same container. That gap is reported as information, never
an error.

Base and head branch names come from the submodule's **own** git data, not
the superproject's: base is `--base` > the submodule's `.gitmodules` entry >
its own `<remote>/HEAD` > `main`; head is `--as` > Claude's proposal > the
branch the commits came from. The chosen head is remembered per submodule
path, so re-running updates that PR instead of opening a second one. Note
that `--branch/-b` means something different here than in `jailbee pr`: it
selects which branch to read **from the submodule**, and is the escape hatch
for a detached submodule.

When the container also has a superproject PR, a successful run notes the
merge order as information only: merge the submodule PR first, so the
superproject PR's gitlink bump then points at a merged commit.

## Using `gh` / `git push` to GitHub from inside a container

`gh` is baked into every container. For it to authenticate, the `github` block
must be enabled in `~/.config/jailbee/global.yaml` (a per-repo token map keyed by
`container_prefix`). And remember the **strict gate**: even with a token, the
network call needs **loose** mode. So the in-container pattern is: `jailbee net loose
<name>` on the host → do the GitHub op inside → `jailbee net strict <name>`.

## Inside a JailBee container

You may be reading this from **inside** a JailBee container — the repo clone lives at
`~/<container_prefix>` and there is no `jailbee` binary or Incus daemon on `PATH`. If
so, you **cannot** run `jailbee ...` commands here; they all operate on the host.
What you can still do from inside:

- Work in the repo clone, commit, run tests/builds — exactly as on a normal dev box.
- Edit `.jailbee/config.yaml` and any `install.d/` snippets in the clone. Those
  changes only take effect when the **host operator** runs `jailbee apply` (live
  profile/ACL/`/etc/hosts`/dockerd changes) or recreates the container (image /
  install-time changes).
- For host-side bridge actions (`jailbee git pull`, `jailbee destroy`, `jailbee net loose`, …),
  describe what you need and let the host operator run it — you can't from here.

## Checking what an agent preset resolved to

`jailbee config show` prints the merged effective config, including the
resolved `agents:` block — preset fields filled in whether or not the
repo's own config mentions them:

```bash
jailbee config show | less   # look for the `agents:` block
```

That's the way to answer "what did enabling `codex`/`claude`/… actually
turn on" instead of re-deriving it from the preset source by hand.
Configuring a new agent, not just inspecting one already on, is the
**jailbee-repo-setup** skill's job.

## When to point elsewhere

- Changing what a container installs, its autostart steps, egress allowlist,
  resources, or any `.jailbee/config.yaml` field → **jailbee-repo-setup** skill.
- First-time host setup (`jailbee init`, UFW/subuid/keyring, `jailbee base build`) →
  the repo [`README.md`](../../../README.md) "Quick start".
- Exact flags / edge cases not on this page →
  [`references/commands.md`](references/commands.md).
