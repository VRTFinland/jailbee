# Child brief template

Copy everything below the line, fill in the `<...>` placeholders, and delete any bracketed alternative that does not apply. The child receives its protocol preamble on its own, so the brief carries only the work.

To splice in the issue body, write the brief in pieces: quoted heredocs for your own text, `gh issue view N --json body -q .body >> brief.txt` for the issue body.

---

```text
Task: <one sentence: fix GitHub issue #N — symptom / requested change>.

[Bug:] Start with `/mattpocock-skills:diagnosing-bugs`. Build the fix with the mattpocock `tdd` skill, and run `code-review` before you finish.
[Change:] Use `/mattpocock-skills:implement`, built with `tdd`, and run `code-review` before you finish.

Choose your test seams yourself, from the existing public boundaries (<candidate seams from the investigation>). Say which ones you chose in your `done` message. Do not ask for confirmation first.

Context:
<issue body, verbatim>

Investigation (check every claim against the tree; the code wins over this summary):
- <file:line — what happens there>
- <existing tests>
[Hypotheses, if the cause is unconfirmed:]
- A. <likely cause, and where to fix it>
- B. <alternative, and how to rule it out>

Design (decided): <the decisions made with the user. Leave this line out if there were none.>
Out of scope: <work explicitly left for later>

[Bug:] Acceptance step 1, before any fix: a tight, deterministic test that goes red on this bug. It must reach the bug through <seam>. In `done`, say which hypothesis it confirmed.

Acceptance:
- <the behavioural tests that must pass>
- `uv run pytest -n auto` runs the full suite in the foreground. Also run `uv run mypy src/`, `uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/`. All of them must be clean.
- Commit any ruff fixes separately, as a `style:` commit.
- CHANGELOG: add a `### Fixed` entry under `## Unreleased` only for a regression against a released version. If the change completes unreleased work, fold it into that work's existing entry.
- Docs and skills under `docs/` must match the changed behaviour.

Report: when you finish, send `done` with a short summary. Include the root cause, the commits, the seams you chose and the suite result. Send `question` only if you are genuinely blocked.
Commit everything before you send `done`.
```
