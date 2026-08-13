# Changelog

## Unreleased

## 1.0.0 - 2026-08-13

### Added: first public release

**JailBee** runs isolated, per-branch development environments in Incus system
containers. Each branch gets a full system container — its own services,
Docker daemon, IDE and browser — cloned copy-on-write from one golden image,
so several stacks run in parallel on a single host without port, Docker-name
or database collisions. Every repo configures itself through
`.jailbee/config.yaml`; the golden image ships stack-neutral, with language
toolchains available as opt-in stacks. The CLI is `jailbee`, or `jb` for short.

The release covers:

- **Container lifecycle** — `jailbee new/shell/tmux/exec/start/stop/restart/destroy`,
  snapshots, optional mounts, background create/destroy with `jailbee job`
  inspection, and interactive pruning of stale containers. `jailbee new --tmux`
  (or `--shell`) lands straight in the new container once it is ready.
- **Host↔container git bridge** — the container is a git remote:
  `jailbee git push/pull/fetch/checkout/diff/retarget`, base-branch-aware
  merges, stacked-PR maintenance, submodule placement.
- **GitHub integration** — `jailbee pr` creates and updates PRs (AI-generated
  head name and description when Claude is enabled), `jailbee new --pr` builds a
  review container from a PR, and `gh` works inside containers via scoped PATs.
- **Networking** — per-container egress allowlist with `strict` and `loose`
  (auto-reverting) modes, a shared Docker registry mirror, and `/etc/hosts`
  pinning.
- **Desktop integration** — JetBrains IDE (`jailbee ide`) and Chrome
  (`jailbee chrome`) passthrough to the host Wayland session, plus a live TUI
  dashboard (`jailbee dashboard`) and an optional Qt dashboard (`jailbee gui`) that
  span every repo on the host.
- **Host tooling** — `jailbee init`/`apply` for profiles, ACLs and shared state,
  `jailbee base build/prune/usage` for the golden image, `jailbee doctor`,
  `jailbee disk-usage`, and shell completion for containers, branches and tags.
- **Experimental macOS support** — drive a Linux VM (Colima/Lima) from an
  Apple Silicon Mac with the repo shared from macOS.

### Fixed: a submodule created in the container kept a container-bound `origin` on the host

Pulling a submodule that was added *inside* a container already worked —
`transport_submodules_to_host` clones a sub-repo the host is missing — but the
clone came over `ext::incus exec … git upload-pack …`, and git recorded that as
its `origin`. `git submodule update --init` does not repair it (only
`git submodule sync` would), so the host was left with a submodule whose remote
pushed into a container and broke as soon as that container was destroyed.

The clone's origin is now set to the URL the container's `.gitmodules` records
for that path — the same upstream any other clone of the superproject gets, and
the mirror of what the host → container direction does. Nested submodules read
their own level's `.gitmodules`. An existing host sub-repo is untouched: its
remotes are the user's. A failure to rewrite the remote warns instead of
failing the pull, since the objects are already across by then.

### Fixed: `jailbee git push` with a submodule the container doesn't have yet

Adding a submodule on the host and pushing broke the transport: the container
has no repo at that path, so `git receive-pack <repo_dir>/<path>` failed with
"does not appear to be a git repository" and the push died on an unhandled
`GitError` traceback — after some submodules had already been transported.

