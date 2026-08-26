# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

`jailbee` (CLI: `jailbee`, short form `jb`) is a Python tool that manages
isolated per-branch development environments using Incus system containers. See
[`README.md`](README.md) for the user-facing overview and
[`docs/architecture.md`](docs/architecture.md) for the design rationale and architecture.

## Source of truth

- **Code is the source of truth** — read the code for current behavior;
  docs describe intent, not the guaranteed current state.
- **Tests document behavior** — every module in `src/jailbee/` has
  a matching `tests/test_*.py` with mocked subprocess. If you change behavior,
  update the tests.

## Stack

- Python 3.13 (>= 3.12 supported), managed with [`uv`](https://docs.astral.sh/uv/)
- Typer (CLI), Rich (TUI), Pydantic v2 (config validation), PyYAML
- pytest + pytest-mock for tests
- mypy `--strict` for type checking, ruff for lint
- Wraps the [`incus`](https://linuxcontainers.org/incus/) CLI via `subprocess`

## Architecture rules (from spec)

- **`incus.py` is the only module that calls `subprocess`** for `incus` CLI ops.
  All other modules use `Incus` wrapper methods so they're unit-testable.
- **`config.py` is read-only after load.** No module mutates the loaded
  `Config` object. Tests construct their own with `make_cfg(tmp_path)` from
  `tests/conftest.py`. Note: `Config` carries three computed attributes
  (`repo_root`, `default_branch`, `container_prefix`) set during
  `load_config()` — these are intentionally not YAML keys.
- **`cli.py` is thin** — argument parsing + delegation only. Business logic
  lives in module functions accepting `Config` + `Incus` as inputs.
- **No global state.** All command functions accept dependencies explicitly.
- **Shelling out to non-incus binaries is intentional** and stays one module
  per concern: `git.py` (`git`), `pr.py` (`git`, `gh`), `doctor.py` (`docker`,
  `systemctl`), `init_command.py` (`systemctl`), `maintenance.py`
  (`du`), `chrome_pool.py` (`rsync`), `macos.py` (`sh`), `cswap.py` (`cswap`).
  `gui.py` is the one module that runs `incus` outside `incus.py`: a *detached*
  `subprocess.Popen` of `incus exec`, so a GUI app outlives the CLI.
  `registry.py` runs the mirror through the `Incus` wrapper and calls no
  `subprocess` of its own.

## Essential commands

All commands run from the repo root.

### Testing
```bash
uv run pytest                # run all tests (fast, all mocked)
uv run pytest -xvs           # stop on first failure, verbose
uv run pytest tests/test_X.py # single file
```

### Lint and type-check
```bash
uv run ruff check src/ tests/        # lint
uv run ruff check --fix src/ tests/  # auto-fix
uv run ruff format src/ tests/       # format
uv run mypy src/                     # type check (strict)
```

> **Run ruff last, commit fixes separately.** Before wrapping up any
> change, run both `uv run ruff check src/ tests/` and
> `uv run ruff format --check src/ tests/`. If either reports issues,
> apply the fixes (`ruff check --fix`, `ruff format`) and commit them as
> a **separate** `style:` (or `chore:`) commit, distinct from the
> behavioural change — keeps diffs reviewable and bisect-friendly.

### Run the CLI in dev
```bash
uv run jailbee --help            # uses local source via uv
uv run jailbee config validate
uv run jailbee doctor
uv run jailbee init              # first-time setup only (errors if profiles exist)
uv run jailbee apply             # re-apply config after edits
```

### Install for daily use
```bash
uv tool install -e .         # editable install — `jailbee` works globally
uv tool uninstall jailbee
```

## Coding patterns

### Always use `incus.list_containers()`, not `incus.list()`
The wrapper method is named `list_containers()` to avoid shadowing Python's
builtin `list` in type annotations (mypy strict catches this). The original
plan called it `list()`; we deviated.

### Follow the existing import style
- Lazy imports inside command functions in `cli.py` keep `jailbee --help` fast.
- Type-only imports go under `if TYPE_CHECKING:` at module top.
- Module-level imports use absolute paths (`from jailbee.x import ...`).

### Config tests use the fixtures
`tests/fixtures/full_config.yaml` and `minimal_config.yaml` are the canonical
test configs. To test custom variations, use `cfg.model_copy(update={...})`
rather than constructing dicts from scratch.

### No `# type: ignore` without comment
mypy strict is on. If you must suppress, add a comment explaining why.

### Changing the golden image or profiles requires an `UPGRADE_NOTES` entry

`src/jailbee/upgrade.py` holds `UPGRADE_NOTES` — the hand-maintained list of
which releases need `jb base build` or `jb apply` re-run. Users who upgrade
between released versions are advised from it on `jb ls` / `jb new` /
`jb shell` (a non-blocking hint on stderr) and in `jb doctor`.

Add an entry whenever a change alters:

- what `jb base build` produces — `provision/install.sh`,
  `provision/install.d.available/`, the provisioning env assembled in
  `golden.build_golden_image`, or the default stacks (`Stacks` in `config.py`)
- what `jb apply` writes — profile rendering in `profiles.py`, the ACL in
  `egress.py`, `/etc/hosts` pinning, the dockerd proxy config

Use the **upcoming** release's version number. An entry above the running
version is invisible until that version ships, so adding it early is safe —
but if the release number changes before publication, the entry is wrong and
must be corrected. A forgotten entry is silent: nothing fails, the advice
simply never arrives.

## Test isolation

- All tests are **fully mocked** — none spin up a real Incus daemon, real
  container, or hit the network.
- `subprocess.run` is mocked via `pytest-mock`.
- Filesystem operations use `tmp_path`.
- If you find yourself wanting to run a real `incus` command in a test, that
  test belongs in a future integration test suite, not the unit suite.

## Manual testing

End-to-end smoke-test recipes (require a real Incus daemon) live in
[`docs/manual-testing.md`](docs/manual-testing.md).

## Repo conventions

- Branch model: work on `main`, one commit per logical step. Bisect-friendly.
- Conventional commit prefixes: `feat:`, `fix:`, `test:`, `docs:`, `chore:`,
  `refactor:`. Scope optional but encouraged: `feat(lifecycle): ...`.
- **Commits are free; pushes are not.** Make local commits as soon as
  a logical step is done — no need to ask. `git push` (and anything
  that mutates upstream: force-push, branch delete on the remote, PR
  creation, etc.) requires explicit user approval each time.
- Don't run `git commit` with `-c user.name=...` — repo-local config is set.
- Run git commands one at a time (not chained with `&&`) for cleaner
  permission prompts.
- **Don't use `git -C <path>` when the cwd is already the repo root.**
  Bash tool keeps cwd persistent and starts at the repo root — `git`
  alone already operates on the right tree. User flagged this after a
  `git -C <repo-root> diff` invocation.
- **Read files with the `Read` tool, not `sed`/`cat`/`head`/`tail`/`awk`.**
  `grep` via Bash is fine for pattern searches, but read actual content
  through `Read` (with `offset`/`limit` for large files). User flagged
  this explicitly after a `sed -n '195,235p'` invocation.
- **Prose artifacts (GitHub issues, PR descriptions, longer comments)
  are written in concise technical English in Claude's own voice, not
  by mimicking the user's casual Finnish issue titles.** Lead with the
  background, then the proposed fix with concrete file/symbol
  references, then tests and dependencies. Short titles, descriptive
  bodies. The user noted this explicitly after a first attempt copied
  their terse Finnish style.
- **Planning artifacts stay out of the tree.** Design specs go in
  `.local/superpowers/specs/`, implementation plans in
  `.local/superpowers/plans/`, named `YYYY-MM-DD-<topic>-design.md` /
  `-plan.md`. `.local/` is gitignored (it also holds scratch scripts,
  probe output and working notes), so the artifacts are kept on disk but
  never committed — they are working notes for one change, not project
  documentation, so never `git add -f` them. This overrides the planning
  skills' own default of `docs/superpowers/` (that path is gitignored
  too, but `.local/` is where this repo keeps such notes). Anything worth
  keeping goes into `docs/` proper, the CHANGELOG, or a GitHub issue.
- **Conversation language is Finnish; code and written artifacts are
  English.** The user prefers chatting in Finnish (explanations,
  questions, brainstorming dialogue), while all code, comments, commit
  messages, specs, and other committed prose stay in concise technical
  English. Respond in Finnish in the chat even though the deliverables
  are English.

## Gotchas

- **Sandbox writes:** Claude Code's sandbox blocks writes outside the current
  working directory. Most Bash commands here need `dangerouslyDisableSandbox: true`.
- **`uv run` vs installed `jailbee`:** Tests and dev work use `uv run jailbee`.
  Installed `jailbee` (via `uv tool install -e .`) uses the same source via
  editable install.
- **Pyright LSP false positives:** Pyright in the IDE often shows
  `jailbee.* could not be resolved` because it doesn't auto-find
  the `.venv`. mypy and pytest are the source of truth — both pass cleanly.
- **`incus list` JSON format:** the wrapper expects Incus 1.x JSON. If a
  future Incus version changes the schema, `lifecycle.list_containers` will
  need adjusting (unit tests use synthetic payloads).

## Related context

- `jailbee` is project-agnostic, but was built to wrap a private application
  codebase into containers; `cfg.repo_root` points at whichever repo it's
  run against.
- The project was built spec-first with the `superpowers:writing-plans` /
  `superpowers:executing-plans` skills. Those specs and plans are not in
  the tree (see Repo conventions) — `docs/` plus the code are the record.
