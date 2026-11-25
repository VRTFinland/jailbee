# AGENTS.md

[`CLAUDE.md`](CLAUDE.md), beside this file, is the authoritative convention set
for this repository: the architecture rules, the coding patterns, the test
isolation contract, the commit and CHANGELOG conventions. **Read it before your
first edit, in full.** It is ~270 lines and every section applies to you.
For web dashboard UI work, also read [`frontend/DESIGN.md`](frontend/DESIGN.md).

It is written for Claude Code. Where it names a tool or a behaviour you do not
have, the corrections below win. Everything else in it holds unchanged.

## Corrections for this agent

- **"Planning artifacts stay out of the tree"** applies to you too:
  `.local/superpowers/{specs,plans}/`, which is gitignored and is *its own git
  repository*. Commit there after each meaningful edit to a spec or plan, and
  never add a remote to it.

## What this environment adds

- **No extra git worktree is needed inside JailBee.** This checkout already
  runs in an isolated container; work on its current branch instead of asking
  to create another worktree for implementation plans.
- **This container has no route to the git remote.** `git fetch` hangs until it
  times out, so `origin/*` refs are stale and `git log origin/main..main` is
  meaningless. Read remote state with `gh api` instead.
- **`sudo` is available and allowed.** Use it for root-level work rather than
  looking for a way around it.
- **Git and GPG operations can block on a physical YubiKey touch.** A `git`,
  `gh` or GPG command that hangs or times out often means the key is waiting to
  be touched. Say so and let the human touch it; do not retry in a loop.

## Definition of done

A change is not finished until all four pass, from the repo root:

```bash
uv run pytest                        # full suite, ~1 min, fully mocked
uv run mypy src/                     # strict
uv run ruff check src/ tests/        # last
uv run ruff format --check src/ tests/
```

Ruff runs **last** and its fixes are committed **separately**, as a `style:`
(or `chore:`) commit distinct from the behavioural change — see CLAUDE.md.

Report failures as failures, with the output. A skipped step is a skipped step.
Host-level verification (a real Incus daemon, a real container) cannot be done
from in here; when a change needs it, say so explicitly and leave it to the
maintainer rather than claiming the work is verified.
