# AGENTS.md

[`CLAUDE.md`](CLAUDE.md), beside this file, is the authoritative convention set
for this repository: the architecture rules, the coding patterns, the test
isolation contract, the commit and CHANGELOG conventions. **Read it before your
first edit, in full.** It is ~270 lines and every section applies to you.

It is written for Claude Code. Where it names a tool or a behaviour you do not
have, the corrections below win. Everything else in it holds unchanged.

## Corrections for this agent

- **"Read files with the `Read` tool, not `sed`/`cat`/`head`/`tail`/`awk`."**
  You have no `Read` tool; read files with whatever your toolset provides. The
  intent survives the tool: read enough of a file to be certain of its content,
  and never infer a function's shape from a grep hit alone.
- **"Claude Code's sandbox blocks writes outside the current working directory
  … `dangerouslyDisableSandbox: true`."** Does not apply. This agent runs with
  full filesystem access inside the container; there is no flag to set.
- **"Run git commands one at a time (not chained with `&&`) for cleaner
  permission prompts."** There are no approval prompts here, so chaining is
  fine. The rule it serves — one commit per logical step, bisect-friendly —
  still holds.
- **"Planning artifacts stay out of the tree"** applies to you too:
  `.local/superpowers/{specs,plans}/`, which is gitignored and is *its own git
  repository*. Commit there after each meaningful edit to a spec or plan, and
  never add a remote to it.
- **Do not set `model` on subagent dispatches.** This harness routes subagent
  models through `~/.config/opencode/opencode.json` (`general`, `explore`,
  `small_model` — currently `openrouter/~deepseek/deepseek-flash-latest`). A
  subagent dispatched without an explicit model uses its configured agent
  model (OpenCode V2 docs: "A subagent uses its configured model, or inherits
  the parent session's model when none is configured"), so omit the parameter
  and let the routing apply. The superpowers `subagent-driven-development`
  skill's Model Selection section ("always specify the model explicitly — an
  omitted model inherits the session's model") describes Claude Code, not
  this harness: following it here silently overrides the user's routing.
  Override only when the user explicitly asks for a specific model, and
  record the override in the session ledger. Resuming an existing subagent
  session (SDD fix rounds 1-3) is not a new dispatch and keeps its original
  model.

## What this environment adds

- **`git push` and anything else that mutates the remote needs explicit human
  approval, every time.** `CLAUDE.md:191` already says this; it is restated
  here because it is the one rule whose breach cannot be undone locally. One
  approval is never a standing approval. Local commits need no approval at all
  — make them freely.
- **This container has no route to the git remote.** `git fetch` hangs until it
  times out, so `origin/*` refs are stale and `git log origin/main..main` is
  meaningless. Read remote state with `gh api` instead.
- **`sudo` is available and allowed.** Use it for root-level work rather than
  looking for a way around it.
- **Git and GPG operations can block on a physical YubiKey touch.** A `git`,
  `gh` or GPG command that hangs or times out often means the key is waiting to
  be touched. Say so and let the human touch it; do not retry in a loop.
- **Coding subagents run on the build model; reviews stay on the orchestrator
  model.** The OpenRouter whitelist in `~/.config/opencode/opencode.json`
  admits exactly two models — `z-ai/glm-5.3` (orchestrator, `final-review`)
  and `deepseek/deepseek-v4.1-flash` (`build`, `task-review`) — and rejects
  every other model, so never offer or request a third. Dispatch subagent work
  that writes code through the `build` agent (cheaper, still a good coder);
  when `build` is not dispatchable in the current session, use `general` with
  `model: openrouter/deepseek/deepseek-v4.1-flash` — the one model override
  this environment sanctions. Brainstorming, planning, coordination and code
  review stay on the orchestrator model: a review's value is the reviewer's
  fresh context, not a different model.

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
