---
name: feature-to-child
description: Build a feature from a GitHub issue in an orchestrator container — grill, spec, tickets, one child implements, review, rework, gate, close.
disable-model-invocation: true
---

# Feature to child

You are the orchestrator, and the `jailbee-orchestration` skill covers the protocol: grants, `send spawn`, the inbox waiter, imports and publishing. This skill is the order of work for a multi-ticket feature. For a single bug or a small change, use `issue-to-child`.

Steps marked **user command** run a skill that is user-only (`disable-model-invocation`). The Skill tool refuses it, so pause and ask the user to type it. Do not reproduce its workflow by hand.

Keep steps 1–4 in one context window: the grilling, spec and tickets build on the same thinking.

## 1. Read the issue and survey

Read the issue with `gh issue view N --repo VRTFinland/jailbee --json title,body,comments`. Send one Explore subagent (sonnet) to map the code the feature touches: modules, `file:line`, existing tests, prior art for the same kind of action.

**Done when** you can name every module the feature crosses.

## 2. Grill

Run `grilling` here, in prose, in rounds. Give each question a recommended answer.

**Done when** the frontier is empty and the user confirms the design. Security boundaries (what data crosses from host to container, what a grant covers) are explicit decisions, not defaults.

## 3. Spec — user command

Ask the user to run `/mattpocock-skills:to-spec`. It writes `.local/superpowers/specs/YYYY-MM-DD-<topic>-design.md` and publishes the spec as a GitHub issue through the outbox. Commit inside `.local/superpowers/`.

**Done when** the spec file is committed and its issue number is known.

## 4. Tickets — user command

Ask the user to run `/mattpocock-skills:to-tickets`. The tickets go to `.local/tickets/<spec-issue>-<slug>/NN-*.md`, following `.local/tickets/README.md`. Commit inside `.local/tickets/`.

**Done when** every ticket has its blocking edges and is committed.

## 5. Brief and spawn

Write the brief to the scratchpad, never the work tree. **Line 1 is the bare command and its argument, alone:**

```text
/mattpocock-skills:implement-spec .local/superpowers/specs/<spec>.md

Task: implement spec #<spec-issue> (requested in #N) — all tickets in .local/tickets/<dir>/.
Design (decided): <the grilling decisions that bound the work, especially security boundaries>
<Acceptance, Report and commit lines from ../issue-to-child/BRIEF-TEMPLATE.md>
```

The harness allows a child's Skill call for a user-only skill only when the command starts a line of the user message. Mid-sentence or in backticks, the child's Skill tool refuses it and the child stops to ask. The broker's preamble goes before the brief; that is fine.

Spawn a fork child, `--file <brief> --wait --until done`, runner `--agent claude-jb --profile codex --model opus`, branch `feat/issue-N-<slug>`, `--on-done keep`. Arm the inbox waiter.

**Done when** the spawn reads `is ready; agent is alive` and the waiter is armed.

If the child asks why it cannot run the command, the brief is wrong. Fix line 1 and relaunch:

```bash
jailbee inbox send stop --child <child> --wait
jailbee inbox send restart --child <child> --file <fixed brief> --wait --until done
```

A restart refused "is running" right after the stop reported done: retry it once.

The codex runner sometimes emits an empty tool call. It retries by itself; ignore it.

## 6. Merge and review

On `done`, merge the `imported_ref` and destroy the child. Send an independent review subagent (opus) the diff range and the spec path, without your conclusions. Ask for verified findings with failure scenarios, and for each weak test: "would this test fail if the code were wrong?"

**Done when** every finding is accepted, rejected with a reason, or turned into a follow-up issue, and the user has decided any finding that changes the design.

## 7. Rework

Write the accepted findings as one more ticket (`NN-review-rework.md`) in the same directory and commit it. Spawn a fork child from the merged HEAD whose brief line 1 is:

```text
/mattpocock-skills:implement .local/tickets/<dir>/NN-review-rework.md
```

Merge its `done` the same way.

**Done when** the rework is merged.

## 8. Gate the merged tree

```bash
uv run pytest -n auto -q
uv run mypy src/
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
make web-e2e        # only if the feature touched frontend/ or src/jailbee/web/
```

**Done when** all of them are clean.

## 9. Close and stage the PR

1. Stage a comment and close for the spec issue and the original issue as one batch, and publish it (see `issue-to-child` step 8). Name any check that needs the host, such as a live GitHub call, in the comment.
2. Publish follow-up issues the review or grilling produced.
3. Stage a PR source manifest with the `pr` skill's body template.

**Done when** both issues are closed and the PR manifest is staged.

## 10. Wrap up

1. Check that `jailbee inbox children` holds only children kept on purpose, and that no suite or waiter processes are left running.
2. Report the outcome and the host commands still owed (manifests, live checks).
3. Ask the user to run `/mattpocock-skills:retro` (**user command**) in this session, before clearing.