`transport_submodules_to_container` now creates the missing sub-repo first
(`git init`, `origin` seeded from the host sub-repo's upstream) and leaves it
on the pushed tip, so the container-side `submodule update --init` finds a
current revision and needs no network. This mirrors the container → host
direction, which already cloned sub-repos the host was missing. An existing
container sub-repo is only pushed into — its HEAD and working tree, which may
carry in-container work, are never touched. `jailbee git push` also exits 1 with
the message on any other git failure instead of printing a traceback.

### Added: `jailbee git checkout --as`, and a real error for a branch the container lacks

`jailbee git checkout` can now land the container's work on a differently named
host branch: `jailbee git checkout compose-4 --as compose-4-1`. The host name was
previously not choosable at all — it was the container's branch name, or its
`user.jailbee.pr_branch` label when set (`--as` outranks that label). `-b/--branch`
keeps its meaning on every bridge command: it selects the branch read *inside
the container*, never the host-side name.

Passing `-b` for a branch the container doesn't have used to reach `git fetch`
and surface as an unhandled `GitError` traceback ("couldn't find remote ref").
It is now caught before the fetch, with the container's actual branch names
listed, and `jailbee git fetch`/`checkout` exit 1 on any other git failure instead
of printing a traceback (`jailbee git pull` already did).

### Changed: dropped the `offline` network mode; `jailbee net loose` gains a TTL override

The third network mode, `offline` (no network device attached), is gone.
`strict` (default-deny egress allowlist) already covers "no unexpected
egress" without a second, harder deny-all mode alongside it. `jailbee net
offline` no longer exists, and `defaults.network` /
`autostart.steps[].network` accept only `strict | loose` (the step field
stays nullable); loading a config that still says `offline` fails with
`network mode 'offline' was removed — use 'strict' (default-deny egress
allowlist)`.

Containers created by an older `jailbee` and still carrying the stale
`<prefix>-net-offline` profile are migrated automatically: `jailbee apply`
moves them onto `<prefix>-net-strict` and deletes the now-unused profile.

**Upgrade note:** that migration only touches container profiles, not
config files. If `.jailbee/config.yaml` or `~/.config/jailbee/global.yaml` still
has `defaults.network: offline` (or an autostart step with `network:
offline`), `jailbee` refuses to load it at all — `jailbee apply` never gets a
chance to run and migrate anything. Edit that line to `strict` by hand
*before* upgrading.

Separately, `jailbee net loose <name>` now takes `--for <duration>` (e.g.
`30s`, `45m`, `4h`; capped at 24h; `never` disables the auto-revert for
this switch, same as `--no-revert`). Omit both `--for` and `--no-revert`
on a TTY and jailbee prompts for how long to stay loose, defaulting to the
configured `loose_auto_revert.after`; the Qt dashboard asks via its own
dialog since its detached actions have no stdin to prompt on. With
`loose_auto_revert.enabled: false`, jailbee schedules no TTL of its own and
asks nothing — but an explicit `--for` is still honoured and still
auto-reverts.

### Added: `LOCAL ±`/`L↑` columns, remembered column preferences, and a destroy guard

`jailbee ls` and both dashboards can now show **LOCAL ±** (`local_diff`) and
**L↑** (`local_count`) — the diff/commit-count between a container's HEAD
and the host's *currently checked-out* branch, as opposed to `AHEAD ±`/`↑`,
which is measured against the container's pinned base branch. Both are off
by default (opt in with `--fields` or the new `ls:`/`dashboard:` config
block). The underlying probe is opportunistic and read-only: it never
fetches or writes a ref, so a `?` in either column just means neither side
happened to already hold the other's tip as a commit object — a `jailbee git
pull` resolves it by putting the container's tip on the host.

New `ls:`/`dashboard:` config blocks (in `~/.config/jailbee/global.yaml` — the
normal home, since column choice is personal — and per-repo
`.jailbee/config.yaml`, merged field-by-field: a repo block that sets only
`hide` still inherits the global `fields`, and vice versa) let a column set
be remembered: `fields` picks an explicit ordered list (naming a column
always shows it, even one that's off by default or would otherwise be
hidden), `hide` subtracts from the built-in default set. `hide` *replaces*
the list it is set in rather than extending it, so `dashboard: {hide: [ip]}`
brings REPO / FULL NAME / GIT STATUS / CREATED / TTL back into the table —
copy the documented default list and append if you meant "one more". Both
apply to table output only — `jailbee ls --format json` keeps its built-in field
set regardless, so a personal preference can't silently narrow a script's
expected shape — and an explicit `--fields` flag beats both in every format.
The dashboards resolve `dashboard:` against the repo you launched from,
falling back to the global file, since they render one shared table across
every repo; the Qt dashboard's Compact card style renders a hardcoded field
selection and ignores `fields`. An unknown column name, `fields: []`, or a
name repeated in `fields` is never fatal at load time, in either file: a
column choice is a personal display preference, and a typo in it must not
break an unrelated command. Both `global.yaml` and a repo's
`.jailbee/config.yaml` recover from it the same way (the bad name dropped, or
`fields` reset to the built-in default set) and print a warning naming the
file it came from; `jailbee config validate` is where all three are still
reported as errors, for both files, with the allowed names listed for an
unknown one.

`jailbee destroy` (and, now, `jailbee git pull`'s post-merge cleanup destroy) warns
before discarding anything a fresh probe shows is at risk — a dirty working
tree, a changed submodule (named as `(added)`, `(committed +n -m)` and/or
`(uncommitted +n -m)`, never as a bare `+0 -0`), or commits held on neither
the host nor a remote — with a summary and a second confirmation defaulting
to No. Unknown never reads as safety: an unmeasurable commit count still
warns ("commits not on the host (count unknown)") when the container's HEAD
is on neither the host nor a remote-tracking ref, and a container whose git
status could not be read at all gets a "could not inspect the container"
reason. A container that was never probed (the normal case for a stopped
one) gets a note instead of silence, with no extra prompt — except in mount
mode, where the working tree *is* the host's directory and survives the
destroy, so there is nothing to warn about. `--force` skips the guard
entirely on every path, matching the existing confirmation skip. The Qt
dashboard (`jailbee gui`) runs the identical assessment, with the identical
wording, in its own dialog, since its destroy launches as a detached,
already-`--force`d background process that has no terminal to prompt on.

### Fixed: `jailbee registry up` repairs a half-provisioned mirror

`jailbee registry up` provisioned the `jailbee-registry-mirror` container exactly
once, on the run that created it. If that run died partway — a network drop
during `apt-get install podman` is enough — the container still existed and
still booted, so every later `jailbee registry up` merely started it, waited 60
seconds for a proxy service that had never been installed, and failed.
Recovery meant reaching past `jailbee` to `incus delete jailbee-registry-mirror`.

`up` now reinstalls the proxy when the container is missing its Quadlet unit
file (the signature of an interrupted install), and once more if the service
still doesn't come up. Reinstalling no longer truncates
`/etc/jailbee-registry-proxy.env`, so per-repo upstreams survive the repair. For
damage a reinstall can't fix, `jailbee registry up --recreate` deletes the
container and rebuilds it from the image; the host-side cache and CA
directories are preserved, so no user container loses its trust in the
mirror's CA.

### Added: `jailbee new` provisions with the target branch's own autostart config

In clone mode, `jailbee new <branch>` now reads the `autostart` block from the
target branch's committed `.jailbee/config.yaml`, at the exact commit it clones —
so a container runs the startup steps its branch actually ships, instead of
whatever the operator's checkout happened to have. Every other config key
(mounts, network defaults, resource limits, `container_prefix`, host-level
keys) still comes from the operator's checkout; a branch cannot change how
containers are run.

A deviation from the checkout prints a compact diff naming the ref or commit it
read (added/removed/changed steps, `step_timeout`/`env` changes).

Whether the branch *gains* anything is a separate comparison, made against the
repo's reviewed baseline — `refs/remotes/origin/<default_branch>` — rather than
the checkout, which is only ever one snapshot of one branch and may lag origin,
run ahead of it, or carry local edits. It prints its own `branch autostart
widens privileges beyond …` block, and falls back to comparing against the
checkout when that ref has no usable config.

Two kinds of widening are reported, weighed differently. A step attaching an
`optional_mounts` entry the baseline's same-named step does not **always** asks
for confirmation before anything is created, defaulting to no: those are
typically personal credential directories (`~/.aws`, `~/.m2`), the step's
command line comes from the same branch, and attaching the mount is what
creates the asset. A step widening network access from `strict` to `loose` asks
only for an untrusted head — `jailbee new --pr N` where the PR's head lives in a
**fork**, i.e. code nobody with push access to the repo has vouched for.
Everything else warns and proceeds, since once the container runs the branch's
code `strict` is an egress allowlist of registries and forges that all accept
uploads — no boundary against that code — while `loose` is the ordinary way a
step installs dependencies. A PR number is not the signal: an internal PR's head
is a branch in the operator's own origin, byte-identical to what
`jailbee new <branch>` clones, and gating one spelling and not the other would only
teach the operator to click through the mount prompt. A new step the baseline
has no counterpart for counts as widening in both cases.

`--yes`/`-y` now covers this prompt too, on top of its existing job of skipping
the "branch already exists" confirmation, and `--no-autostart` skips the branch
config entirely — none of its steps run, so there is nothing to diff or confirm.

With `jailbee new --background`, the ref resolution (including autofetch) and the
whole branch-config check run in the foreground *before* the run detaches, so
the question is asked in the terminal the operator is still at — a detached
worker has no stdin and could only ever answer "no". Declining creates no
container and records no job. The answer is pinned to the commit it was given
for: if the branch moves between confirmation and provisioning, the worker
aborts naming the move instead of provisioning a config nobody saw. With no
terminal at all, `jailbee new` says so and points at `--yes`.

A branch with no committed `.jailbee/config.yaml` falls back silently to the
checkout's autostart; one that fails to validate, or references an
`optional_mounts` key the checkout doesn't define, warns and falls back the
same way. `--mount` and `--no-clone` are unaffected — they share the host
working tree, so there is no distinct target branch. `jailbee start`, `jailbee
restart`, and `jailbee apply` are unaffected too: only container creation reads
the branch.

### Deprecated

JailBee was called `gie` (`gisgro-incus-env`) before this release. Six
pieces of pre-1.0 compatibility exist so that an install from before the
rename keeps working while it migrates, and all six are removed in
**1.1.0**. See [`docs/migrating-from-gie.md`](docs/migrating-from-gie.md)
for the full migration guide (what `jailbee migrate` does, what it refuses
to do, and how to upgrade).

- The **`gie` console script** — an alias for the same `jailbee` entry
  point, installed alongside `jailbee` and `jb`.
- The **`.gie/config.yaml` fallback** — `jailbee` still reads a repo's
  config from `.gie/` if `.jailbee/` doesn't exist, with a one-time
  deprecation warning naming the `git mv` to run.
- **`claude.install_gie_skills` as a config alias** for
  `claude.install_jailbee_skills`.
- The **legacy `/etc/hosts` sentinel** — `jailbee net refresh` still
  recognizes the pre-1.0 `# BEGIN/END gie-managed allowlist` markers left
  by containers it hasn't migrated yet, so it replaces the old block
  instead of leaving it behind.
- The **`<data>/gie` compatibility symlink** — `jailbee migrate` leaves
  `~/.local/share/gie` pointing at `~/.local/share/jailbee` after moving
  it, because Incus disk devices store absolute source paths. `jailbee
  apply` rewrites the profile-level ones; per-container devices are
  attached once at creation and never refreshed, so a container created
  before the rename relies on the symlink for as long as it lives.
- **`jailbee migrate` itself** — the one-shot command that moves a pre-1.0
  install's host directories, container labels, git refs, systemd units,
  shared bridge, registry mirror, and bundled skills into the `jailbee`
  namespace. It repoints each repo's `<prefix>-net-loose` profile at
  `jailbee-loose` and deletes `gie-loose` (renaming is impossible while a
  profile references it), and refuses — naming both paths — rather than
  skipping a directory move whose target already exists.
