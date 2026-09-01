# Changelog

## Unreleased

### Changed

- **The dashboard title row no longer jumps.** The `⟳` marker that toggled
  on and off with every gather re-centred the whole title, and the refresh
  timing lived in the subtitle. The title is now left-aligned and ends in a
  fixed-width refresh field (`↻ 12s/3s`, age clamped to two digits), so its
  width no longer changes between frames; the subtitle carries a transient
  notice and nothing else.

### Added

- **The TUI dashboard now names itself in the terminal's title bar.**
  `🐝 <repo>/<container>` follows the selected row, so a window switcher
  or a tab strip says which repo you are looking at instead of `bash`. The
  terminal's own title is pushed on the xterm title stack at launch and
  restored on quit. The Qt window's title gained the same bee.
- **`jailbee tui` launches the terminal dashboard**, mirroring `jailbee gui`
  for the Qt one. `jailbee dashboard` is unchanged.
- **`jailbee pr --pr N` binds a container to an existing PR by number.**
  JailBee could already adopt a PR it found for the container's branch, but
  that lookup is by name — it finds nothing when the container's branch is
  named differently from the PR's head branch, and the run then opened a
  *second* PR for the same work. `--pr N` names the PR outright: same
  confirmation and same `pr` / `pr_branch` / `pr_adopted` labels (so the
  foreign-head guards stay on), but a closed/merged or fork PR is refused
  rather than falling through to a new PR — an explicitly named PR is not
  something to silently replace. Re-running with the same number is a no-op;
  a different number asks before retargeting, which is also how a mistyped
  number is corrected. `--pr` with `--as` is a usage error.
  `jailbee submodule pr --pr N` does the same for one submodule, resolved
  against the submodule's own repo and remote.
- **Both dashboards can create containers.** In the TUI, `n` asks for a
  branch and a base branch and runs `jailbee new` in the handed-over
  terminal; in the Qt dashboard, `&Container → New…` (Ctrl+N) and the
  repo-group context menu open a dialog and launch the same command in a
  terminal window. The base branch is a field, pre-filled with the branch
  that repo's host checkout is on, because `jailbee new` forks a new branch
  off `default_branch` when no base is given. Everything else — network,
  memory, cpu, mount, autostart — comes from the repo's config, as it does
  for a bare `jailbee new <branch> <base>`. Neither front-end passes
  `--yes`: `jailbee new` keeps asking about branch reuse and about a branch
  autostart config that widens network access.

## 1.2.2 - 2026-08-28

### Fixed

- **A polluted cache pool root is now resolvable from the command line
  instead of by hand.** When a pool root (e.g. `caches/gradle`) held both
  `slots/` and loose cache content, every command that boots a container
  refused with "Move or delete the loose entries by hand, then re-run" —
  and there was no supported way to do it. `jailbee apply` and `jailbee
  init` now offer to move the loose entries to a timestamped sibling
  directory, which unblocks the pool without deleting anything: a pool root
  is a cache directory, but `~/.gradle` also holds `gradle.properties` and
  `init.d/`. `--yes` never moves anything.
- **`jailbee apply` no longer offers a restart that cannot succeed.** With a
  pool root still unresolved, every restart hit the same error once per
  container, after the user had already been shown it. Apply now says so in
  one line and skips the restarts.
- **`jailbee new` fails before creating anything, not half-way through.** An
  unresolved pool root surfaced from `allocate_startup`, by which point the
  container existed with its GUI sockets and port forwards attached — and in
  background mode it arrived as a traceback in a log file. `new`, `start`
  and `restart` now check the pools up front, in the terminal the user is
  still sitting at. `jailbee doctor`'s advice for this state is true again.
- **A restart failure no longer leaves the upgrade hint nagging.** `jailbee
  apply` recorded its watermark only when every restart and port forward had
  also succeeded, so `jb ls` went on advising an `apply` that had in fact
  written the profiles, ACL, `/etc/hosts` and dockerd proxy. Those failures
  are still reported and still exit non-zero.

## 1.2.1 - 2026-08-28

### Fixed

- **The upgrade hint no longer attributes a release's changes to an older
  version.** When jailbee has never observed `jb base build` (or `jb apply`)
  run in a repo, its watermark is the version the repo was *first seen* under,
  and that assumption survives later upgrades. The hint printed that watermark
  as the release that changed things, so 1.2.0's golden-image changes were
  announced as "jailbee 1.1.0 changed what `jb base build` produces". The line
  now names the releases the listed reasons actually come from.

## 1.2.0 - 2026-08-28

### Added

- **`jailbee claude` switches which stored Claude login a repo's containers
  use.** `jailbee claude park` stores the login in use and empties the holder
  — the credential group directory, or the repo's own config home when it
  shares none — so the next `claude` in a container prompts `/login` and a
  second account enters the pool. `jailbee claude use <email>` then swaps
  between them: the live credential is parked, the target is activated, and
  every member repo's recorded account is repointed at it. The account record
  travels *with* the grant — parking copies Claude Code's own `oauthAccount`
  into the parked file, activating writes it back — so `jailbee claude ls`
  names the live account straight away and two switches in a row keep both
  filenames. Without a record to restore (a login that entered through
  `/login`, never parked by jailbee) the field is cleared instead and Claude
  Code repopulates it from the credential it now finds. A running Claude session adopts the new
  login on its next turn — the credential file's mtime is what invalidates its
  cached token — so nothing needs restarting; only the account name shown in
  `/status` can lag. `jailbee claude ls` lists the store with the live account
  first, `jailbee claude rm` deletes one for good. Both `use` and `rm` take the
  account as an *optional* argument: run either bare and it offers the stored
  logins as an arrow-key menu, like `jailbee shell`/`jailbee tmux` do for
  containers, with TAB completion over slot names when you'd rather type. The switch runs under Claude
  Code's own advisory locks, so a concurrent token refresh cannot overwrite it,
  and it carries the machine-shared credential keys (`mcpOAuth`,
  `pluginSecrets`, …) across from the live file rather than restoring the
  target's stale copies. A login is always **moved**, never copied: two copies
  share one refresh-token lineage and the first rotation would silently log
  the other out. No `cswap`, no database table, no golden-image rebuild —
  the store is `<xdg_data_home>/jailbee/claude-credentials/_parked/` and the
  filesystem is the only state. `jailbee doctor` reports the live account and
  the parked count once anything is stored.
