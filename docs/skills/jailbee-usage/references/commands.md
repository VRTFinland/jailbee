# JailBee command reference

Flag-level reference for every `jailbee` command. The SKILL.md body covers the
mental model and the common workflows; come here for an exact flag or an edge
case. This is a distillation of `jailbee <cmd> --help` plus the behaviour documented
in the repo's `CLAUDE.md` smoke-test recipes — the **code is the source of
truth**; if a flag here disagrees with `jailbee <cmd> --help`, trust the CLI.

Common conventions:

- **`<name>`** is a container name. The **short** form (`feat-foo`) resolves to
  `<container_prefix>-feat-foo` automatically; full names from other repos also
  work. Omitting `<name>` where a TTY exists opens an interactive picker for most
  commands.
- **`-c` / `--config <path>`** overrides the `.jailbee/config.yaml` location on nearly
  every command (omitted from the tables below).
- Container names derive from branches by replacing `/` with `-`
  (`feat/foo` → `feat-foo`).

## Table of contents

- [Setup & host (`init`, `apply`, `doctor`, `base`, `registry`, `net install`)](#setup--host)
- [Config (`config show|validate|init`)](#config)
- [Create & lifecycle (`new`, `start`, `stop`, `restart`, `destroy`)](#create--lifecycle)
- [Inspect (`ls`, `dashboard`, `job`, `disk-usage`, `prune`)](#inspect)
- [Enter & run (`shell`, `tmux`, `exec`)](#enter--run)
- [Git bridge (`git fetch|checkout|pull|push|diff|retarget`)](#git-bridge)
- [PR publishing (`pr`)](#pr-publishing)
- [Submodules (`submodule checkout`)](#submodules)
- [Network (`net strict|loose|refresh|status|unregister|install`)](#network)
- [GUI (`ide`, `chrome`, `chrome-pool`)](#gui)
- [Mounts (`mount`, `unmount`)](#mounts)
- [Snapshots (`snapshot create|restore|ls|delete`)](#snapshots)

## Setup & host

These mutate host-level Incus state or take minutes — don't run them
speculatively.

| Command | What it does |
|---|---|
| `jailbee init` | First-time setup: create per-repo Incus profiles, egress ACL, the `jailbee-loose` bridge, shared dirs; install the `jailbee-net-refresh` user timer. Errors if profiles already exist. |
| `jailbee apply [-y] [--no-restart]` | Re-push current config (profiles, ACL, `/etc/hosts`, dockerd proxy) to running containers. Replaces the old `jailbee init --reapply` / `jailbee net refresh`. Idempotent; prompts to restart containers if profiles changed (`-y` to skip prompt, `--no-restart` to never restart). |
| `jailbee base build` | Build the golden image from `install.d/` snippets. 10–15 min, one-time (re-run after changing `golden.*` or snippets). |
| `jailbee base prune [--all] [--days N] [--yes-to-all]` | Remove superseded dated golden-image archives (`<alias>-YYYY-MM-DD`). Lists all candidates up front and confirms once — a single batch confirmation, not per-archive — printing the total count + size before prompting. The live base image is always kept; archives currently in use by a container are skipped (batch continues, exit 0). `--all` prunes archives for every registered repo, not just the current one. `--days N` limits removal to archives older than N days (omitted: every dated archive is a candidate). `--yes-to-all` skips the confirmation prompt entirely. |
| `jailbee base usage [--all]` | Show disk usage of golden base images: each live base image and dated archive with its size, a per-repo subtotal, a prunable figure (archives only, i.e. what `jailbee base prune` would reclaim), and a grand total across images shown. `--all` includes every registered repo, not just the current one. |
| `jailbee doctor` | Host-level diagnostics: Incus running, bridges, subuid/subgid mapping, keyring limits, registry mirror, GitHub token perms. Read-only. |
| `jailbee registry up [--recreate]\|down\|status` | Control the Incus-hosted Docker registry mirror (rpardini proxy; caches all upstreams). `up` is idempotent and self-repairing: if an earlier provisioning run died partway (a network drop during `apt-get install`), it reinstalls the proxy rather than failing forever. `--recreate` deletes and rebuilds the container for damage reinstalling can't fix; the host-side cache and CA survive. `status`: `running`/`stopped`/`degraded`/`missing`. |
| `jailbee net install` | (Re)install the `jailbee-net-refresh` user systemd timer + service. Idempotent; safe to re-run. |
| `jailbee version` / `jailbee --version` | Print the version. |

## Config

| Command | What it does |
|---|---|
| `jailbee config init [--global]` | Write a fully-commented `.jailbee/config.yaml` (or `~/.config/jailbee/global.yaml` with `--global`). The template comments ARE the schema docs. |
| `jailbee config show` | Print the merged effective config (global + per-repo) as YAML. Use to check active `push.default_*`, `new.background`, etc. |
| `jailbee config validate` | Schema + cross-field + runtime-path checks. Fail-closed: unknown keys, bad `container_prefix`, malformed `egress_allow`, reserved `provision_env` keys, duplicate autostart step names, and unknown `optional_mounts` references are rejected. |

## Create & lifecycle

### `jailbee new [NAME] [BASE]`

`BASE` names the container's **base branch** (`user.jailbee.base_branch` + the
`refs/jailbee/base/<base>` anchor that `jailbee ls` AHEAD/MERGE and `jailbee git pull` use).
`NAME` is forked off it only when `NAME` does not exist in the source repo; when
it does, that branch is cloned as-is and `BASE` is just the anchor — JailBee prints
`Branch 'X' already exists in source repo.` and asks `Use existing branch 'X'
with base 'BASE'?` (`--yes`/`-y` skips, declining exits 0). `BASE` may exist only
as `refs/remotes/origin/<base>`; a missing one errors with the exact `git fetch`.
Without `BASE` the base is the repo's default branch. To change the base later,
use `jailbee git retarget`.

| Flag | Effect |
|---|---|
| `--current` | Use the host's currently checked-out branch. Alone → it's the work branch; with a `NAME` positional → it's the `BASE`. Mutually exclusive with the `BASE` positional; errors on detached HEAD. |
| `--pr <N>` | Fetch GitHub PR #N's head into the source repo as `refs/jailbee/pr/<N>/head` (never a branch, so a checked-out or diverging branch of the head's name is irrelevant) and check the container's clone out at that exact commit. Also refreshes `origin/<baseRefName>` so the container's base anchor — and therefore `jailbee ls` AHEAD — matches GitHub's diff. Stores `user.jailbee.pr`. Needs `gh` authed. `--no-fetch` uses the copy already on the host (`refs/jailbee/pr/<N>/head`, else a local branch of the head's name) and skips both fetches. |
| `--mount` / `-m` | Mount mode: bind-mount the host repo RW instead of cloning. The positional becomes the **container name** (no slashes). Incompatible with `--base`, `--current`, `--pr`. The git bridge refuses on mount-mode containers. Autostart always comes from your checkout in this mode — there is no distinct target branch to read. |
| `--name <n>` | Override the derived container name. |
| `--net <mode>` | Initial network mode for this container (`strict`/`loose`). |
| `--memory <m>` / `--cpu <n>` | One-off resource overrides (else `defaults.memory`/`defaults.cpu`). |
| `--from-base <alias>` | Clone from a non-default golden image alias. |
| `--no-clone` | Bare container, no repo clone (`jailbee shell` then falls back to `$HOME`). Same as `--mount`: no target branch, so autostart comes from your checkout. |
| `--no-autostart` | Skip the repo's autostart steps — fastest, least-risk way to get a container. Also skips reading the target branch's autostart config (see below): nothing runs, so there is nothing to diff or confirm. |
| `--yes` / `-y` | Skip the "branch already exists" confirmation above, and accept a target branch's autostart config that widens network access or attaches a host mount (see below) without asking. Required when there is no TTY. |
| `--background` / `-b` | Provision detached; return immediately. Track via `jailbee ls` JOB column, or `jailbee job ls`. Overrides `new.background`. `--no-background` forces foreground. Can't combine with `--attach shell`/`--attach tmux`, `--tmux`, or `--shell`, which force foreground on their own; `--attach none` / `--no-attach` are fine to combine. The ref resolution (incl. autofetch) and the branch-autostart check run **in the foreground first**, so a confirmation is asked in your terminal rather than declined by the stdin-less worker; declining creates nothing. With no TTY, pass `--yes`. |
| `--attach <mode>` / `--tmux` / `--shell` / `--no-attach` | What to do once the container is up: `--attach shell\|tmux\|none`, or the `--tmux` / `--shell` shorthands. Overrides the `after_new` config key. All four are mutually exclusive. `--attach shell`/`--attach tmux` (and the `--tmux`/`--shell` shorthands) also force foreground provisioning, so `--tmux` works in a repo with `new.background: true` without adding `--no-background`; `--attach none` / `--no-attach` don't need to, since there's nothing to attach to. `--tmux` lands in the autostart tmux session (created on demand, and focused on the `claude` window when `claude.autostart` is set). |

Clone source: defaults to the **upstream** tip (`origin/<default>`), controlled by
`new.clone_from` (`origin`|`local`) and `new.autofetch`. Submodules are brought
along automatically unless `new.submodules: false`; the host's submodules must be
initialised first. If `.local/` exists at the repo root it's bind-mounted RW into
the clone at `~/<prefix>/.local` (a host⇄container scratch channel, git-excluded);
disable with `share_local: false`.

**Autostart config source.** In clone mode, `jailbee new` reads the `autostart`
section from the target branch's committed `.jailbee/config.yaml` at the exact
commit it clones — every other key (mounts, network defaults, resource
limits, `container_prefix`, host-level keys) still comes from your checkout.
A deviation from your checkout prints a compact diff naming the ref or commit
it read (`+`/`-`/`~` for added/removed/changed steps, `!` for
`step_timeout`/`env` changes — step names are trigger-qualified, e.g.
`on_create[build]` vs `on_start[build]`).

Privileges are a **separate** comparison, against the repo's reviewed baseline
`refs/remotes/<upstream-remote>/<default_branch>` rather than your checkout (a checkout
lagging the default branch grants nothing, and used to be treated as an
escalation); it prints its own `branch autostart widens privileges beyond …`
block with `⚠ network access 'loose' in: …` / `⚠ attaches host mount(s): …`
lines, and falls back to comparing against your checkout when that ref has no
usable config. A step attaching an `optional_mounts` entry (typically a
credential directory like `~/.aws`) the baseline's same-named step does not
**always** asks for confirmation before anything is created (default no;
`--yes` accepts) — attaching the mount is what creates the asset. A step
widening network access from `strict` to `loose` asks only for an untrusted
head: a `--pr` whose head lives in a **fork**. An internal PR is a branch in
your own origin (same content, same push rights), so it warns and proceeds like
any other branch — `strict` is no boundary against code the container already
runs. A new step the baseline has no counterpart for counts as widening in both.

No committed branch config, or one that fails to validate or references an
`optional_mounts` key your host config doesn't define, falls back silently or
with a warning to your checkout's autostart. `--no-autostart` skips the branch
config entirely — no steps run, so there is nothing to diff or confirm.
`jailbee start`/`restart`/`apply` never read the branch — only `jailbee new` does.
See [Configuration](../../../config.md#where-does-the-autostart-config-come-from).

### `jailbee start|stop|restart [NAME]`

`start` and `restart` re-run autostart (`on_start` steps). `stop` halts. All
accept a picker when `NAME` is omitted with a TTY.

### `jailbee destroy [NAME]`

| Flag | Effect |
|---|---|
| (no args) | Interactive checkbox of this repo's containers (TTY required). |
| `--all` | Destroy every container in this repo (one confirmation, or none with `--force`). Mutually exclusive with a `NAME`. |
| `--force` | Skip confirmation. |
| `--background` / `-b` | Detached destroy; track via `jailbee ls`. Overrides `destroy.background`. `--no-background` forces foreground. |

A container mid-background-destroy refuses attach (`jailbee shell` reports it's being
destroyed).

**The destroy guard.** Before the confirmation above, JailBee assesses what the
destroy would discard: a dirty working tree, a changed submodule, or commits
that exist on neither the host nor a remote-tracking ref (`remote_contained
is not True` counts as at risk — unknown is not safe). Nothing here fires
for work already pulled to the host (`jailbee git pull`) or pushed to a remote
(`jailbee git push`, `jailbee pr`, a plain push from inside). Three outcomes:

- **Something at risk** → a one-line summary per container (e.g. `feat-foo:
  working tree +3 -1 · 2 commits not on the host`) and a second confirmation,
  `Destroying loses this. Continue?`, defaulting to **No**. A submodule is
  named by which of its signals is set — `submodule sub/bar (added)`,
  `(removed)`, `(committed +40 -2)`, `(uncommitted +1 -0)`, or a combination —
  never as a bare `+0 -0`. When the commit count itself is unmeasurable
  (`AHEAD ↑` shows `?`, e.g. a PR-review container whose base ref never made
  it into the clone), the commit check still runs off the container's HEAD and
  reads `commits not on the host (count unknown)`; a container sitting on a
  commit the host or a remote-tracking ref already holds stays silent.
- **A running container whose git status could not be read at all** (every
  probed field came back unmeasured — an `incus exec` failure/timeout) →
  the same treatment, with the reason `could not inspect the container`.
- **Never probed** (`git_status is None` — the normal state for a **stopped**
  container) → a `git status unknown for: …` note, but no extra
  confirmation; the guard never reads silence as safety, but it also never
  invents a risk it has no evidence for. **Mount-mode containers are
  excluded**: their working tree *is* the host directory, bind-mounted in, so
  it survives the destroy and there is nothing unknown to report.

`--force` skips both the ordinary confirmation and the guard's assessment
entirely, on the single-name, `--all` **and** interactive-picker paths. On
the single-name and `--all` paths the underlying git-status probe is
skipped along with it — nothing downstream would use it — so `--force`
there means no `incus exec` at all. The picker is the one exception: its
checkbox rows render the git columns regardless of `--force`, so that probe
still runs — it serves the listing, not just the guard. `jailbee git pull`'s
post-merge cleanup destroy (`pull:
destroy_container: prompt`) runs the identical guard; setting that policy to
`always` is this call's own `--force` equivalent and bypasses it the same
way. The Qt dashboard (`jailbee gui`) shows the same summary — and the same
unknown-status sentence — in its own dialog instead of the CLI's: it launches
destroy as a detached, `--force`-appended background process, so the CLI's
prompt (and its guard) never run there — the dialog is the *only* guard in
that path.

## Inspect

### `jailbee ls`

| Flag | Effect |
|---|---|
| `--all` | Containers from every jailbee-managed repo (adds a REPO column). Default: cwd repo only. |
| `-o` / `--format <fmt>` | `table` (default) or `json`. |
| `--fields <list>` | Comma-separated columns. Allowed: `name, full_name, repo, mode, base, state, created, job, network, ttl, loose_until, ip, memory_limit, mem, wt, ahead_diff, ahead_count, conflict, local_diff, local_count, git_status, pr`. Wins outright over the `ls:` config block, and applies to every `--format`. |

Git-status columns: **BASE** (base branch), **WT** (uncommitted: `+adds -dels`),
**AHEAD ±** / **↑** (commits ahead of base, 3-dot/"PR view"), **MERGE**
(`ok`/`conflict`/`?`/`—`). The **JOB** column shows in-flight and failed
background-job phases (`jailbee new`/`jailbee destroy --background`); see `jailbee job`
below to inspect or clear one. **TTL** appears only while a container is in
loose mode. Stopped/mount-mode containers show `—` in the four git columns.

The default table is NAME, BASE, STATE, CREATED, NETWORK and the four git
columns. **IP** and **MEM** are *not* in it — they are on by default in the
dashboards instead, where the view refreshes and a live number earns its
width; reach them from `ls` with `--fields ip,mem`. **MODE** is dynamic like
JOB, TTL and PR: it appears only once a mount-mode container exists, since
on a clone-only host every row would read `clone`.

Two more git-status columns exist, **off by default**: **LOCAL ±**
(`local_diff`) and **L↑** (`local_count`) — the diff/commit-count between
the container's HEAD and the *host's currently checked-out branch*, as
opposed to `AHEAD ±`/`↑`'s comparison against the container's pinned base.
Opt in with `--fields` or the `ls:` config block. Either can show `?`: the
comparison needs one side to already hold the other's commit as an object
in the same repository, and JailBee never fetches or pushes to force an answer
out of a listing command — `?` means neither side happened to have the
other's tip; `jailbee git pull <name>` puts the container's tip on the host and
resolves it. `head_sha` and `remote_contained` are not columns, but appear
in the `git_status` field's JSON payload (`-o json`) for scripting — they
also feed the destroy guard's "commits not on the host" check (see `jailbee
destroy` above).

The two pairs count submodules differently, which is worth knowing when they
sit side by side. `AHEAD ±` excludes gitlink lines
(`--ignore-submodules=all`) and instead folds in each submodule's *own*
committed diff, so a pointer bump contributes the submodule's real content
delta. `LOCAL ±` uses `--ignore-submodules=dirty` with no per-submodule pass
(as does its host-side fallback), so the same bump contributes only the
gitlink's `+1 -1`. The two columns therefore report different numbers for
the same commit — intended, not a bug.

The column set for the table is configurable per repo or globally (`ls:` in
`~/.config/jailbee/global.yaml` / `.jailbee/config.yaml`) — see [Configuration:
`ls:`/`dashboard:`](../../../config.md#ls--dashboard--remembered-columns).
It narrows the **table** only: `-o json` keeps its own built-in field set
regardless of `ls.fields`/`ls.hide` (an explicit `--fields` flag still
narrows JSON too). A repo block overrides the global one field by field
(`hide` alone in the repo keeps the global `fields`); a `hide` list
*replaces* rather than extends, and the Qt dashboard's Compact card style
ignores `fields` entirely.

### `jailbee dashboard`

Live, auto-refreshing TUI of all JailBee containers across registered repos + the cwd
repo, grouped by repo. Keys: `↑/↓` or `j/k` move (spans repos, skips headers),
`Enter` action menu (tmux/shell/ide/chrome/restart/stop/destroy for Running;
start/destroy for Stopped), `r` force refresh, `q`/`Ctrl-C` quit. Two-tier
refresh: base state ~3s, git columns ~10s — tune with `-i` / `--git-interval`, or
`--no-git` to drop git columns. Requires a TTY. Orphan containers (jailbee-managed but
repo not registered) show view-only.

`jailbee dashboard --gui` (alias: `jailbee gui`) launches a **graphical Qt** dashboard
instead of the terminal TUI; it detaches to the background by default (`--foreground`
keeps it bound to the terminal). Same `-i` / `--git-interval` / `--no-git` knobs.

The Qt GUI has a **View** menu to switch between a wide **Table** layout and
a width-adaptive **Cards** layout (cards re-wrap to fill the window). Within
Cards, the same menu picks a card style — **Compact** (default; hides clean
git rows) or **Grid**. Each repo's card group has a header that can be
clicked to collapse/expand it. It persists, between sessions, in the SQLite
state DB: the chosen layout, card style, collapsed repo groups, the table's
column widths/order, and the refresh cadence / paused state — but never the
window size or position. `-i`/`--interval` precedence at startup: explicit
flag > persisted value > 3s default. `--git-interval` is not persisted.
Fresh installs default to the Cards layout.

### `jailbee job`

| Command | Notes |
|---|---|
| `jailbee job ls [--all-repos] [-o json] [--fields …]` | List in-flight and failed background jobs with phase, pid, age, error and log path |
| `jailbee job log <name> [--follow]` | Print (or follow) the worker log of a background job |
| `jailbee job clear [<name>] [--all]` | Acknowledge a dead background job — clears the `failed`/stale record without touching the container. Refuses a job whose worker is still alive |

A `failed` job is a database record, not a container state: the container
(if one was created) is left running untouched. `jailbee job clear` is how you
acknowledge it — nothing else drops the record short of `jailbee destroy`.

### `jailbee disk-usage` / `jailbee prune`

`disk-usage`: breakdown by component. Golden-image sizes come from the Incus
API; container/snapshot sizes live on root-only storage-pool paths, so they
show `n/a` unless you run `sudo jailbee disk-usage`. `prune`: interactively delete
stopped containers older than 30 days (`--yes-to-all` to skip prompts).

## Enter & run

| Command | Notes |
|---|---|
| `jailbee shell [NAME]` | Interactive shell, lands in `~/<container_prefix>` (the clone); falls back to `$HOME` if there's no clone. Waits if the container is being created in the background. |
| `jailbee tmux [NAME]` | Attach the autostart tmux session (where `background: true` steps run). |
| `jailbee exec NAME CMD...` | Run a command as the dev user. `jailbee exec feat-foo -- pnpm test`. `--cwd home` runs from `$HOME` instead of the clone. Preserves `container.env` (routes via `incus exec`, not sudo). |

## Git bridge

All refuse on mount-mode containers. `jailbee pull`/`push`/`diff` are top-level
aliases for the `jailbee git` forms. There is **no `jailbee git merge`** — superseded by
`jailbee git pull`. With exactly one eligible container and no NAME given, `push` /
`pull` / `checkout` print a plan block (both branches, both tips, the action)
and ask `[Y/n]` before doing anything, so JailBee choosing the container silently
never means the direction is a surprise (`confirm.auto_target`).

### Container → host

| Command | Behaviour |
|---|---|
| `jailbee git fetch [NAME] [-b BRANCH]` | Fetch the container's branch into `refs/jailbee/<short>/<branch>`. Container must be running. Pure transport. Picker if no NAME. |
| `jailbee git checkout [NAME] [-b BRANCH] [--as NAME] [--confirm\|--no-confirm]` | Fetch + fast-forward (or create) the matching host branch. Refuses on divergence → use `jailbee git pull`. `-b` = which branch to read **from the container**; `--as` = the branch written **on the host** (default: the container branch, or `user.jailbee.pr_branch` when set, which `--as` outranks). With one eligible container and no NAME, shows the plan-and-confirm block first (`confirm.auto_target`, default true); `--no-confirm` skips it. Off a TTY the block prints and nothing is asked. |

On every container → host command `-b BRANCH` names a branch **inside the
container**. A branch the container doesn't have is rejected before the fetch,
listing the container's actual branch names — it does not create anything.

### `jailbee git pull [NAME]`

Fetch + **merge the container's branch into its base branch**
(`user.jailbee.base_branch`), default a `--no-ff` merge commit.

| Flag | Effect |
|---|---|
| `-b` / `--branch <b>` | Override branch detection. |
| `--ff` | Fast-forward only; refuse on divergence. |
| `--into <branch>` | Merge into this host branch instead of the recorded base. |
| `--current` | Merge into the host's currently checked-out branch (= `--into <checked-out branch>`); mirrors `jailbee git push --current`. Mutually exclusive with `--into`; errors on detached HEAD. |
| `--checkout` | If the target branch isn't checked out, check it out, merge, restore the original. Refuses on a dirty host tree. |
| `--cleanup` / `--no-cleanup` | Force both / neither of the post-merge steps (destroy container, delete merged branch). Otherwise governed by the `pull:` config block (`destroy_container`, `delete_branch` ∈ `prompt|always|never`). Cleanup failures are warnings. |
| `--confirm` / `--no-confirm` | Show / skip the plan-and-confirm block shown when JailBee picks the container itself (one candidate, no name). Default: `confirm.auto_target` (true). Off a TTY the block prints and nothing is asked. |
| (no NAME, TTY) | Multi-select picker; pulls in order, **stops at the first failure** (remaining listed, not attempted). |

Conflicts leave the host tree in merge state — resolve and `git commit` (or
`git merge --abort`).

### `jailbee git push [NAME]`

Send a host branch into the container's clone. Source/action from flags, config
defaults (`push.default_source`, `push.default_action`), or interactive when those
are `ask`. CLI flags always win.

| Flag | Effect |
|---|---|
| `--from <branch>` | Host branch to send (default: host default branch). |
| `--current` | Send the host's current branch. Implies the local ref (no fetch). |
| `--merge` / `--rebase` | After transport, merge/rebase the pushed ref into the container's branch. Refuse on a dirty container tree; conflicts leave the container mid-op → resolve in `jailbee shell`. |
| `--plain` | Transport only, no apply. |
| `--pr` | PR containers only: re-fetch the PR head from GitHub into `refs/jailbee/pr/<N>/head` and push that exact ref. The fetch runs host-side, so the container needs no `jailbee net loose`. Refused on non-PR containers. Mutually exclusive with `--from`, `--current`, `--from-origin` and `--from-local` (the ref is fixed). |
| `--from-local` | Push the host's local `refs/heads/<source>` and skip the host fetch. Use when the host has commits not yet pushed to origin. |
| `--from-origin` | Force `refs/remotes/origin/<source>` (overrides `push.push_from: local` and the `--current` default). |
| `--fetch` / `--no-fetch` | Run/skip `git fetch origin <source>` on the host before resolving. Default: `push.autofetch` (true). Only applies when pushing the origin ref. |
| `--confirm` / `--no-confirm` | Show / skip the plan-and-confirm block shown when JailBee picks the container itself (one candidate, no name). Default: `confirm.auto_target` (true). Unlike pull/checkout, this never triggers off a TTY: without an explicit NAME, `push` requires a TTY in the first place and errors before it lists containers, so the block is never reached there. |
| (no NAME, TTY) | Multi-select picker; source/action chosen once, applied to all. Failures **don't** stop the batch — ✓/✗ summary at the end, non-zero exit if any failed. |

`push.default_source` defaults to `base` (push the container's base branch in
without prompting); set to `ask` for the source picker.

**Which copy of the branch travels.** By default (`push.push_from: origin`,
`push.autofetch: true`) the host fetches `origin/<source>` and pushes that ref,
not `refs/heads/<source>`. `git fetch` only moves `refs/remotes/origin/*`; the
local branch advances on `git pull`, so for a branch nobody checks out on the
host (typically the base branch) the local ref is stale exactly when the user
just fetched — and pushing it would force-move the container's
`refs/jailbee/base/<base>` anchor backwards, inflating `jailbee ls` AHEAD. `--current`
always resolves locally; `--pr` sidesteps the question by pushing
`refs/jailbee/pr/<N>/head` verbatim. If the origin ref is pushed while the local
branch has commits it lacks, JailBee prints the count and points at `--from-local`.

### `jailbee git diff [NAME]`

Default: the commits `jailbee git pull` would bring (3-dot diff vs the base branch).
`--wt` working-tree only, `--all` both, `--stat` summary, `-b` override branch.

### `jailbee git retarget NAME NEWBASE [--merge]`

Re-point a container at a new base branch (also top-level `jailbee retarget`). Rewrites
`user.jailbee.base_branch`, so `jailbee git pull`, `jailbee git push`, and the `jailbee ls`
**AHEAD ±** / **MERGE** columns all follow the new base. Built for **stacked-PR
chains**: when a parent PR (say `feat/a`) merges to `main`, retarget the dependent
container from `feat/a` to `main`.

| Flag | Effect |
|---|---|
| `--merge` | After retargeting, merge the new base into the container's branch (equivalent to a follow-up `jailbee git push <name> --merge`). Without it, JailBee just flips the label and prints the `jailbee git push --merge` command to run. |

## PR publishing

### `jailbee pr [NAME]`

Create or update the container's GitHub PR (also `jailbee git pr`). Publishes the
container's branch to the GitHub `origin` by **fetching it to the host, then
fast-forward pushing under the host's credentials** — so, unlike `jailbee git push
--pr`, the network call happens host-side and needs no `jailbee net loose` on the
container. No PR yet → opens a **draft** PR; PR exists → the push updates it. On a
`jailbee new --pr N` container it asks once whether to push the container's commits
to PR #N's head, records the answer (`user.jailbee.pr_adopted`) and updates that PR
on every later run; cross-repository (fork) PRs are refused with a manual-push
recipe. On such a container (a PR JailBee did not create) the PR description is
never regenerated unless you ask — the interactive "update the description?"
offer is suppressed — and `--force` takes a second confirmation.

A container with no PR label gets the same treatment when its **branch** already
has one: before opening anything JailBee runs `gh pr view <container branch>` and, on
an open same-repo PR, asks `Push this container's commits to PR #N instead of
opening a new one?` (`--yes` skips; declining exits without publishing and points
at `--as`). Adopting records `pr` / `pr_branch` / `pr_adopted` but **not**
`pr_author`, so the hands-off rules above apply. A closed/merged or fork PR falls
through to opening a new PR with a printed reason; `--as` skips the lookup; the
lookup itself is best-effort (no `gh`/network → ordinary create path).

Requires `gh` authenticated on the host. No NAME + a TTY → picker.

| Flag | Effect |
|---|---|
| `--title <t>` / `--body <b>` | Set PR title / body (override AI per field). Default on create: last commit subject / placeholder. |
| `--base <branch>` | PR base branch (default: the container's recorded base branch). |
| `--ready` / `--draft` | Mark ready for review / move back to draft. Default: draft on create, unchanged on update. (`--no-draft` is a hidden back-compat alias for `--ready`.) |
| `--description` / `-d` | Update only: regenerate the PR description with Claude and apply it. |
| `--as <branch>` | Explicit PR head branch name (overrides AI naming). **New PRs only** — exit 2 on any container that already has a PR (authored or adopted): its head is fixed, and a different branch would leave the PR untouched. |
| `--yes` / `-y` | Skip the confirmations asked on a `jailbee new --pr` container: the one-time adoption, and the `--force` overwrite gate. Required when there is no TTY. |
| `--no-ai` | Skip AI generation of the title/body; keep the container branch name as-is. |
| `--force` | Force-push the PR head with `--force-with-lease` (rebased/amended branch); refuses if the remote moved. Requires an explicit NAME. On a PR JailBee did not create it first asks to confirm overwriting that head (`--yes` skips; no TTY → error). |
| `--web` | Open the PR in the browser afterwards. |
| `-b` / `--branch <b>` | Override branch detection. |

When `claude.enabled` + `claude.ai_pr_description` (both default on), a new PR's
title/body come from the container's Claude CLI; `claude.ai_pr_branch` similarly
proposes the head branch name (confirmed interactively). On an existing PR the
description is left untouched unless you pass `--description`, `--title`/`--body`,
or accept the interactive prompt — which is only offered for a PR JailBee itself
created.

## Submodules

### `jailbee submodule checkout [NAME] [-b BRANCH]`

Recursively place submodules on the superproject's branch (they can end up on a
detached HEAD after clone / `jailbee git push`/`pull`). With **no NAME** it fixes the
**host** repo's submodules; with a NAME it fixes that **container's** submodules.
`-b` overrides the branch (default: current).

```bash
jailbee submodule checkout                # host repo, current branch
jailbee submodule checkout -b feat/x      # host repo, explicit branch
jailbee submodule checkout feat-foo       # container 'feat-foo', its branch
```

## Network

| Command | Behaviour |
|---|---|
| `jailbee net strict [NAME]` | Egress allowlist (default mode). Clears any loose TTL. **`github.com` intentionally blocked** — don't add it to the allowlist; use loose for pushes. |
| `jailbee net loose [NAME] [--for DUR\|--no-revert]` | Full NAT (uses the `jailbee-loose` bridge). Auto-reverts to the previous mode after a TTL (default 5 min, `loose_auto_revert` config). `--for` sets that TTL for this switch only: `30s`/`45m`/`4h`, max 24h, or `never` (= `--no-revert`); the two flags are mutually exclusive and a bad value exits 2. With neither flag JailBee asks — only on a TTY, with `JAILBEE_NONINTERACTIVE` unset and the policy enabled; otherwise `loose_auto_revert.after` applies with no prompt. With `enabled: false` JailBee schedules no TTL and never asks, but an explicit `--for` is still honoured and still auto-reverts. |
| `jailbee net refresh [--json]` | Re-resolve `egress_allow` hostnames, merge into the per-repo pool, push ACL + `/etc/hosts`. Useful after CDN IP rotation. |
| `jailbee net status` | Refresh-timer health, registered repos, per-repo pool sizes, per-container loose expiry. |
| `jailbee net unregister [--repo <path>]` | Remove the repo from the refresh registry. `jailbee apply` re-registers. |

## GUI

| Command | Notes |
|---|---|
| `jailbee ide [NAME] [--app idea\|webstorm\|pycharm\|...]` | Launch a JetBrains IDE in the container. Needs `jetbrains.enabled`. One IDE at a time across containers (shared profile). |
| `jailbee chrome [NAME] [URL]` | Launch Chrome (per-container profile slot, seeded from the most recent). Needs `chrome.enabled`. URL falls back to `chrome.url`. |
| `jailbee chrome-pool ls` / `prune` | Inspect / delete unallocated Chrome profile slots. |

## Mounts

| Command | Notes |
|---|---|
| `jailbee mount KIND [NAME]` | Attach an `optional_mounts` entry (e.g. `aws`) to a live container. |
| `jailbee unmount KIND [NAME]` | Detach it. |

(For always-on mounts, list them under `host_mounts` in config instead — that's
the jailbee-repo-setup skill's domain.)

## Snapshots

| Command | Notes |
|---|---|
| `jailbee snapshot create [NAME] [TAG]` | Snapshot a container's state (cheap, COW). |
| `jailbee snapshot restore NAME TAG` | Roll back to a snapshot. |
| `jailbee snapshot ls [NAME]` | List snapshots. |
| `jailbee snapshot delete [NAME] [TAG]` | Delete a snapshot. |
