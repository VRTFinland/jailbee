---
name: issue-to-child
description: Fix a GitHub issue from an orchestrator container by sending it to a child, then reviewing, merging and closing it.
disable-model-invocation: true
---

# Issue to child

You are the orchestrator, and the `jailbee-orchestration` skill covers the protocol: grants, `send spawn`, the inbox waiter, imports and publishing. This skill is the order of work on top of it. Several issues can run at once, one child each, and they move through these steps independently.

## 1. Read the issue

Run `gh issue view N --repo VRTFinland/jailbee --json title,body,comments`.

**Done when** you have read the body and every comment, and you can say in one sentence what is broken or what is asked for.

## 2. Investigate (read-only)

Send a read-only investigation subagent (Explore, sonnet). It reports:

- the root cause or the code the change touches,
- `file:line` references,
- existing tests,
- open design questions.

Before you put one of its claims in a brief, check that claim in the tree, because the code wins over its report.

**Done when** you can name the files and seams the child will touch, and every design question is either decided or put to the user.

## 3. Choose the entry skill

Pick the mattpocock skill that matches the kind of issue:

- **Bug** → `/mattpocock-skills:diagnosing-bugs`. Acceptance step 1 is a tight loop that goes red on this bug.
- **Well-specified change** → `/mattpocock-skills:implement`, built with `tdd`.
- **Foggy request** → grill the user first, in prose and without blocking. Spawn only once the design is decided.

**Done when** the issue has one entry skill, and any decision that is the user's has been answered.

## 4. Brief and spawn

Fill in [BRIEF-TEMPLATE.md](BRIEF-TEMPLATE.md) and write it to the scratchpad, never into the work tree. Then spawn a fork child with `--file <brief> --wait --until done`:

- **Runner:** `--agent claude-jb --profile codex --model opus` by default. For routine, well-specified work use a cheaper tier, `sonnet`.
- **Branch:** `fix/issue-N-<slug>`.
- **Note:** `--note "GitHub #N"`.

Arm the inbox waiter in the background.

**Done when** the spawn's final status reads `is ready; agent is alive` and the waiter is armed.

## 5. Merge and review

When the child sends `done`:

1. Merge its `imported_ref`.
2. Send an independent review subagent (opus) the diff range and the issue's intent. Do not give it your own conclusions. Ask it for verified findings with failure scenarios. For each weak test, it should ask "would this test fail if the code were wrong?"

**Done when** the work is merged and every review finding is either fixed or explicitly rejected with a reason.

## 6. Rework the findings

Write the findings you accept into a new brief, built from the same template. Send it to a new fork child from the merged HEAD, then merge that child's `done` the same way. Leave out the findings you reject, and tell the user why.

**Done when** the rework is merged, or there was nothing to rework.

## 7. Gate the merged tree

Run on the merged tree:

```bash
uv run pytest -n auto -q
uv run mypy src/
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

**Done when** all four are clean. A failure in a test the change does not touch: rerun it alone, and if it passes, report it as a flake with its node id. That flake is a candidate for its own diagnosing-bugs child.

## 8. Close the issue

Stage the comment and the close as one batch:

```bash
jailbee issue comment N --batch close-N --body "<what changed; next release>"
jailbee issue state N closed --reason completed --batch close-N
jailbee outbox submit close-N
jailbee inbox send publish --issue <staged file> --wait --until done --json
```

**Done when** the publish status is `done` and the `results` array lists every action. If the grant does not cover publishing, keep the staged manifest and give the user the host command instead.

## 9. Wrap up

1. Check that `jailbee inbox children` is empty or holds only children kept on purpose.
2. Check that no suite or waiter processes are left running.
3. Report each issue's outcome to the user and offer `/retro`.