- **Several repos on one host can now share a single Claude Code login.**
  `claude_credentials` in `~/.config/jailbee/global.yaml` names a `group`
  every repo on the host defaults into, with a per-repo `repos:` map
  (keyed by `container_prefix`) to override it — an explicit `null` there
  opts one repo out while the rest of the host still shares. The block is
  host-level only; setting it (or the derived `claude_credentials_dir`) in a
  repo's committed `.jailbee/config.yaml` is rejected, since a group name is
  a property of this one machine. `jailbee apply` does the work: it creates
  `<xdg_data_home>/jailbee/claude-credentials/<group>/` and **moves** this
  repo's stored credential into it. When the repo *and* the group already
  hold one, only one login can be shared and the other becomes unused, so
  `apply` asks which to keep — the group's, this repo's (which re-points
  every member repo), or cancel — and deletes the loser; the two are
  independent grants, so nothing the survivor depends on is touched. Without
  a TTY to ask on, it refuses rather than silently discarding a login. Only
  the credential is shared — each repo keeps its own `~/.claude`, so
  project history, MCP config and sessions stay per-repo — and no golden
  image rebuild is needed: the mount and the
  `CLAUDE_SECURESTORAGE_CONFIG_DIR` env var reach existing containers on the
  next `jailbee apply`. `jailbee doctor` reports the group, its directory,
  and the other member repos, and catches the one broken state (group
  configured but `apply` not yet run). A `global.yaml` generated by
  `jailbee config init --global` now ships `group: default`, so a fresh host
  shares one login with no configuration at all; an existing `global.yaml` is
  never rewritten, so hosts that predate the key keep every repo on its own
  credential until they opt in. Turning sharing on for a host whose repos are
  already logged in is a migration — every `jailbee apply` after the first
  asks which login to keep — which is exactly why the default lives in the
  template rather than in the schema.
