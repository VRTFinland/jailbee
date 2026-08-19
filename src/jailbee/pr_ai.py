"""In-container Claude bridge for generating PR title, body & branch name.

`jailbee pr` calls `generate_pr_text` after pushing the branch and
before `gh pr create`, when `claude.enabled` and `claude.ai_pr_description`
are on. It runs the container's own Claude CLI over the branch's commits and
diff to produce a concise PR title, body, and a convention-following head
branch name.

Design rules this module obeys:
  - It NEVER calls `gh` — that stays in `pr.py`.
  - It NEVER calls `subprocess` directly — it goes through `Incus.exec`, so
    it stays unit-testable (the architecture rule for non-incus modules).
  - It is strictly best-effort: every expected failure (Claude missing,
    timeout, unparseable output) returns None so the caller falls back to a
    placeholder. It does not raise for those cases.
  - The prompt must never invite work whose cost the repository controls. The
    run has a fixed timeout, and Claude has an unrestricted shell
    (`--dangerously-skip-permissions`), so asking it to describe "how it was
    tested" made it run the project's own test suite — 59s of a 165s run in
    jailbee's repo, more than the whole budget in a larger one. Hence the
    explicit do-not-run clause in `_PROMPT_TEMPLATE`: keep it there.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.incus import IncusError, IncusTimeoutError
from jailbee.tui import warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

_MAX_TITLE_LEN = 120

_PROMPT_TEMPLATE = """\
You are generating a GitHub pull-request title and description for the work on \
the current branch, which targets the base branch `{base}`.
The branch is named `{branch}`.

Inspect the change yourself using your shell tools:
  - `git log {base}..HEAD` for the commits on this branch
  - `git diff {base}...HEAD` for the cumulative diff
  - `.github/pull_request_template.md`, or a file under
    `.github/PULL_REQUEST_TEMPLATE/`. If the repository ships a PR template,
    follow its headings and fill in every section it asks for — it is this
    project's own definition of a complete description.
  - the spec, plan, or issue this branch implements. Look for it in `docs/`,
    `specs/`, and the commit messages. When you find one, describe the change
    against that stated intent, and say plainly what it deliberately leaves out.
  - `CONTRIBUTING.md`, `CLAUDE.md`, or `AGENTS.md` for this repository's own
    rules on how commits and pull requests are written.

If the branch name or a commit message references an issue number, read it with
`gh issue view <number>` and use it for the background section. Add a
`Closes #<number>` line when merging this PR really does close that issue. Skip
this silently when `gh` is missing or cannot reach GitHub.

Write a concise, technical pull request in clear English:
  - Title: imperative, <= 72 characters, matching the repository's
    conventional-commit style if you can see one (e.g. `feat(scope): ...`).
  - Body: GitHub-flavored Markdown — short background, then what changed with
    concrete file/symbol references, then how it was tested.

Do NOT run this project's tests, build, linters, formatters or installers, and
do not start any long-running command. Describe how the change was tested from
the commits, the diff and the repository's CI config — you are writing a
description, not verifying the branch.
{project_clause}{fixed_clause}
Also choose the branch name to use as this PR's head on the remote. Infer the
repository's branch-naming convention from `git branch -r`, the names of
recently merged pull requests, and any CONTRIBUTING.md / CLAUDE.md guidance. If
the current branch name `{branch}` already follows that convention, return it
unchanged.

Respond with ONLY a JSON object, no prose and no code fences:
{{"title": "<title>", "body": "<body>", "branch": "<branch>"}}
"""

_PROJECT_BLOCK_HEADER = "--- PROJECT-SPECIFIC INSTRUCTIONS ---"

# The project block sits after the generic rules and before the JSON contract:
# a project may dictate the title and body shape, but never the response format
# `_parse_pr_text` depends on.
_PROJECT_BLOCK_TEMPLATE = f"""
{_PROJECT_BLOCK_HEADER}
The instructions below come from this repository's own jailbee config
(`claude.pr_prompt`). Where they conflict with any of the generic guidance
above, THESE WIN. They do not override the JSON response format below.

