# Commands

| Command | Description |
|---|---|
| `jailbee init` | First-time setup: create Incus profiles, ACLs, shared directories |
| `jailbee apply [-y] [--no-restart]` | Re-apply current config (profiles, ACL, /etc/hosts, dockerd proxy); prompt to restart containers if profiles changed |
| `jailbee new <name> [<base>] [opts]` | Create container, clone repo, run autostart. `<name>` names the environment (used as the branch inside the container, slugified into the container name). `<base>` sets the container's base branch: it is branched off when `<name>` is new, and used purely as the comparison anchor when `<name>` already exists (that branch is cloned as-is for review, after a confirmation). Without `<base>` the base is the repo's default branch. `--current`, `--pr <N>`, `--mount`, `--background`, `--tmux`, `--shell`, `--no-clone`, `--no-autostart` |
| `jailbee ls [--all] [-o json] [--fields …]` | List managed containers + their git status (own repo by default; `--all` for every repo). `LOCAL ±`/`L↑` — the diff vs the host's *currently checked-out* branch, as opposed to `AHEAD ±`'s pinned base — are off by default; opt in with `--fields` or the `ls:` config block (table output only; `-o json` always keeps its built-in field set unless `--fields` is passed). See [Configuration](config.md#ls--dashboard--remembered-columns) |
| `jailbee job ls [--all-repos] [-o json] [--fields …]` | List in-flight and failed background jobs with phase, pid, age, error and log path |
| `jailbee job log <name> [--follow]` | Print (or follow) the worker log of a background job |
| `jailbee job clear [<name>] [--all]` | Acknowledge a dead background job — clears the `failed`/stale record without touching the container. Refuses a job whose worker is still alive |
| `jailbee dashboard` | Live, auto-refreshing TUI of containers across all repos; navigate + act (Enter) |
| `jailbee shell <name>` | Interactive shell (lands in the in-container clone) |
| `jailbee tmux <name>` | Attach to the autostart tmux session inside the container |
| `jailbee exec <name> -- <cmd>` | Run a command in the container as the dev user (e.g. `jailbee exec smoke -- pnpm test`) |
| `jailbee start/stop/restart <name>` | Lifecycle (start/restart re-run autostart) |
| `jailbee destroy [<name>] [--all] [--force] [--background]` | Destroy one container, `--all` for the whole repo, or no-arg interactive checkbox. Before the usual confirmation, jailbee assesses what would be lost (dirty tree, changed submodule, commits held nowhere else) and, if anything is at risk, shows a summary and a second confirmation defaulting to No; `--force` skips both prompts and the assessment |
| `jailbee git fetch <name> [-b <branch>]` | Fetch commits from a container's clone into `refs/jailbee/<short>/<branch>` |
| `jailbee git checkout <name> [-b <branch>] [--as <name>] [--confirm\|--no-confirm]` | Fetch + check out the container's branch on the host (ff-only). `-b` picks which branch to read **from the container**, `--as` names the branch written **on the host** (default: the container branch, or its PR head when set). With one eligible container and no name, confirms first (`confirm.auto_target`). |
| `jailbee git pull [<name>] [--into <b>\|--current] [--ff] [--checkout] [--cleanup\|--no-cleanup] [--confirm\|--no-confirm]` | Fetch + merge the container's branch into its **base branch** (alias: `jailbee pull`). `--current` merges into the host's checked-out branch instead (mutually exclusive with `--into`). No name → multi-select picker. With one eligible container and no name, confirms first (`confirm.auto_target`). |
| `jailbee git retarget <name> <base> [--merge]` | Re-point a container's base branch (stacked-PR maintenance) |
| `jailbee git push [<name>] [--merge\|--rebase\|--plain\|--force] [--from <b>\|--current] [--from-origin\|--from-local] [--fetch\|--no-fetch] [--confirm\|--no-confirm]` | Send a host branch into a container (alias: `jailbee push`). No name → multi-select picker. By default the host fetches and pushes `origin/<source>`, not the local branch — a local `refs/heads/<base>` only advances on `git pull` (`push.push_from` / `push.autofetch`). `--from-local` sends the host's local branch instead; `--current` always resolves locally, and `--pr` pushes `refs/jailbee/pr/<N>/head` verbatim (so `--from-local`/`--from-origin` are rejected with it). With one eligible container and no name, confirms first (`confirm.auto_target`). |
| `jailbee git push <name> --pr` | For `jailbee new --pr` containers, re-fetch the PR head from GitHub and push it in (pull in commits the author pushed since you started) |
| `jailbee pr <name> [--ready\|--draft] [--description] [--title <t>] [--body <t>] [--as <name>] [--force] [--web] [--no-ai] [--yes]` | Create a draft PR or update an existing one. Opens a new PR when the container has none yet; when one exists, pushes new commits and optionally regenerates the description (`--description`) / toggles draft state. When `claude.enabled`, the new PR's head branch name and description are AI-generated (convention-aware, confirmed on a TTY). `--as <name>` sets the head name explicitly on a **new** PR (exit 2 once the container has one — the PR's head is fixed); `--force` force-pushes a rebased/amended branch with `--force-with-lease` (requires explicit container name). `--no-ai` opts out of AI generation entirely (top-level command; hidden alias: `jailbee git pr`). On a `jailbee new --pr` container jailbee asks once whether to push the container's commits to that PR's head branch and remembers the answer (`--yes` skips the prompt); on that PR — one jailbee did not create — `--force` also asks before overwriting the head and the description is never regenerated unless you pass `--description`/`--title`/`--body`. Fork PRs are refused with a manual-push recipe. When a container has no PR but its branch already has an open one, jailbee offers to push to that PR instead of opening a duplicate (same recorded labels and hands-off rules; closed/merged and fork PRs fall through to a new PR, and `--as` skips the check). |
| `jailbee git diff <name> [--wt\|--all\|--stat]` | Show the diff between a container and the host (alias: `jailbee diff`) |
| `jailbee submodule checkout [<name>] [-b <branch>]` | Place submodules on the superproject branch locally — host repo by default, or a named container; no host↔container transport |
| `jailbee net strict <name>` | Switch to the egress allowlist; clears any loose TTL |
| `jailbee net loose <name> [--for <dur>\|--no-revert]` | Switch to full NAT. `--for` sets the auto-revert TTL for this switch only (`30s`, `45m`, `4h`; max 24h; `never` = no auto-revert, same as `--no-revert`). With neither flag on a TTY, jailbee asks; otherwise `loose_auto_revert.after` applies. See [Configuration](config.md#loose_auto_revert) |
| `jailbee net refresh/status/unregister/install` | Egress-pool refresh timer + allowlist management |
| `jailbee snapshot create/restore/ls/delete <name> [tag]` | Snapshots |
| `jailbee mount <kind> <name>` / `jailbee unmount <kind> <name>` | Optional mounts |
| `jailbee ide <name> [--app idea\|webstorm]` | Launch JetBrains IDE |
| `jailbee chrome <name> [URL]` | Launch Chrome |
| `jailbee chrome-pool ls/prune` | Inspect or clean Chrome profile pool |
| `jailbee base build` | Build the golden image |
| `jailbee base prune [--all] [--days N] [--yes-to-all]` | Remove superseded dated golden-image archives (`<alias>-YYYY-MM-DD`). Lists all candidates and confirms once (a single batch confirmation, not per-archive); the live base image is always kept; in-use archives are skipped (batch continues). `--all` prunes archives for every registered repo, not just the current one; `--days N` only removes archives older than N days (default: all dated archives are candidates); `--yes-to-all` skips the confirmation prompt |
| `jailbee base usage [--all]` | Show disk usage of golden base images: each live base and dated archive with its size, per-repo subtotals, a prunable (archives-only) figure, and a grand total. `--all` includes every registered repo, not just the current one |
| `jailbee registry up [--recreate]` / `down` / `status` | Docker registry mirror control. `up` repairs a half-provisioned mirror in place (reinstalls the proxy when its Quadlet unit is missing, and once more if the service never starts); `--recreate` deletes the container and rebuilds it from the image, preserving the host-side cache and CA |
| `jailbee doctor` | Diagnostics |
| `jailbee disk-usage` | Disk usage breakdown |
| `jailbee prune` | Interactive cleanup of stale containers |
| `jailbee config show/validate/init` | Configuration |
| `jailbee version` | Print the jailbee version |

### `jailbee gui` / `jailbee dashboard --gui`

The graphical (Qt) counterpart to the terminal dashboard — the same live,
cross-repo container view with per-container actions via right-click / popup
menus. Requires the optional PySide6 extra. From a checkout of the jailbee repo:

    make install

(or: `uv tool install -e '.[gui]'`)

By default the GUI **detaches to the background**: the command prints a
"Launched jailbee dashboard GUI in the background" message and returns
immediately, with the window's stdout/stderr logged to `/tmp/jailbee-gui.log`
(useful if the window fails to appear — e.g. missing PySide6 platform
plugins). Pass `--foreground` to run it attached to the current terminal
instead (blocks until the window closes; errors surface directly).

Interactive actions (shell, tmux) open in a host terminal emulator. Set
`$JAILBEE_TERMINAL` to force a specific emulator; otherwise jailbee auto-detects one
(x-terminal-emulator, ptyxis, gnome-terminal, konsole, foot, alacritty, kitty, xterm). IDE and
Chrome launches reuse the same `jailbee ide` / `jailbee chrome` behaviour.

Options mirror the TUI: `--interval`, `--git-interval`, `--no-git`, plus
`--foreground` (GUI-only).

#### Layouts: Table vs Cards

A **View** menu switches the container list between two layouts:

- **Table** — the original wide, sortable columns view.
- **Cards** — a width-adaptive card grid; cards re-wrap to fill the window
  (one column when narrow, several side by side when wide).

Both layouts share the same right-click action menu and selection behaviour.
Fresh installs (no prior GUI session) open in **Cards**.

#### Persisted state

Between sessions the GUI remembers, per-machine, in the same SQLite state DB
used for other jailbee state (`state.sqlite`):

- the selected layout (Table or Cards),
- the table layout's column widths and order,
- the refresh cadence and whether auto-refresh is paused (set via the
  **Refresh** menu).

**Not** persisted: window size and position — that's left to the window
manager.

`--interval` precedence when the GUI starts: an explicit `--interval`/`-i`
flag wins, otherwise the persisted refresh cadence from the last session is
used, otherwise the default of 3s. `--git-interval` is never persisted — pass
it each time it should differ from the default.

---

> For full flag-level detail without `jailbee <cmd> --help`, the **jailbee-usage**
> skill (`docs/skills/jailbee-usage/`) and its `references/commands.md` document
> every command and option.