- **`jb setup` installs the post-install steps a package install cannot.**
  Shell completions for both console scripts, the `jailbee-net-refresh` user
  timer, and jailbee's Claude Code skills in `~/.claude/skills` used to be
  inlined in this repo's `make install`, which meant a `uv tool install
  jailbee` never performed them and nothing said so. `jb setup` is the
  machine-level counterpart to the per-repo `jb init`: interactive by default
  (one question per step, defaulting to yes for a missing step and no for one
  already in place), `--yes` for scripts, `--only` to pick steps and
  `--shell` to override shell detection. It needs no repo config, so it works
  from the directory a fresh install starts in, and every step is idempotent
  — re-run it after upgrading. Completions go where each shell already looks,
  so bash and fish need no rc edit; zsh's `compinit` line is offered
  interactively and only ever printed under `--yes`. Host prerequisites
  (Incus, the firewall, UID delegation) are deliberately out of scope: the
  command ends by pointing at `jb doctor` and
  [docs/installation.md](docs/installation.md).
- **The commands you run daily say once when a setup step is missing.**
  `jb ls`, `jb new` and `jb shell` print a one-shot hint on stderr naming the
  missing steps and `jb setup`. It fires at most once per machine — running
  `jb setup` silences it too, declined steps included — so a long-time user
  whose install predates this sees it once and never again. `jb doctor` is
  where the state stays visible: it grew `shell completions` and
  `claude skills (host)` checks (the latter only when the Claude integration
  is enabled), and its inactive-timer advice now names `jb setup` rather than
  `jb init`, which is per repo and refuses to run twice.
- **`jailbee net egress ls|add|rm|export`** (short alias `jailbee egress`)
  widens one container's, or this host's copy of the repo's, strict-mode
  allowlist without editing committed config. `add`/`rm` default to
  container scope (the entry lives in the container's own
  `user.jailbee.egress_extra` label and dies with it); `--repo` scopes to
  every container of the repo on this host instead (host-local state, not
  in git). Both materialise against the container's **current** network
  mode — adding to a `loose` container stores the label but changes no ACL
  until it returns to `strict`. Overrides are additive only: `rm` refuses
  an entry that exists only in `config.yaml`, pointing at the file, but
  removes one that is *also* stored as an override, which is what makes
  the promote-then-clean-up workflow below actually work. `ls` shows every
  applicable entry with its source; `export` prints a complete replacement
  for the config's `egress_allow:` key (existing entries plus promotable
  overrides) to paste over the whole key rather than append — a second
  `egress_allow:` key would make `yaml.safe_load` silently keep only the
  last one. `jailbee net status` gained a section listing every
  egress override on the host.
- **jailbee now tells you when a release needs `jb base build` or `jb apply`
  re-run.** Some releases change what the golden image contains
  (`provision/install.sh`, `install.d.available/` snippets, provisioning env)
  or what `jb apply` writes (profiles, ACL); neither is re-run automatically,
  so a user who upgrades the tool could keep a stale image or stale profiles
  indefinitely with nothing to say so. Each release now declares its
  requirements in `UPGRADE_NOTES`, jailbee records per repo the version at
  which `jb base build` and `jb apply` last ran, and the difference is
  reported as a non-blocking hint on stderr from `jb ls`, `jb new` and
  `jb shell`, plus an `upgrade actions` check in `jb doctor`. The hint names
  the reason, not just the command, and repeats until the action actually
  runs — a partly failed `jb apply` (a restart or port-forward failure) does
  not count as having run. Repos are backfilled silently on first sight, so
  no backlog is invented for history jailbee never observed; only the
  release that introduces the tracking can advise about itself. An install
  whose version jailbee cannot read at all (it reports `0.0.0+unknown`, which
  means the package metadata is missing) is silently excluded — there is
  nothing to compare. Editable installs are not excluded: they report the
  version in `pyproject.toml` and take part like any other.
- **Generic agent support: `agents:` config key.** Claude Code's integration
  — shared credentials/settings mount, strict-mode egress, install/update at
  `jailbee new` time, autostart launch, `jailbee doctor` check — is now a
  declarative mapping any terminal coding agent can use, not code specific to
  Claude. Six presets ship (`claude`, `codex`, `gemini`, `aider`, `opencode`,
  `grok`); enabling one is usually two lines
  (`agents: {codex: {enabled: true, autostart: true}}`), and every preset
  field is overridable. Only `claude` is exercised in production — the other
  five are untested templates whose package names, config paths and
  especially host lists are best-effort, corrected by whoever adopts one. An
  agent name outside the shipped six works too, with no preset base. See
  [docs/agents.md](docs/agents.md).
- **`agents.claude` is the preferred spelling** of Claude's config block. The
  existing top-level `claude:` block remains a supported legacy alias —
  translated into `agents.claude` at load — and defining both at once is a
  `ConfigError`.
- **`jailbee submodule pr [CONTAINER] [PATH]`** opens or updates a pull
  request in a submodule's own GitHub repository, for commits made inside
  that submodule in a container. A submodule is a separate repo, so it needs
  its own PR — `jailbee pr` publishes only the superproject branch and the
  two commands are independent, with one PR produced per run. Target
  selection is automatic when exactly one submodule has commits ahead of its
  own base anchor (`refs/jailbee/base/<super-base>`, pinned at container
  creation) — deliberately not the superproject's gitlink diff `jailbee ls`
  uses, so commits made inside a submodule are seen even before the gitlink
  bump lands in the superproject; several candidates are listed and require
  `PATH`. Base and head branch names are resolved from the submodule's own
  data (`.gitmodules`, its own remote's `HEAD`, or Claude's proposal when
  `claude.ai_pr_branch` is on), not inherited from the superproject, and the
  chosen head is remembered per submodule path so a re-run updates the same
  PR. Flags mirror `jailbee pr` (`--title`, `--body`, `--base`, `--as`,
  `--ready`/`--draft`, `--description`, `--no-ai`, `--force`, `--yes`,
  `--web`, `--open`), with `--branch/-b` repointed to mean "read from the
  submodule" rather than the superproject. The whole decision matrix
  (AI title/branch generation, PR adoption, foreign-head guards, outcome
  rendering) is shared with `jailbee pr` via a new internal `pr_flow` module
  — `jailbee pr`'s own behaviour is unchanged. See
  [Submodule pull requests](docs/git-bridge.md#submodules).
- TUI dashboard: fold repo groups with `Space` (or `Enter` on a group
  header), and a settings overlay on **F2** / `S` for columns and folding.
  Repo headers are now cursor stops.
- GUI dashboard: **View ▸ Columns**. The two front-ends keep independent
  settings.
- **BREAKING: `jailbee submodule checkout -b <branch>` now switches the host
  superproject too.** It used to align the submodules and leave the
  superproject wherever it was, so moving the whole tree took two commands
  (`git checkout <branch>`, then this one) and a bare `-b` quietly produced a
  superproject/submodule mismatch instead. `-b` now means "put the tree on
  this branch": the superproject checkout runs first — it is what rewrites the
  gitlinks the alignment places branches at — and a refused checkout (dirty
  tree, unknown branch) fails without touching the submodules. Pass
  `--submodules-only` for the old behaviour; it is also the way to align
  submodules from a detached HEAD or to keep a deliberate mismatch. A
  container's branch is its identity, so `jailbee submodule checkout <name> -b`
  is unchanged: pure submodule placement, no branch switch.
- **BREAKING: Claude Code's `~/.claude.json` moved inside the shared `~/.claude`
  mount.** The golden image now exports `CLAUDE_CONFIG_DIR=$HOME/.claude`, so
  Claude Code reads and writes `~/.claude/.claude.json` instead of
  `~/.claude.json`, and the separate file-level bind mount
  (`<shared_dir>/claude.json`, Incus device `claude-json`) is gone. A
  file-level bind cannot survive an atomic rewrite: temp-file + rename
  replaces the inode and the container keeps the old, unlinked one — so any
  host-side write to that file was invisible inside the container.
  **To upgrade:** run `jailbee base build` (for the new environment variable)
  and `jailbee apply` in each repo (to move the existing file and drop the
  device), then re-create containers from the new image. `jailbee apply` moves
  `<shared_dir>/claude.json` to `<shared_dir>/claude/.claude.json`
  automatically; it never overwrites an existing destination.
  `CLAUDE_CONFIG_DIR` is also set as a base-profile environment variable, so
  between `jailbee apply` and re-creating a container, existing containers
  already get it on every `incus exec` — no window where `claude` writes to
  the wrong path, even before the image is rebuilt or the container re-created.
  `jailbee new` performs the same migration in a repo that has not been
  re-`apply`ed yet, so the pre-move file is never orphaned by a fresh
  container onboarding into the destination first.
- **The Claude install/update step now runs bounded, with its output kept.**
  It used to run through an unbounded `incus.exec` call; it now runs through
  the same autostart step pipeline every other agent's install/update uses,
  bounded by `autostart.step_timeout` (default 600s) and landing in a
  persistent tmux window instead of being captured and discarded. A stuck
  install is therefore capped at `autostart.step_timeout` instead of hanging
  `jailbee new` indefinitely, and its output is inspectable afterwards via
  `jailbee tmux`.
- **Every agent install/update command runs in its own `bash -c` child
  shell.** A command spanning multiple lines — which every bundled install
  script and any user `install: |` block scalar is — used to be pasted
  verbatim into the shell line that records the step's exit code, so a `set -e`
  trip, an explicit `exit`, or just the script's trailing newline stopped that
  bookkeeping from ever running. The step then sat until
  `autostart.step_timeout` elapsed and reported a timeout regardless of what
  actually happened.
- **`ensure_agents` starts the container's autostart tmux session even under
  `--no-autostart`.** Agent installs are infrastructure rather than user
  autostart commands (the same reasoning `inject_github_token` already
  follows), so they run either way and need the session to run in. Visible
  consequence: `jailbee tmux` on a `--no-autostart` container now finds a
  session with an `install-<agent>` window in it rather than nothing.
- **The Docker registry mirror is now opt-in by detection.**
  `docker_registry_mirror.enabled` defaults to `auto`: the mirror is wired only
  into repos that ask for it — a golden image that would contain Docker
  (`golden.stacks.docker`, an `enable_snippets` / `install.d` `50-docker`, a
  `golden.extra_apt_packages` entry starting with `docker`, minus
  `disable_snippets`), a non-empty per-repo
  `docker_registry_mirror.extra_registries`, or `golden.stacks.ecr`. Repos
  without any of those no longer need the mirror container to exist at all. Set
  `enabled: true` in `~/.config/jailbee/global.yaml` to force the previous
  behaviour for every repo; `false` still disables it everywhere.
  **Upgrade note:** if a repo's image has Docker by a route jailbee cannot
  detect — a differently-named `install.d` snippet, a custom
  `golden.provision_script` without `golden.stacks.docker`, or Docker installed
  by hand inside a container — its strict containers lose the `/etc/hosts`
  mirror pin and the ACL rule on the next refresh while their dockerd keeps
  proxying to the now-unresolvable mirror host, so `docker pull` inside the
  container fails. Set `docker_registry_mirror.enabled: true` (or declare the
  stack and rebuild the golden image).
- The dashboards no longer show **IP** by default, matching `jailbee ls`.
  MEM remains the one deliberate difference between the two default column
  sets. Enable IP in the dashboard settings (F2 / View ▸ Columns) or ask for
  it with `ls --fields ip`.
- **`dashboard:` in `global.yaml` and `.jailbee/config.yaml` is deprecated.**
  Each dashboard front-end now remembers its own columns, editable in the UI.
  Your global block is imported into each front-end's own settings the first
  time you open that dashboard after upgrading; the key is still accepted but
  ignored, and can be deleted once both the TUI and the GUI have been opened.
  **A repo-level `dashboard:` block is dropped, not imported** — the setting
  is personal and cross-repo, so only the global file is read this way.
  `jailbee config validate` reports both, and — like the existing
  `golden.python` deprecation — this now makes `config validate` exit 2 for a
  config that previously validated clean, so upgrading can turn a green CI or
  pre-commit hook red until the block is removed.
- **Generic cache pools: `pooled_caches`, and Gradle/Maven join Chrome as
  per-container instead of shared.** A repo's containers used to share one
  `~/.gradle` (and one `~/.m2`) through a single profile-level bind mount, so
  Gradle and Maven's own inter-process lock on the cache directory was
  shared across containers too — a build in one container made every other
  container's build wait on that lock, or fail once it timed out, which in
  practice surfaced as an agent process timing out inside a container. The
  mechanism `chrome_pool.py` already used for Chrome's profile is now
  generic (`pool.py`), and applies to any cache named in the new
  `pooled_caches: {name: bool}` config key or carrying its own `pool:`
  block on a `shared_caches` entry (`SharedCache.pool`). `POOL_PRESETS`
  ships built-in specs for `gradle`, `m2`, `npm`, `pnpm-store` and
  `chrome-profile`; `gradle`, `m2` and `chrome-profile` default on, while
  `npm` and `pnpm-store` ship a preset but leave pooling to an explicit
  `pooled_caches: {npm: true}` / `{pnpm-store: true}`. A pooled cache is
  not a shared mount: each container gets its own slot directory under
  `<shared_dir>/<host_subpath>/slots/`, attached as a disk device named
  `<cache name>-slot`, allocated at container creation and on every boot (or
  on demand, for Chrome) and released at `jailbee destroy`. A fresh slot is
  seeded by copying the warmest existing slot, with each preset's
  `link_paths` subtrees hardlinked instead of copied — a multi-gigabyte
  Gradle module cache or Maven repository costs almost nothing per extra
  container this way. `link_paths` may only name subtrees written once and
  later deleted whole, never rewritten in place: hardlinking a lock file
  would restore exactly the cross-container sharing pooling exists to
  remove. `jailbee pool ls [NAME]` / `jailbee pool prune [NAME]` replace
  `jailbee chrome-pool ls/prune`, which remains a hidden, deprecated alias
  scoped to the Chrome pool; `pool ls`'s footer prints the deduplicated
  on-disk total, since summing per-slot sizes counts every hardlinked file
  once per slot and over-reports severalfold. `jailbee init` and
  `jailbee apply` create the pool layout and migrate a pre-existing cache
  sitting directly under the pool root into `slots/slot-0`, so it stays warm
  as the first seed rather than being discarded; a pooled cache attaches
  when a container next boots, so restart any container that was running
  during `jailbee apply` before trusting it to be using its own slot.
  `jailbee doctor` reports a pool root that still needs migrating.
  See [`pooled_caches`](docs/config.md#pooled_caches).

### Fixed

- **A container boot no longer hijacks the host's gpg-agent and PulseAudio
  sockets.** The `gpg-socket` and `pulse-socket` devices bind-mount the host's
  own `/run/user/<uid>/gnupg` and `/run/user/<uid>/pulse` *directories* into
  the container, and the golden image enables linger — so the container runs
  its own `systemd --user`, whose `gpg-agent.socket`, `dirmngr.socket` and
  `pulseaudio.socket` listen on paths *inside those mounts* and unlink
  whatever file is already there before binding. Every container boot
  therefore deleted the host's live sockets: the host gpg-agent logged
  `socket file has been removed - shutting down` and the container's agent
  answered in its place — with no smartcard access, so a YubiKey silently
  disappeared from `ssh-add -l` on the *host*, mid-session, until the
  container's agent was killed and the host's socket units restarted by hand.
  Both devices are now mounted read-only, which turns that unlink into
  `EROFS`; connecting to a unix socket needs no writable filesystem, so agent
  forwarding and audio are unaffected, and the mount is read-only from the
  container's side alone, leaving the host free to re-create its own sockets.
  `install.sh` additionally masks those user socket units (`systemctl
  --global`), so the container does not even try and no failed units
  accumulate in its user session. The read-only flag is what covers gpg's own
  agent autostart, which never goes through systemd. Existing containers pick
  the flag up on their next `jb start`/`jb restart` (every boot path detaches
  and re-attaches these devices); the masking needs a `jb base build`.

- **A successful `jb start`/`jb restart` clears the failed boot record it
  supersedes.** A background boot that failed leaves a `failed` job row
  behind, and only `jb job clear` used to remove it: a foreground
  `jb restart` brought the container back up, ran autostart, and left
  `jb ls` still flagging the container while the attach guards kept
  pointing at `jb job clear`. A foreground boot that completes (autostart
  included) now clears that row itself. Only *boot* records: a failed
  `jb new` means the container's setup — clone, credential wiring, first
  autostart — never finished, which a reboot does not complete, so its
  record survives to keep saying so, and `jb job clear` stays the way to
  acknowledge it. A live job's record is left alone too, since its worker
  is still writing to the container. Background boots already behaved
  this way; the row is overwritten on spawn and deleted on success.
- **`jb doctor` reports when the refresh timer runs a different jailbee.**
  `install_systemd_units` bakes `which("jailbee")` into
  `jailbee-net-refresh.service` at install time and rewrites the unit only
  when its rendered content changes, so a path that stops being the current
  install sticks — and that unit fires every minute, unprompted, carrying the
  `loose` TTL auto-revert with it. A stale one therefore means old code on a
  schedule (which is how the state database used to get reset) and a TTL that
  quietly stops honouring `jb net loose --for`. The new `net refresh binary`
  check compares the unit's `ExecStart` with the `jailbee` on PATH and points
  at `jb init` when they differ. It reports nothing when there is nothing to
  compare — no unit (the timer check already says so) or no `jailbee` on PATH
  (a `uv run` dev invocation is not a broken unit).
- **The state database is opened once per process, not once per call.**
  `get_engine` built a fresh engine — new connection pool, full
  `_ensure_schema` including `create_all` — on every call, and the dashboards
  call it on every refresh tick. Besides the per-tick cost, that is what gave
  the reset above its reach: a dashboard left open across an upgrade kept
  re-asserting its own (older) idea of the schema over a database a newer
  jailbee had already migrated, several times a minute, for as long as the
  window stayed open. The engine is now cached per database path for the
  lifetime of the process, so the schema is checked once, at startup: a stale
  process serves the schema it started with and simply does not see rows and
  tables added since, until it is restarted.
- **An older jailbee no longer wipes the state database.** `_ensure_schema`
  reset the whole database whenever the on-disk schema version was one it
  could not reach by forward migration — which includes the everyday case of
  running an older jailbee against a newer database (a rollback, or a
  maintainer moving between branches). The reset was justified with "pool data
  is regenerable from DNS", but it dropped every table: `registered_repo` in
  particular is the dashboard's only way to map a container back to its repo
  and the refresh timer's only work list, and nothing rebuilds it. The visible
  result was a dashboard where every repo except the current directory's
  rendered as a view-only `(orphan)` group, plus egress pools that quietly
  stopped being refreshed — with no error anywhere. A newer database is now
  used as-is (migrations are additive, so it is a superset) and its version is
  left alone. A genuine gap in the migration chain still resets, but copies
  the database aside first — `state.sqlite.bak-v<version>`, via SQLite's own
  backup API so WAL contents come along — and logs where it went. Re-register
  a repo the old behaviour dropped by running `jb apply` in it.
- **A background `jb new` no longer dies with its job row.** Every phase
  update, failure record and cleanup of a detached `new`/`destroy` worker went
  straight to the state database with nothing catching a write failure. So the
  reset above cost more than bookkeeping: an older jailbee dropping
  `background_op` under a live worker ended the whole operation with
  `sqlalchemy.exc.OperationalError: no such table: background_op` in the job
  log — before the container was even created — and the fix for the reset
  cannot help here, because the process doing the dropping is the *old* build
  still running. Job rows are bookkeeping for `jb ls` / `jb job`, not part of
  the container, so each write is now guarded: a failure warns on stderr, into
  the job log where the traceback used to be, and the create or destroy
  carries on. The same guard covers the foreground's own insert (which runs
  after the worker is already spawned) and the engine lookup itself, so an
  unwritable state dir costs tracking instead of the container.
- **Claude Code no longer re-onboards in every new container after the
  `.claude.json` relocation.** The relocation has two halves: `jailbee new`
  retires the `shared-claude-json` device (the whole repo's, since it rewrites
  `<prefix>-binds`), while the replacement — `CLAUDE_CONFIG_DIR`, pointing
  Claude Code at the shared `~/.claude` mount — was written only by
  `jailbee apply` and `jailbee base build`. Upgrading jailbee and carrying on
  with `jailbee new` therefore left neither in place: Claude Code resolved the
  container-local `$HOME/.claude.json`, found nothing, and asked for a login
  in every container, each time. The upgrade advice is a non-blocking hint, so
  nothing stopped it. `jailbee new` now sets `environment.CLAUDE_CONFIG_DIR` on
  `<prefix>-base` when nothing declares it yet — one key, not a profile
  rewrite, and never over an existing value, so a `container.env` override and
  `jailbee apply`'s own rendering both stay authoritative. Incus injects
  `environment.*` into every `incus exec` whatever the image holds, so this
  repairs the repo's existing containers too, with no rebuild. `jailbee apply`
  (and `jailbee base build`, for shells jailbee does not spawn) remain the
  documented upgrade actions.
- **`jailbee pr` no longer looks like it hangs on the container fetch.** The
  fetch summary and the dirty-tree warning are now printed *before* the push
  to the upstream remote, followed by an explicit
  `Pushing '<branch>' to origin…` line. `git push` inherits its output and
  prints nothing until the remote answers, so a push blocked on remote
  authentication left git's own fetch output as the last thing on screen and
  read as a stalled fetch — several steps earlier than where the command
  actually was. The container-side `git status --porcelain` probe that runs
  between the two is also bounded by a 60-second timeout now, so a stalled
  `incus exec` reports instead of waiting forever.
- **`claude` no longer breaks in one container when another container
  updates it.** The Claude version store (`~/.local/share/claude/versions`)
  is a bind mount shared by every container of a repo, but
  `~/.local/bin/claude` is a per-container symlink pinned to one exact
  version at `jailbee new` time — and Claude's own updater prunes old
  releases from the shared store. A `claude update` in any container could
  therefore delete the version another container pointed at, leaving a
  dangling launcher (`-bash: .../claude: No such file or directory`) that
  never healed, because the relink only ran at `jailbee new`. The golden
  image now ships `/etc/profile.d/jailbee-claude.sh`, which repoints a
  missing or dangling launcher at the newest usable release in the store on
  every login shell — the path every in-container `claude` invocation takes.
  A healthy pin is left alone, so `claude.auto_update: false` keeps its
  version. Requires a base-image rebuild (`jailbee base build`); existing
  containers can be repaired in place with
  `ln -sfn ~/.local/share/claude/versions/$(ls -1 ~/.local/share/claude/versions | sort -V | tail -1) ~/.local/bin/claude`.
- **A stopped or missing registry mirror is no longer fatal.** `jailbee init`
  (which the docs tell you to run *before* `jailbee registry up`) and
  `jailbee apply` now warn and continue instead of aborting, and the background
  egress refresh logs instead of raising. `jailbee start` / `jailbee restart`
  never aborted on this and still don't: they skip the `/etc/hosts` mirror pin
  silently. `jailbee doctor` no longer reports a red mirror line for repos that
  don't use Docker, and one repo's failed refresh no longer skips every other
  registered repo in the same cycle. `jailbee new` still refuses in strict mode,
  where the mirror is the container's only route to Docker Hub.
- **`jailbee net strict` warns when the mirror a repo wants is unavailable.**
  Going strict is what removes the container's direct route to Docker Hub, so
  the switch is the last point at which the remedy
  (`jailbee registry up && jailbee apply`) can still be named — previously it
  was silent, and a container created with `--network loose` while the mirror
  was down became a strict container with a broken `docker pull` and no
  indication why.
- **`jailbee net refresh` exits non-zero again when a repo's refresh raises.**
  The new per-repo error handling in the 60-second refresh loop dropped the
  failed repo from the results, so the command (verbatim the systemd unit's
  `ExecStart`) printed nothing and exited 0. Failures are now recorded as an
  `error` result and reported as `FAIL`.
- **Listing a container no longer takes git's index lock.** The git-status
  probe behind `jailbee ls`, the `jailbee git push` picker and both dashboards'
  periodic refresh only reads — but `git diff`, `git diff --cached` and
  `git submodule foreach 'git diff'` refresh the index and write it back, which
  takes `.git/index.lock`. A refresh landing on the same container as a write
  therefore makes the write fail — the shape of a failure seen in the wild,
  where `jailbee git push --merge` died with `error: Unable to create
  '/home/dev/<repo>/.git/index.lock': File exists` mid-fast-forward and
  succeeded on an immediate retry. The probe and the dirty-tree preflight now
  run with `GIT_OPTIONAL_LOCKS=0`, so they neither take the lock nor fail on
  one — at the cost of not persisting the refreshed stat cache, which nothing
  here reuses.
- **A container-side `git merge` / `rebase` / `reset --hard` blocked by
  `.git/index.lock` is retried instead of reported.** These refuse to start
  while another git process holds the lock, and they refuse before changing
  anything, so `jailbee git push` now retries three times with a linear backoff
  — enough for the short-lived git command that causes the contention in
  practice (an editor, a coding agent, another `jailbee` invocation). A lock
  that outlives the budget is reported as what it is, naming the lock file and
  how to inspect it, rather than as a wall of `incus exec` command line plus
  git's own advice.

## 1.1.0 - 2026-08-20

### Added

- **The workflow commands are in both dashboards' action menus.** `pr` (create
  or update the PR — not just `pr --open`, which only views one), `git push`
  ("update from base"), `git pull` ("send commits to host"), `git diff` and
  `job log` were reachable only from the command line, which meant leaving the
  view that told you they were needed. Every entry is gated on whether it would
  do something: the PR and git-bridge verbs need a running clone-mode container,
  `git pull` needs commits ahead of the base, `git diff` needs something to
  show, `job log` needs a job row. A git status that is merely unknown — under
  `--no-git`, or before the first git-tier refresh — hides nothing, because a
  missing column is not evidence of a clean tree.
- **Output from those commands survives long enough to read.** In the TUI
  `git diff` opens in `$PAGER` (`less -R`, then `more`) with colour forced past
  the pipe, and `pr`/`git push`/`git pull`/`job log` wait for Enter before the
  dashboard repaints over them. Their own prompts keep working, since the TUI
  hands over the real terminal.
- **`jailbee git diff --color/--no-color`** to force colour either way. The
  default still follows stdout; the dashboard's pager path needs the override
  because a pipe is not a TTY.
- **The Qt dashboard runs those commands as a GUI, not in a terminal.** Only
  `shell` and `tmux` still open a host terminal emulator, because they need a
  real TTY. The printing verbs stream into a JailBee window with Stop and Copy
  buttons and the exit code on its status line — non-modal, so the dashboard
  keeps refreshing behind it and several commands can be watched at once. Stop
  exists because `job log` on a live job follows the worker's log until it is
  stopped. The questions those commands would ask on a TTY are asked up front in
  Qt dialogs and passed as flags, since the GUI's child process has no stdin:
  `git push`'s merge/rebase choice (only when the repo's `push.default_action`
  is `ask`), `pr`'s draft/description/adoption choices, and a confirmation for
  `git pull`, which is the one bridge command that writes to the host's own
  working tree.
- **"Refresh from PR head" in both dashboards' action menus.** A container
  created with `jailbee new --pr N` falls behind whenever the PR's author
  pushes; the entry dispatches `jailbee git push <name> --pr`, which catches it
  up. It appears only on a review container — one whose PR JailBee did not open
  from its own branch, the `#123↓` case in the PR column — because an authored
  PR's head is downstream of the container and the refresh could only be a
  no-op. Like the base update it sits beside, it needs a running clone-mode
  container. The Qt dialog asks only the merge/rebase/plain question here: `--pr`
  is itself the source, so there is nothing to ask about `push.default_source`.