{{project_prompt}}
--- END PROJECT-SPECIFIC INSTRUCTIONS ---
"""


@dataclass(frozen=True)
class PrText:
    """A generated PR title, body, and proposed head branch name."""

    title: str
    body: str
    branch: str


def generate_pr_text(
    cfg: Config,
    incus: Incus,
    full_name: str,
    *,
    branch: str,
    base: str,
    fixed_title: str | None = None,
    fixed_body: str | None = None,
    timeout: int | None = None,
) -> PrText | None:
    """Ask the in-container Claude CLI for a PR title and body.

    Returns a PrText on success, or None on any expected failure (Claude
    missing/non-zero exit, timeout, or output that can't be parsed into a
    valid title+body). The caller logs a warning and falls back. Both `branch`
    and `base` are interpolated into the prompt.

    ``timeout`` defaults to `claude.ai_pr_timeout`; pass it only to override
    the configured budget.
    """
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.lifecycle import container_repo_dir, short_name

    repo_dir = container_repo_dir(cfg, incus, full_name)
    prompt = _build_prompt(branch, base, fixed_title, fixed_body, cfg.claude.pr_prompt)
    # `claude` lives at ~/.local/bin/claude, which is not on the default
    # `incus exec --user` PATH. Run it through a login shell (`bash -lc`) so
    # ~/.profile puts ~/.local/bin on PATH — the same pattern tmux/autostart
    # use. The prompt and the model are passed via env vars (not interpolated
    # into the shell string) so their content can never be parsed as shell.
    # `${VAR:+...}` drops the whole --model flag when the var is empty, which
    # is how `ai_pr_model: null` inherits the container's own default model.
    #
    # The session id is chosen HERE rather than read from Claude's reply: with
    # `--output-format json` nothing reaches stdout until the run ends, so a
    # timeout — the one failure where the transcript is worth reading — is
    # exactly the case where the reply, and the id in it, never arrive.
    # Pre-assigning it means the warning below can name a session that is
    # already on disk in the container.
    session_id = str(uuid.uuid4())
    budget = cfg.claude.ai_pr_timeout if timeout is None else timeout
    shell_cmd = (
        'claude ${JAILBEE_PR_MODEL:+--model "$JAILBEE_PR_MODEL"} '
        '--session-id "$JAILBEE_PR_SESSION" '
        '-p "$JAILBEE_PR_PROMPT" --output-format json --dangerously-skip-permissions'
    )
    env = {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "USER": CONTAINER_USERNAME,
        "LOGNAME": CONTAINER_USERNAME,
        "JAILBEE_PR_PROMPT": prompt,
        "JAILBEE_PR_MODEL": cfg.claude.ai_pr_model or "",
        "JAILBEE_PR_SESSION": session_id,
    }
    try:
        stdout = incus.exec(
            full_name,
            ["bash", "-lc", shell_cmd],
            uid=cfg.container_user.uid,
            gid=cfg.container_user.gid,
            cwd=repo_dir,
            env=env,
            timeout=budget,
        )
    except IncusTimeoutError as exc:
        # A timeout is the one failure that leaves something to read: Claude
        # writes its transcript as it goes, so the run that ran out of budget
        # is on disk and resumable even though jailbee received no bytes.
        # Naming the container and the session is the difference between a
        # dead end and a diagnosis — `claude --resume` alone lists only the
        # sessions of whatever directory it is run from.
        warn(f"In-container Claude could not generate the PR text: {exc}")
        warn(
            f"That attempt left a transcript in the container. To see how far it got: "
            f"`jailbee shell {short_name(cfg, full_name)}`, then "
            f"`claude --resume {session_id}`. Raise `claude.ai_pr_timeout` "
            f"(currently {budget}s) if it was simply still working."
        )
        return None
    except IncusError as exc:
        # The caller only learns that generation failed. Report why here: a
        # rejected `claude.ai_pr_model` or a missing `claude` are otherwise
        # indistinguishable from the generic fallback message.
        warn(f"In-container Claude could not generate the PR text: {exc}")
        return None
    return _parse_pr_text(stdout, fixed_title, fixed_body, current_branch=branch)


def _build_prompt(
    branch: str,
    base: str,
    fixed_title: str | None,
    fixed_body: str | None,
    project_prompt: str | None = None,
) -> str:
    project_clause = ""
    if project_prompt is not None and project_prompt.strip():
        project_clause = _PROJECT_BLOCK_TEMPLATE.format(project_prompt=project_prompt)
    fixed_lines: list[str] = []
    if fixed_title is not None:
        fixed_lines.append(
            f"The title is already chosen — use exactly this and do not change "
            f'it: "{fixed_title}". Generate only the body.'
        )
    if fixed_body is not None:
        fixed_lines.append(
            "The body is already written — echo it back unchanged and generate only the title."
        )
    fixed_clause = ("\n" + "\n".join(fixed_lines) + "\n") if fixed_lines else ""
    return _PROMPT_TEMPLATE.format(
        branch=branch,
        base=base,
        project_clause=project_clause,
        fixed_clause=fixed_clause,
    )


def _parse_pr_text(
    stdout: str,
    fixed_title: str | None,
    fixed_body: str | None,
    *,
    current_branch: str,
) -> PrText | None:
    """Parse `claude --output-format json` stdout into a PrText, or None.

    Peels the outer envelope (`{"result": "<text>"}`), then extracts the inner
    JSON object from the model text (tolerating ```json fences and surrounding
    prose). Validates non-empty title/body and a sane title length. Explicit
    fixed_title/fixed_body always win over whatever the model produced. The
    model's proposed `branch` is used only if it passes
    `git.check_ref_format`; otherwise it falls back to `current_branch`.
    """
    from jailbee import git

    candidate = _unwrap_envelope(stdout)
    obj = _extract_json_object(candidate)
    if obj is None:
        return None

    title = fixed_title if fixed_title is not None else _as_str(obj.get("title"))
    body = fixed_body if fixed_body is not None else _as_str(obj.get("body"))
    if not title or not title.strip():
        return None
    if not body or not body.strip():
        return None
    if len(title) > _MAX_TITLE_LEN:
        return None

    proposed = _as_str(obj.get("branch"))
    if proposed and proposed.strip() and git.check_ref_format(proposed.strip()):
        branch = proposed.strip()
    else:
        branch = current_branch
    return PrText(title=title.strip(), body=body.strip(), branch=branch)


def _unwrap_envelope(stdout: str) -> str:
    """Return the model text from Claude's JSON envelope, or stdout itself."""
    try:
        outer = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout
    if isinstance(outer, dict):
        result = outer.get("result")
        if isinstance(result, str):
            return result
    return stdout


def _extract_json_object(text: str) -> dict[str, object] | None:
    """Parse a JSON object out of model text (strip fences, then brace-slice).

    First tries the stripped text directly, then repeatedly calls
    ``_brace_slice`` (advancing past each failing candidate) so that a stray
    balanced ``{…}`` fragment before the real JSON object does not defeat
    extraction.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the trailing fence.
        inner = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[: -len("```")]
        stripped = inner.strip()

    # Fast path: the stripped text *is* the JSON object.
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Slow path: scan for balanced brace substrings, trying each in order.
    offset = 0
    while True:
        candidate = _brace_slice(stripped, offset)
        if candidate is None:
            return None
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
        # Advance past the opening brace of the candidate just tried.
        next_start = stripped.find("{", offset)
        if next_start == -1:
            return None
        offset = next_start + 1


def _brace_slice(text: str, offset: int = 0) -> str | None:
    """Return the first balanced top-level JSON object substring in *text*.

    Scans from *offset* (default 0) to find the first ``{``, then walks
    character-by-character tracking brace depth while honouring JSON string
    boundaries (``"…"`` with ``\\`` escapes).  Returns the slice from that
    opening ``{`` to its matching ``}`` (inclusive), or None if no balanced
    object is found from *offset* onwards.
    """
    start = text.find("{", offset)
    if start == -1:
        return None

    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2  # skip the escaped character
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
