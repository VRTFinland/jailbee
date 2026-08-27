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

- [Setup & host (`setup`, `init`, `apply`, `doctor`, `base`, `registry`, `net install`)](#setup--host)
- [Config (`config show|validate|init`)](#config)
- [Create & lifecycle (`new`, `start`, `stop`, `restart`, `destroy`)](#create--lifecycle)
- [Inspect (`ls`, `dashboard`, `job`, `disk-usage`, `prune`)](#inspect)
- [Enter & run (`shell`, `tmux`, `exec`)](#enter--run)
- [Git bridge (`git fetch|checkout|pull|push|diff|retarget`)](#git-bridge)
- [PR publishing (`pr`)](#pr-publishing)
- [Submodules (`submodule checkout`, `submodule pr`)](#submodules)
- [Network (`net strict|loose|refresh|status|unregister|install`, `net egress ls|add|rm|export`)](#network)
- [Claude accounts (`claude ls|use|add|allow|release|rm`)](#claude-accounts)
- [GUI (`ide`, `chrome`, `chrome-pool`)](#gui)
- [Mounts (`mount`, `unmount`)](#mounts)
- [Snapshots (`snapshot create|restore|ls|delete`)](#snapshots)

## Setup & host

These mutate host-level Incus state or take minutes — don't run them
speculatively.

| Command | What it does |
|---|---|
| `jailbee setup [--yes] [--only completions\|timer\|skills] [--shell bash\|zsh\|fish]` | Per-*machine* post-install steps, the counterpart to the per-repo `jailbee init`: completion scripts for `jailbee` and `jb`, the `jailbee-net-refresh` user timer, and jailbee's Claude skills in `~/.claude/skills`. Interactive by default (one question per step, defaulting to yes for a missing step and no for an installed one); `--yes` asks nothing and never edits a shell rc. Idempotent — re-run after upgrading jailbee. Needs no repo config, so it works from any directory. Does **not** touch host prerequisites (Incus, firewall, UID delegation): `jailbee doctor` reports those. |
| `jailbee init` | First-time setup: create per-repo Incus profiles, egress ACL, the `jailbee-loose` bridge, shared dirs; install the `jailbee-net-refresh` user timer. Errors if profiles already exist. |
| `jailbee apply [-y] [--no-restart]` | Re-push current config (profiles, ACL, `/etc/hosts`, dockerd proxy) to running containers. Replaces the old `jailbee init --reapply` / `jailbee net refresh`. Idempotent; prompts to restart containers if profiles changed (`-y` to skip prompt, `--no-restart` to never restart). |
| `jailbee base build` | Build the golden image from `install.d/` snippets. 10–15 min, one-time (re-run after changing `golden.*` or snippets). |
| `jailbee base prune [--all] [--days N] [--yes-to-all]` | Remove superseded dated golden-image archives (`<alias>-YYYY-MM-DD`). Lists all candidates up front and confirms once — a single batch confirmation, not per-archive — printing the total count + size before prompting. The live base image is always kept; archives currently in use by a container are skipped (batch continues, exit 0). `--all` prunes archives for every registered repo, not just the current one. `--days N` limits removal to archives older than N days (omitted: every dated archive is a candidate). `--yes-to-all` skips the confirmation prompt entirely. |
| `jailbee base usage [--all]` | Show disk usage of golden base images: each live base image and dated archive with its size, a per-repo subtotal, a prunable figure (archives only, i.e. what `jailbee base prune` would reclaim), and a grand total across images shown. `--all` includes every registered repo, not just the current one. |
| `jailbee doctor` | Host- and repo-level diagnostics: Incus running, bridges, subuid/subgid mapping, keyring limits, registry mirror, GitHub token perms, agent setup, port forwards, the `jailbee setup` steps (`shell completions`, `claude skills (host)`), and `upgrade actions` — whether this repo still owes a `jailbee base build` / `jailbee apply` after a jailbee upgrade. Exits non-zero if any check fails, a pending upgrade action included. Not purely read-only: the first run in a repo inserts that repo's upgrade-watermark row. |
| `jailbee registry up [--recreate]\|down\|status` | Control the Incus-hosted Docker registry mirror (rpardini proxy; caches all upstreams). `up` is idempotent and self-repairing: if an earlier provisioning run died partway (a network drop during `apt-get install`), it reinstalls the proxy rather than failing forever. `--recreate` deletes and rebuilds the container for damage reinstalling can't fix; the host-side cache and CA survive. `status`: `running`/`stopped`/`degraded`/`missing`. |
| `jailbee net install` | (Re)install the `jailbee-net-refresh` user systemd timer + service. Idempotent; safe to re-run. Equivalent to `jailbee setup --yes --only timer`. |
| `jailbee version` / `jailbee --version` | Print the version. |

## Config

| Command | What it does |
|---|---|
| `jailbee config init [--global]` | Write a fully-commented `.jailbee/config.yaml` (or `~/.config/jailbee/global.yaml` with `--global`). The template comments ARE the schema docs. |
| `jailbee config show` | Print the merged effective config (global + per-repo) as YAML. Use to check active `push.default_*`, `new.background`, etc. Prints the resolved `agents:` block too — preset fields filled in whether or not the repo's own config mentions them, so it's the way to check what a `claude`/`codex`/… preset actually resolved to rather than re-deriving it by hand. |
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
| `--no-autostart` | Skip the repo's autostart steps — fastest, least-risk way to get a container. Also skips reading the target branch's autostart config (see below): nothing runs, so there is nothing to diff or confirm. Enabled agents are still installed (that is infrastructure, not a user step), so `jailbee tmux` on such a container finds a session holding an `install-<agent>` window — but not the agent's own launch window. |
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
columns. **IP** and **MEM** are *not* in it — reach either from `ls` with
`--fields ip,mem`. The dashboards' own default column set differs in
exactly one of those two: they add **MEM**, since the view refreshes and a
live number earns its width there; **IP** is off by default in both —
enable it in the dashboard settings (see below) if you want it there
instead. **MODE** is dynamic like JOB, TTL and PR: it appears only once a
mount-mode container exists, since on a clone-only host every row would
read `clone`.

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
repo, grouped by repo. Keys: `↑/↓` or `j/k` move (spans repos; repo headers
are cursor stops, not skipped), `Enter` action menu (or fold/unfold the repo
group when the cursor is on its header), `Space` fold/unfold the repo group
under the cursor (works from the header or any container row in it), `F2`/`S`
settings overlay (columns + folding), `r` force refresh, `h`/`?` keybinding
help, `q`/`Ctrl-C` quit. The action menu opens *inline below the table* — the
dashboard stays visible and keeps refreshing behind it; `↑/↓` then move the
menu cursor, `Enter` runs the entry, `Esc`/`q` closes it (`Ctrl-C` always quits
the dashboard).

The menu, in order: `job clear`, `job log`, `pr --open`, `pr`, `git push`,
`git push --pr`, `git pull`, `git diff`, then
tmux/shell/ide/chrome/net/restart/stop/destroy for Running (start/destroy for
Stopped). Each entry appears only when it would do something:

- `job clear`/`job log` need a background-job row (`job log` follows a live
  worker's log and prints a finished one once);
- `pr --open` needs a known PR;
- `pr`, `git push`, `git push --pr`, `git pull` and `git diff` need a **running
  clone-mode** container (a stopped or `--mount` container has no clone to
  publish from);
- `git push --pr` ("Refresh from PR head") also needs a **review** PR — one
  JailBee did not open from the container's own branch, the `#123↓` case in the
  PR column. On an authored PR the head is downstream of the container, so the
  refresh could only be a no-op;
- `git pull` also needs commits ahead of the base, and `git diff` something to
  show. A git status that is merely *unknown* — under `--no-git`, or before the
  first git-tier refresh — hides nothing: a missing column is not evidence of a
  clean tree.

Quick-action keys skip the menu for the highlighted row: `t` attach tmux, `s`
open a shell, `i` launch the IDE, `c` launch Chrome, `p` open the PR, `P`
create/update the PR, `u` update from base, `d` show the diff. Each one
fires only when that action is offered for that container — the gate is the
same one the menu uses, so a Stopped container has no `t`/`s`, `i`/`c` need the
repo's `jetbrains.enabled`/`chrome.enabled`, `p` needs a known PR, `P`/`u`/`d`
need a running clone-mode container, and orphan
rows have none of them. A declined key prints the reason in the panel footer
for a couple of seconds. `git pull` and `job log` are deliberately menu-only:
the first writes to the host's own working tree, and the second's command
varies with `--follow`.

`F2` (or `S`) opens a settings overlay drawn below the live table: `↑`/`↓`
move, `Space` toggles the row under the cursor, `Tab` switches between the
Fields and Repos tabs, `Esc` closes. Changes apply and persist immediately
— there is no OK/Cancel. This is where the TUI's own column set and folded
repo groups live now (in `state.sqlite`'s `view_prefs` table); the `dashboard:`
config block is deprecated (still accepted, but ignored — see
[Configuration](../../../config.md#ls--dashboard--remembered-columns)). The
Qt dashboard (`jailbee gui`) keeps an independent set of its own, via
View ▸ Columns.

Output is not lost when an action prints something. `git diff` opens in
`$PAGER` (`less -R`, then `more`), with colour forced past the pipe; `pr`,
`git push`, `git pull` and `job log` run in the foreground and then wait for
Enter, because the dashboard repaints over the screen the moment it returns.
Prompts those commands would normally ask (`git push`'s merge/rebase picker,
`pr`'s branch-name confirmation) work exactly as on the command line — the TUI
hands over the real terminal. Two-tier
refresh: base state ~3s, git columns ~10s — tune with `-i` / `--git-interval`, or
`--no-git` to drop git columns. Requires a TTY. Orphan containers (jailbee-managed but
repo not registered) show view-only.

`jailbee dashboard --gui` (alias: `jailbee gui`) launches a **graphical Qt** dashboard
instead of the terminal TUI; it detaches to the background by default (`--foreground`
keeps it bound to the terminal). Same `-i` / `--git-interval` / `--no-git` knobs.
It offers the same menu entries under the same rules, but runs them as a GUI
rather than in a terminal: only `shell`/`tmux` open a host terminal emulator,
while `pr`, `git push`, `git pull`, `git diff` and `job log` stream their output
into a JailBee window with Stop and Copy buttons and the exit code on its status
line (non-modal, so the dashboard keeps refreshing behind it). Stop is what ends
a `job log --follow`.

Its child process has no stdin, so anything the CLI would prompt for is asked
first in a dialog and passed as a flag — and only where the CLI would ask:
`git push` asks merge/rebase/plain when the repo's `push.default_action` is
`ask` (its default) and the source when `push.default_source` is `ask`, `pr`
asks about draft/ready, regenerating the description and publishing to an
existing PR's head, and `git pull` asks for confirmation because it merges into
the host's own branch. Cancelling any of those dispatches nothing.
`git push --pr` asks the action the same way but never the source — the PR head
*is* the source, and the CLI rejects `--from`/`--current` alongside `--pr` — so
a repo with a pinned `push.default_action` sees no dialog there at all.

The Qt GUI has a **View** menu to switch between a wide **Table** layout and
a width-adaptive **Cards** layout (cards re-wrap to fill the window). Within
Cards, the same menu picks a card style — **Compact** (default; hides clean
git rows) or **Grid**; Compact renders a hardcoded field selection and
ignores whichever columns are enabled — switch to Grid or Table to see one
that Compact doesn't show. **View ▸ Columns** toggles which columns are
enabled, independently of the TUI's own set (see `jailbee dashboard` above)
— at least one must stay checked. Each repo's card group has a header that
can be clicked to collapse/expand it. It persists, between sessions, in the
SQLite state DB: the chosen layout, card style, collapsed repo groups, the
enabled columns, the table's column widths/order, and the refresh cadence /
paused state — but never the window size or position. `-i`/`--interval`
precedence at startup: explicit flag > persisted value > 3s default.
`--git-interval` is not persisted. Fresh installs default to the Cards
layout.

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

**Attaching over a failed background job.** `shell`, `tmux`, `ide` and
`chrome` all wait on an in-flight `jailbee new --background`. When that job
ended badly — an autostart step failed, or its worker died — but the
container is up, the command reports the failure, points at `jailbee job
clear NAME`, and asks `Continue anyway? [Y/n]` before going in: the failed
container is exactly what you asked to look at. `--force` (and a
non-interactive stdin) skips the question; both dashboards pass it, since
they already show the job state in the JOB column. Ctrl-C out of the wait
gets a similar offer for a container that exists but is still unfinished —
on stricter terms, since the interrupt is an explicit cancel: `Attach
anyway?` defaults to no, is asked even under `--force`, and is skipped
(exit 1) without a TTY.

The command still refuses, without asking, when there is nothing to attach
to (a create that died before the container existed) or when a destroy is
actively tearing the container down.

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
`git merge --abort`). Conflicting submodule gitlinks are merged automatically
first, at every nesting level; if that clears everything the merge commit is
made for you. Whatever is left prints in the `── Submodules` block grouped as
`auto-merged`, `in merge state — resolve these`, and `skipped, not touched`
(dirty sub-repo, or a gitlink on one side only). Every submodule is attempted
in one pass, so one run reports them all. `jailbee git push --merge` prints the
same block for the container side.

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
| `--pr` | PR containers only: re-fetch the PR head from GitHub into `refs/jailbee/pr/<N>/head` and push that exact ref. The fetch runs host-side, so the container needs no `jailbee net loose`. Refused on non-PR containers, and requires an explicit NAME (the label it reads is the container's, so there is no picker). Mutually exclusive with `--from`, `--current`, `--from-origin` and `--from-local` (the ref is fixed). Both dashboards expose it as "Refresh from PR head". |
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
Colour follows stdout; `--color`/`--no-color` forces it either way, which is how
`jailbee dashboard` keeps the diff coloured while piping it into a pager.

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

Generation reads the commits and cumulative diff, `.github/pull_request_template.md`,
the spec or issue the branch implements, and `CONTRIBUTING.md` / `CLAUDE.md` /
`AGENTS.md`, and links a referenced issue with `Closes #N` when it can reach
`gh`. It runs on `claude.ai_pr_model` (default `sonnet`; `null` inherits the
container's default). `claude.pr_prompt` adds project-specific instructions that
outrank JailBee's generic title/body rules — see the config-schema reference in
the `jailbee-repo-setup` skill.

The prompt forbids running the project's tests, build, linters or installers —
testing is described from the commits and the CI config, because the run's budget
is fixed while a suite's cost is the repository's. `claude.ai_pr_timeout`
(default 600 s) bounds the run; on expiry you get a warning plus a placeholder
description, fixable afterwards with `jailbee pr --description`. The warning also
names the container and the session id of the attempt, so you can see how far it
got: `jailbee shell <name>`, then `claude --resume <id>` — Claude writes its
transcript as it works, so a run that ran out of budget is still on disk.

## Submodules

### `jailbee submodule checkout [NAME] [-b BRANCH] [--submodules-only]`

Put the tree — superproject and submodules, recursively — on one branch.
Submodules can end up on a detached HEAD after clone / `jailbee git
push`/`pull`. With **no NAME** it works on the **host** repo; with a NAME on
that **container's** submodules.

```bash
jailbee submodule checkout                # host, align to current branch
jailbee submodule checkout -b master      # host, whole tree to master
jailbee submodule checkout -b master --submodules-only
jailbee submodule checkout feat-foo       # container 'feat-foo', its branch
```

On the host, `-b BRANCH` checks that branch out in the **superproject** first
and then aligns the submodules to it — one command for jumping the whole tree
between `master` and a container's branch (towards a container, use `jailbee
git checkout <container>`, which already aligns submodules). `--submodules-only`
skips the superproject checkout, which is the only way to align submodules
from a detached HEAD or to keep a deliberate mismatch. With a container NAME,
`-b` is pure submodule placement: a container's branch is its identity and is
never switched here.

Placement never rewinds a submodule branch. A submodule whose local branch is
ahead of the superproject's recorded gitlink keeps that newer branch checked
out and warns — bump the pointer with `git add <sub> && git commit` in the
superproject. A dirty or genuinely diverged submodule is left on its detached
HEAD, also with a warning.

### `jailbee submodule pr [NAME] [PATH]`

Create or update a GitHub PR in a **submodule's own** repository, from commits
made inside it in a container — a separate repo from the superproject, so a
separate PR from `jailbee pr`. One PR per run; independent of `jailbee pr`
(neither command is a precondition for the other).

```bash
jailbee submodule pr feat-foo              # auto-target, draft PR
jailbee submodule pr feat-foo libs/foo     # explicit submodule
jailbee submodule pr feat-foo --ready      # mark ready for review
jailbee submodule pr feat-foo --open       # just open it in the browser
```

Without PATH, the submodule that has commits ahead of its own base is targeted
automatically; several ahead prints a table (path, commits, last subject) and
exits 2 asking you to name one — two submodules are two repositories and two
PRs. None ahead is reported as a fact (exit 0), not an error; name one with
PATH to publish it anyway.

The candidate signal is deliberately the submodule's **own**
`refs/jailbee/base/<super-base>` anchor (seeded at container creation), not
the superproject's gitlink diff `jailbee ls` uses. This matters: when commits
were made inside the submodule but the gitlink bump has not been committed in
the superproject yet, the gitlink diff reads zero while the anchor sees
exactly the commits the PR is for — reported as an info line ("commits not
yet in the superproject's gitlink"), never as an error.

| Flag | Effect |
|---|---|
| `--title <t>` / `--body <b>` | Set PR title / body (override AI per field). |
| `--base <branch>` | PR base branch (default: the submodule's own default — see below). |
| `--ready` / `--draft` | Mark ready for review / move back to draft. Default: draft on create. |
| `--description` / `-d` | Update only: regenerate the description with Claude and apply it. |
| `--as <branch>` | Explicit PR head branch name. **New PRs only** — exit 2 once the path has a recorded PR. |
| `--yes` / `-y` | Skip confirmations. Required when there is no TTY. Does **not** skip the AI-proposed branch-name prompt on a TTY (Enter accepts the proposal) — that prompt only skips when stdin is not a TTY, or the proposal equals the branch the commits came from. |
| `--no-ai` | Skip AI generation of the title/body/branch. |
| `--force` | Force-push with `--force-with-lease`; a foreign (adopted) head asks first (`--yes` skips). |
| `--web` | Open the PR in the browser afterwards. |
| `-b` / `--branch <b>` | **Different meaning than in `jailbee pr`:** which branch to read **from the submodule** in the container — the escape hatch for a detached submodule or for publishing a branch other than the one checked out there. |
| `--open` | Read the recorded PR for PATH and open it; no preflight, no transport, no `gh` mutation. Requires PATH when PRs are recorded for more than one path. No recorded PR is exit 1. |

Base branch: `--base` > `submodule.<name>.branch` declared in `.gitmodules`
(unless `.`) > the sub-repo's `<remote>/HEAD` > `main`. The declaring
`.gitmodules` is found by descending from the repo root level by level —
correct for both a top-level submodule (`libs/foo`, declared by
`repo_root/.gitmodules`) and a nested one (declared by its immediate
parent's `.gitmodules`). The remote is resolved **per submodule**, since a
submodule may name its upstream something the superproject does not — this
cannot reuse the container-side submodule-default logic, which hardcodes
`origin` for its own callers.

Head branch (the name pushed to the submodule's upstream): `--as` > Claude's
proposal (`claude.ai_pr_branch`, confirmed interactively) > the branch the
commits were read from (`--branch`, else the submodule's current branch in
the container). A detached submodule with no `--branch` still publishes (from
its `HEAD` ref) but needs `--as` or the AI to name the branch; without either,
exit 2 explains why. The chosen head is remembered in one container config
key, `user.jailbee.sub_pr` (a JSON map keyed by submodule path), so a re-run
updates that PR instead of opening a second one for the same work.

On success, when the container also has a superproject PR
(`user.jailbee.pr`), JailBee notes the merge order as information, never a
gate: merge the submodule PR first, so the superproject PR's gitlink bump
then points at a merged commit.

Exit codes: 2 for usage errors (ambiguous target with no PATH, unknown PATH,
`--as` once a PR is recorded, a detached submodule with no resolvable head
name, `--open` with PRs recorded for more than one path); 1 for operational
failures (preflight, a non-GitHub submodule upstream, publish failure, `gh`
failure, `--open` with no PR recorded, `--force` onto a foreign/adopted PR
head with no TTY to confirm on, first-run adoption of an existing PR with no
TTY to confirm on); 0 for success and for "no submodule has commits ahead of
its base."

## Network

| Command | Behaviour |
|---|---|
| `jailbee net strict [NAME]` | Egress allowlist (default mode). Clears any loose TTL. **`github.com` intentionally blocked** — don't add it to the allowlist; use loose for pushes. |
| `jailbee net loose [NAME] [--for DUR\|--no-revert]` | Full NAT (uses the `jailbee-loose` bridge). Auto-reverts to the previous mode after a TTL (default 5 min, `loose_auto_revert` config). `--for` sets that TTL for this switch only: `30s`/`45m`/`4h`, max 24h, or `never` (= `--no-revert`); the two flags are mutually exclusive and a bad value exits 2. With neither flag JailBee asks — only on a TTY, with `JAILBEE_NONINTERACTIVE` unset and the policy enabled; otherwise `loose_auto_revert.after` applies with no prompt. With `enabled: false` JailBee schedules no TTL and never asks, but an explicit `--for` is still honoured and still auto-reverts. |
| `jailbee net refresh [--json]` | Re-resolve `egress_allow` hostnames, merge into the per-repo pool, push ACL + `/etc/hosts`. Useful after CDN IP rotation. |
| `jailbee net status` | Refresh-timer health, registered repos, per-repo pool sizes, per-container loose expiry, and (new) an "Egress overrides" section listing every host-local override on this host. |
| `jailbee net unregister [--repo <path>]` | Remove the repo from the refresh registry. `jailbee apply` re-registers. |

### Egress overrides — `jailbee net egress`

Also available as the short root alias `jailbee egress` (hidden from
`jailbee --help`, works identically). `NAME` resolves the same way as every
other container command (short branch name, full name, or an interactive
picker on a TTY); `--repo` always short-circuits that resolution — a
repo-scope change touches no one container.

| Command | Behaviour |
|---|---|
| `jailbee net egress add ENTRY [NAME] [--repo]` | Allow one host. Defaults to container scope (stored in the container's `user.jailbee.egress_extra` label — dies with the container). `--repo` stores a host-local, uncommitted override applying to every container of this repo on this host instead. Rejects before storing anything: a malformed `ENTRY` (exit 2) or a hostname that fails to resolve (exit 1) — two different failures, two different codes. A no-op (exit 0, informational) if the entry is already covered by `config.yaml` or already stored at that scope. Materialises against the container's **current** network mode: adding to a `loose` container stores the label but touches no ACL/NIC until the container returns to `strict` (`--repo` has no container to materialise against, so it always just stores). |
| `jailbee net egress rm ENTRY [NAME] [--repo]` | Remove an override. **Refuses** (exit 1, points at the config file) an entry that exists *only* in `config.yaml` — overrides can only widen the allowlist, never narrow config. Removes normally if the same entry is *also* stored as an override (typically one promoted with `export` and then pasted, or re-added on purpose) — that's the row that goes away, config is untouched. Same current-network-mode materialisation as `add`. |
| `jailbee net egress ls [NAME] [--format table\|json]` | Show every applicable entry with its source (`config`, `repo-override`, `container`) and a `redundant` note when an override duplicates a wider-scoped entry that already covers it — a repo-override row against `config.yaml`, or a container row against either `config.yaml` or a repo-scope override. With no `NAME`, shows repo scope only (config + repo overrides) — a read command must not prompt for a container — and prints a one-line stderr hint that container-scope entries are hidden; pass `NAME` to include that container's own overrides. |
| `jailbee net egress export [NAME]` | Print a **complete replacement** for the repo config's `egress_allow:` key — existing file entries plus promotable overrides — sourced from the repo config file itself, never from the global config layer or jailbee's feature auto-added hosts (those track releases and would go stale frozen into a file). Paste over the *whole* key, don't append one below it: a second `egress_allow:` mapping key makes `yaml.safe_load` silently keep only the last one, discarding the first. With no `NAME`, repo-scope overrides only; pass `NAME` to include that container's overrides too. After pasting and `jailbee apply`, the now-redundant overrides can be dropped with `jailbee net egress rm`. |

Overrides are **additive only** — neither scope can revoke what
`config.yaml` grants. See [docs/security.md](../../../security.md#egress-overrides)
for the security posture: an override widens a boundary that never passes
code review, and no container can reach these commands to grant itself
egress (no `jailbee` binary, no Incus socket inside a container).

`--repo` add/rm only store or remove the override row — they never touch a
container's ACL or `/etc/hosts` themselves. `jailbee apply`, or the next
`jailbee net refresh` timer tick, is what actually materialises a `--repo`
change. Container-scope add/rm are the opposite: they materialise
immediately (against the container's current network mode, per the table
above), with no separate apply step needed.

## Claude accounts

`jailbee claude` switches this repo's Claude Code login between accounts
stored in a host-global pool, without a browser login. Needs the optional
`cswap` (claude-swap) binary (`uv tool install claude-swap`); every verb
below exits 1 with an install hint if it isn't on `PATH`, and exits 1 if
`agents.claude`/`claude.enabled` is off for this repo (no shared Claude
directory to switch), or if `<shared_dir>/claude` doesn't exist yet — that
directory is created by `jailbee init`/`apply`/`new`, so a repo that has
never applied is told to run `jailbee apply` first. (The one exception to
the `cswap` requirement is `jailbee claude release` with no argument: it
touches nothing but jailbee's own database, so it keeps working when `cswap`
has been uninstalled.) One stored account is held by at most one repo at a
time — that's what stops two repos from independently refreshing the same
grant and silently logging one of them out.

**What the ledger tracks, and what it doesn't.** It records which repo was
handed which *stored* account — not which credential is live on disk, and
three ordinary actions move one without the other. `jailbee claude release`
frees the row but leaves this repo logged in as that account and still
rotating its credential. `jailbee claude ls` — and every verb that resolves
a slot/alias/email reference — asks `cswap` for each account's quota, which
goes to the network and **refreshes the stored token of every account that
isn't live in this repo**, including one another repo is live on. And a
`cswap switch` run by hand, outside jailbee, moves a credential with no row
at all. So the ledger stops the pool from *handing out* the same stored
grant twice; it cannot stop a grant already handed out from being rotated
under someone. `jailbee claude ls` flags what it can see: an account
`cswap` reports as live here with no holding row is shown as `live here,
unclaimed`, and one live here while another repo holds it says so too.

| Command | Behaviour |
|---|---|
| `jailbee claude ls [--format\|-o table\|json]` | List pooled accounts: slot, alias, email, 5h/7d quota %, holder, and a NOTE column (shown only when some row has something to say — `live here, unclaimed`, `live here, but held by <repo>`, `held out of rotation (cswap disable)`, `re-login needed`, `not allowed for this repo`, …). Table mode also prints a stderr hint naming which account (if any) this repo currently holds, and, when this repo's allowlist is narrowed, which accounts are allowed. **Not free and not read-only from Anthropic's point of view:** fetching the quota column refreshes the stored token of every account that isn't live in this repo (see the paragraph above), so don't loop it. |
| `jailbee claude use [REF] [--force]` | Switch this repo's login to `REF` (slot number, alias, or email); omit for an interactive picker (exits 2 off a TTY). Refuses (exit 1): a `REF` matching no account; `REF` not on this repo's allowlist; the account held by another repo (message names the repo and prints the exact `cd … && jailbee claude release` to run there); or — unless `--force` — a live login that was never captured with `jailbee claude add` (`cswap` stashes it before switching, but recovering a stash is a manual job, so the message points at `jailbee claude add --alias <name>` first). `--force` prompts for confirmation on a TTY before switching over an uncaptured login — off a TTY that prompt defaults to "no", so `--force` still exits 1 there rather than forcing anything. Every one of those refusals is checked *before* the `--force` prompt, so you're never asked to accept a risk on a command that then declines. Already holding the target account is a no-op that still refreshes the displayed slot number. If `cswap` reports landing on a different account than the one that was asked for (a slot renumbered by `cswap move` between the listing and the switch), the command fails and writes no holding rather than recording a claim it can't back. No browser login, no container restart — Claude Code picks up the new credential on its next message. |
| `jailbee claude add [--alias NAME] [--slot N]` | Capture this repo's current live login into the pool, **and record this repo as the holder** — the pool now has a copy of this repo's credential, so the ledger has to say so. Exits 1 if there is no live login; if another repo already holds that account (re-capturing would overwrite the stored copy that repo is tracking, so this is refused before `cswap` runs and before the alias prompt); or if `cswap add` itself fails or is cancelled at its own interactive `[y/N]` slot-collision prompt. Omitting `--alias` prompts for one (default: the email's local part) or exits 2 off a TTY. `--slot` stores into a specific slot instead of the next free one. Re-running for a login already in the pool just refreshes its stored credential. |
| `jailbee claude allow [REFS...]` | Replace (not append to) this repo's allowlist with `REFS` — slot numbers, aliases, or emails. No `REFS` opens a checkbox picker pre-checked with the current list (exits 2 off a TTY); unchecking everything clears the restriction. An empty list, either way, means every pooled account is allowed. Refuses (exit 1) if any `REF` fails to resolve, leaving the existing allowlist untouched — a list with one typo in it is not applied half-way. |
| `jailbee claude release [REF]` | Give up a holding. No `REF`: releases **every** holding of this repo (normally one; a crash mid-switch can leave two, and both are freed and reported) and prints the caveat that matters — releasing is bookkeeping only, this repo stays logged in as that account and keeps refreshing its credential, so another repo taking it now can log this one out. The clean handover is `jailbee claude use <other>` here first, which checks this repo's rotated credential back in. A no-op with exit 0 if it holds none, and the only verb that works with `cswap` uninstalled. With `REF`: releases that account's holding wherever it is — the escape hatch for a repo whose checkout is gone, or for a stuck `claiming` row a crashed `use` left behind (`jailbee doctor` reports these under "claude pool ledger", and names both recoveries: re-running `use` there if the switch completed, releasing if it didn't). Exits 1 only if `REF` doesn't resolve, or if `cswap` isn't installed (the `REF` form needs a listing). |
| `jailbee claude rm REF` | Remove an account from the pool entirely via `cswap remove` (its own interactive `[y/N]` confirmation). Warns, but still proceeds, if another repo currently holds it. Omit `REF` for a picker (exits 2 off a TTY) — unlike the other pickers, this one also lists accounts held elsewhere, since removal isn't blocked by a holding. Exits 1 if `REF` doesn't resolve, or if `cswap remove` fails or is cancelled. |

`REF` resolution (`use`, `allow`, `release`, `rm`) tries an exact slot
number, then alias, then email. A `REF` matching nothing at all — the
everyday typo — is refused (exit 1, listing the available accounts)
rather than silently ignored. So is ambiguity: a digit that is *also*
another account's alias, an alias shared by two accounts, or an email
shared by two accounts (one address can be both a personal login and an
organization member) all fail with exit 1 and a message naming the slot
number as the unambiguous fallback.

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