- **Quick-action keys in `jailbee dashboard`.** `t` attaches tmux, `s` opens a
  shell, `i` the IDE, `c` Chrome, `p` the PR, `P` creates or updates the PR,
  `u` updates the container from its base and `d` shows the diff, straight from
  the highlighted row without going through the action menu. A key only fires
  when that action is one the row's own menu would offer — a stopped container
  has no tmux, the IDE and Chrome follow the repo's `jetbrains`/`chrome` config,
  `P`/`u`/`d` need a running clone-mode container, and orphan rows
  stay view-only — and when it declines, the footer says why rather than going
  silent. `git pull` and `job log` are deliberately menu-only: the first writes
  to the host's own working tree, and the second's command varies with
  `--follow`. `h` (or `?`) opens a keybinding help overlay.
- **`jailbee --version`** alongside the existing `jailbee version` subcommand.
- **`host_ports` config for forwarding a host service into every container.**
  A `.jailbee/config.yaml` block like `host_ports: [{ name: adb, port: 5037 }]`
  attaches an Incus proxy device to every container of the repo, so e.g. a
  host adb server on `127.0.0.1:5037` is reachable inside the container
  without an `ADB_SERVER_SOCKET` override. Only the host-to-container
  direction is configurable here — a host-side listener is machine-wide, so
  declaring the reverse per repo would make the repo's containers fight over
  it; that direction is `jailbee port to-host`, run per container instead.
  Entries are attached at `jailbee new` and reconciled by `jailbee apply` —
  no image rebuild, no container restart. This supersedes the
  adb-over-a-bind-mounted-socket recipe in
  [`project-config.md`](docs/project-config.md#sharing-host-sockets) for the
  common case (a TCP adb server), which remains the way to reach a host adb
  server that only ever speaks over a unix socket — `host_ports` forwards
  TCP/UDP only.
- **`jailbee port` command group.** `jailbee port to-container PORT [NAME]`
  makes a host service reachable inside the container (the adb case);
  `jailbee port to-host PORT [NAME]` is the mirror, making a container
  service reachable on the host (`--host-port auto` picks a free host port
  and prints it). Both take the container-side port as the positional
  argument and `--host-port` for the host side — there is no `HOST:CONTAINER`
  syntax. `jailbee port ls [NAME]` lists every forward on a container (or
  every container of the repo), including one added directly with `incus`.
  `jailbee port rm HANDLE [NAME]` removes one by device name, config entry
  name, or container port. A forward bypasses the network ACL by
  construction, so it works the same in `strict` and `loose`, and shows up
  in its own section of `jailbee net status`.
- **`claude.pr_prompt` — a project's own PR standard.** `jailbee pr` generated
  its title and body from a prompt hardcoded in JailBee, so a repo with its own
  conventions had no way to reach the model. Set `claude.pr_prompt` in the
  repo's `.jailbee/config.yaml` (a YAML block scalar) and those instructions
  are embedded in JailBee's prompt as a delimited section that **outranks** the
  generic title/body guidance. It is placed before the JSON response contract,
  which it cannot override — so a project can dictate the shape of its
  descriptions without having to restate, and risk breaking, the format
  JailBee has to parse back.
- **`claude.ai_pr_model`** selects the model used for PR-text generation. An
  alias (`sonnet`, `opus`, `haiku`) or a full model ID; `null` inherits the
  container's own default.

### Changed

- **`--help` renders arguments in Typer's new style.** Typer 0.27 deliberately
  dropped ALL-CAPS metavars, so a usage line now reads
  `jailbee snapshot restore [OPTIONS] {name} {tag}` instead of
  `... [OPTIONS] NAME TAG` — braces for a required positional, brackets for an
  optional one — and the type column in the Arguments panel shows the Python
  type (`<str>`) rather than Click's `TEXT`. Only the help output changed; the
  arguments themselves, their order, and every command's behaviour are
  untouched. JailBee follows Typer's house style here rather than pinning 51
  explicit metavars against it.
- **A failed background job no longer locks you out of the container.**
  `jailbee shell`/`tmux`/`ide`/`chrome` used to refuse outright when the
  container's `jailbee new --background` job had ended in `failed` — the
  common case being an autostart step that errored — and demanded `--force`
  to get in. That inverted the point: the failed container is exactly what
  you asked to look at. The commands now report what failed, name
  `jailbee job clear`, and ask `Continue anyway? [Y/n]` (default yes) before
  attaching. Ctrl-C out of the wait gets a similar offer once the container
  exists, replacing the other half of what `--force` used to do — on stricter
  terms, since an interrupt is an explicit cancel: that one defaults to no,
  is asked even under `--force`, and is skipped without a TTY. `--force`
  survives as "don't ask", the same meaning it already has on
  `jailbee destroy`; a non-interactive stdin is treated the same way. Both
  dashboards pass it, since their JOB column already showed the failure.
  Attaching is still refused, without a prompt, when there is no container to
  attach to or a destroy is actively tearing it down. The autostart failure
  message drops its `--force` hint — it rendered for foreground failures too,
  where no background job exists and the flag never did anything.
- **`jailbee pr` now generates its description on Sonnet**, not on whatever
  model the container defaults to (typically Opus). Writing a PR description is
  a bounded job — read a diff, follow a template, emit JSON — and pinning it
  means the generation no longer competes for the same budget as the coding
  work that just happened in that container. Set `claude.ai_pr_model: null` to
  restore the previous inherit-the-default behaviour, or name any model you
  prefer. `haiku` works, but its smaller context window may not hold a large
  cumulative diff.
- **The AI PR prompt now looks at what the project already documents.** Its
  only nod to project context used to be a single line asking Claude to read
  "any obviously relevant spec, plan, or README file". It now names
  `.github/pull_request_template.md` (and `.github/PULL_REQUEST_TEMPLATE/`) as
  the required shape when the repo ships one, searches named locations for the
  spec or plan the branch implements — describing the change against that
  intent and stating what it deliberately leaves out — reads `CONTRIBUTING.md`
  / `CLAUDE.md` / `AGENTS.md` for PR-writing rules rather than only for branch
  naming, and looks up an issue referenced by the branch name or a commit
  message to add a `Closes #N` line.
- **A failed PR-text generation says why.** "Claude PR-text generation failed;
  using a placeholder" was printed whether `claude` was missing, the run timed
  out, or the configured model does not exist. The underlying reason is now
  reported alongside it.
- **The dashboard's action menu opens inline, under the table.** Pressing
  `Enter` used to hand the terminal to a separate prompt, which took the whole
  dashboard off screen — you picked an action for a container you could no
  longer see. The menu is now drawn below the container rows, which stay
  visible and keep refreshing behind it; `↑/↓` move the menu cursor, `Enter`
  runs the entry, `Esc`/`q` closes it. `Ctrl-C` still quits the dashboard
  outright, even with the menu open. The keybinding hint moved from the panel
  border into the panel itself, where it can wrap instead of being clipped.
- **The submodule conflict report is grouped by what you have to do about
  it.** Every unresolved submodule used to be listed the same way, so one that
  was skipped untouched — a dirty sub-repo needing a stash and a re-run — read
  exactly like one git had left mid-merge, needing `git add && git commit`.
  The block now separates `auto-merged`, `in merge state — resolve these`, and
  `skipped, not touched`, each with a count and a reason in plain terms.
  `jailbee git push --merge` prints the same block as `jailbee pull`; it used
  to format the same information its own way.

### Fixed

- **The golden image no longer ships Ubuntu's automatic apt machinery.**
  `install.sh` masks `apt-daily{,-upgrade}.timer`, their services and
  `unattended-upgrades`. The timer fires within minutes of every boot — that
  is, right on top of the golden build's own apt run and of yours inside a
  branch container — and an upgrade still in flight at shutdown blocks
  systemd, the most likely explanation for a build container that would not
  stop. Masked rather than disabled, so reinstalling the packages cannot
  quietly bring it back. Takes effect on the next `jailbee base build`.
- **A container that will not shut down no longer costs ten silent minutes.**
  Every jailbee stop passed no `--timeout`, and incusd reads that as a
  600-second clean-shutdown budget: a container whose init hangs froze the
  command with no output and then failed with `Failed shutting down instance,
  status is "Running": context deadline exceeded`. In `jailbee base build`
  that discarded a complete, successful provisioning run at the very last
  step, and pointed at `incus info --show-log` for a container it deleted one
  line later. Stops now use a 120-second budget with the elapsed time on
  screen, and an expired budget first asks the still-running container what is
  holding it up — pending systemd jobs, processes in uninterruptible sleep,
  the tail of the console log where `A stop job is running for …` appears.
  Disposable containers (the golden-image build container, the registry
  mirror, anything `jailbee destroy` is about to delete anyway) are then
  force-stopped so the work completes; `jailbee stop` on a container holding
  your own work still reports rather than pulling the plug, and says how to
  inspect it and how to force it.
- **`jailbee exec` finds per-user tools again.** It ran the command under a
  non-login `bash -c`, and `incus exec` supplies only a bare default PATH, so
  anything installed per-user was invisible: `~/.local/bin` (added by
  `/etc/profile.d/local-bin.sh`) and `~/.npm-global/bin` (added by the nodejs
  install.d snippet) are both login-shell-only. Both examples the command
  documents — `jailbee exec smoke -- claude` and `jailbee exec smoke -- pnpm
  test` — therefore died with "command not found". It now uses `bash -lc`, the
  shell `jailbee shell` and the PR-text bridge already use; a non-interactive
  login shell adds nothing of its own to stdout, so piped output is unchanged.
- **`jailbee pr` no longer runs your test suite to write a PR description.** The
  prompt asked the body to say "how it was tested", and since the generation gets
  an unrestricted shell it answered that by running the project's tests. Measured
  in JailBee's own repo on a 21-file diff: 59 s of a 165 s run went to
  `uv run pytest`, against a hard-coded 180 s cap — so a repo whose suite is
  slower than JailBee's fully-mocked one could not finish at all, and the only
  symptom was `In-container Claude could not generate the PR text: ... timed out
  after 180s` followed by a placeholder description. The prompt now forbids
  running tests, builds, linters and installers, and asks it to describe testing
  from the commits and the CI config; the same run takes 109 s. A project that
  genuinely wants more can still ask for it in `claude.pr_prompt`, which outranks
  the guard.
- **A timed-out PR-text generation now says where to look.** Because
  `--output-format json` emits nothing until the run finishes, an expiry reached
  the user as a bare "timed out" with no output on either stream — while Claude's
  own transcript of the attempt sat in the container the whole time, unmentioned
  and hard to find (`claude --resume` lists only the sessions of the directory it
  is run from). `jailbee pr` now pins the session id up front and names it, the
  container and the effective budget on expiry, so `jailbee shell <name>` +
  `claude --resume <id>` shows how far the run actually got. Failures that leave
  nothing to resume — a missing `claude`, a rejected `claude.ai_pr_model` — say
  nothing about a transcript, which is why `incus.py` now raises an
  `IncusTimeoutError` subclass rather than making callers pattern-match the message.
- **The PR-text timeout is configurable** as `claude.ai_pr_timeout`, default
  600 s (was hard-coded at 180 s, with neither `jailbee pr` call site able to
  override it). Generation is an agentic run of a dozen-plus turns over the log,
  the diff, the PR template, the branch's spec and the CI config, so its cost
  scales with the repository rather than with the diff alone. Raise it for a
  large tree; `ai_pr_description: false` is still how you switch generation off.
- **The dashboards notice repos registered while they are open.** Both
  `jailbee dashboard` and `jailbee gui` resolved the list of registered repos
  once at launch and reused it for the whole session. A repo that registered
  later — the first `jailbee new` in it, or a re-registration after the pool
  timer dropped a repo whose config file briefly disappeared — was not merely
  missing: its containers still showed up, but under a view-only `(orphan)`
  group, where right-clicking them opened no action menu at all until the
  window was restarted. The repo list is now re-resolved on every refresh.
- **A view-only container says why it has no actions.** Right-clicking a
  container in an `(orphan)` group used to do nothing whatsoever in the Qt
  dashboard, which is indistinguishable from a broken menu. It now opens with
  a disabled entry naming the repo whose config could not be loaded — the same
  sentence the TUI dashboard prints.
- **Conflicting gitlinks in nested submodules are resolved too.** `git merge`
  exits non-zero when its only conflict is a submodule pointer, so a submodule
  whose *own* gitlink conflicted was classified as a content conflict and left
  to the user — the recursion meant to handle it could never run. JailBee now
  classifies by what the submodule's index actually contains, so a chain of
  conflicting gitlinks is merged all the way down in a single `jailbee pull`
  or `jailbee git push --merge`.
- **A missing `incus` binary is now reported, not a traceback.** On a host
  where Incus was not installed (or not on `PATH`), every command that talks
  to it ended in a raw `FileNotFoundError` traceback — including `jailbee
  doctor`, which detected the missing binary, recorded the failure, and then
  crashed before printing it. The wrapper now raises `IncusError` like it
  does for timeouts, the CLI prints it as a one-line message and exits 1, and
  `doctor` skips the checks that need the binary rather than repeating one
  root cause a dozen times.
- **The upstream remote no longer has to be called `origin`.** Every host-side
  git operation assumed that name, so a repo whose upstream had been renamed
  (`git remote rename` is ordinary) saw `jailbee new --pr` and branch
  publishing fail outright, while `jailbee git push` silently pushed the local
  ref instead of the fetched upstream one, `jailbee git checkout` created host
  branches with no upstream, and default-branch detection fell back to the
  literal `main`. jailbee now resolves the name — see [Which remote is the
  upstream?](docs/config.md#which-remote-is-the-upstream). Repos that do have
  an `origin` are unaffected; `jailbee doctor` reports which remote was
  resolved.
- **The autostart privilege gate no longer degrades silently.** Its baseline
  is the reviewed config on the upstream's default branch, and a remote under
  another name made that ref unresolvable — so the baseline became the
  caller's own checkout permanently, with nothing printed. The ref now follows
  the resolved remote, and a baseline that cannot be read at all is warned
  about instead of only noted.
- **Container branch tracking is no longer copied verbatim from the host.**
  The in-container clone's only remote is `origin`, but `branch.<b>.remote`
  was taken from the host, so a host using another name left the container
  tracking a remote that does not exist there and `git push` inside it failed.

### Removed

**The `gie`-era compatibility surface is gone**, on the schedule 1.0.0
announced. Five of the six pieces go in this release; the sixth is kept and
described under Deprecated below. The migrator only ever shipped in 1.0.x, so
a still-unmigrated install has to be walked forward on a 1.0.x version before
upgrading past it — this release cannot do it. See
[`docs/migrating-from-gie.md` as of 1.0.0](https://github.com/VRTFinland/jailbee/blob/v1.0.0/docs/migrating-from-gie.md).

- **The `gie` console script.** `pip`/`uv` installed `jailbee`, `jb` and `gie`
  as three names for the same CLI. The third is no longer declared, so an
  upgrade leaves `gie` behind as a command not found. Anything scripted
  against it — aliases, cron entries, editor run configs — needs the name
  changed to `jailbee` or `jb`. One leftover to clean up by hand if you ever
  ran `make install`: `~/.local/share/bash-completion/completions/gie` stays
  on disk and now completes a command that does not exist. It is inert, and
  `rm` clears it.
- **`claude.install_gie_skills` as a config alias.** The key is
  `claude.install_jailbee_skills`. The old name is now a retired key, so a
  config still using it fails to load with an error naming the replacement
  rather than being silently ignored — check `~/.config/jailbee/global.yaml`
  if `jailbee` suddenly refuses to start.
- **`jailbee migrate`.** The whole command and its module. `jailbee doctor`'s
  "pre-1.0 gie state" check goes with it, replaced by a narrower "legacy repo
  config" check for the one thing still worth reporting (below). That check no
  longer needs the `incus` binary, so it now also runs on a host without Incus.
- **The legacy `/etc/hosts` sentinel.** `jailbee net refresh` recognised the
  pre-rename `# BEGIN gie-managed allowlist` markers so it would replace such
  a block instead of appending a second one beside it. A container old enough
  to still carry those markers must be recreated.
- **The `<data>/gie` compatibility symlink.** `jailbee migrate` used to leave
  `~/.local/share/gie` pointing at `~/.local/share/jailbee` so that
  absolute disk-device sources baked into pre-rename containers kept
  resolving. Nothing creates it any more; an existing one is inert and can be
  deleted once no container depends on it.

Two pre-1.0 cleanups that ran on every `apply` go too, having nothing left to
clean: the container-side removal of the `gie-registry-mirror` CA certificate
and keytool alias, and `make install-skill`'s removal of the old
`gie-repo-setup`/`gie-usage` Claude skills.

### Deprecated

- **`.gie/config.yaml` is the one piece of pre-1.0 compatibility kept.** A
  repo whose config still lives in `.gie/` is read, with a warning naming the
  `git mv` that fixes it, because that file is committed to shared
  application repos and every branch has to be renamed before the fallback
  can go. It is removed in **2.0.0**. `jailbee doctor` reports whether the
  repo you are in still relies on it.

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
**1.1.0**. See [`docs/migrating-from-gie.md` as of 1.0.0](https://github.com/VRTFinland/jailbee/blob/v1.0.0/docs/migrating-from-gie.md)
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
