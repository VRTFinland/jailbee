"""Shared interactive PR flow for `jailbee pr` and `jailbee submodule pr`.

This module is the interactive layer between `cli.py`'s argument parsing and
the fact-and-effect modules (`sync`, `pr`, `pr_ai`, `submodule_pr`). It may
prompt and it may raise `typer.Exit` — that is its job, and the reason the
flow lives here rather than in `submodule_pr.py`, which stays prompt-free.

Two values parameterise every function so that one flow serves a superproject
PR and a submodule PR:

  - `PrScope` — which repository `gh` and `git` run in, which remote is its
    upstream, how the PR is named in messages, and the AI's cwd suffix.
  - `PrState` — where the chosen PR head is remembered. The superproject
    implementation reads container labels; the submodule one reads a JSON map
    (see `submodule_pr.SubmodulePrState`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never

import typer

from jailbee.tui import error, warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus as IncusType


@dataclass(frozen=True)
class PrScope:
    """Where a PR lives, and how to talk about it.

    `repo_root` is the directory `gh` and `git` run in — the superproject root
    for `jailbee pr`, the host sub-repo for `jailbee submodule pr`. `remote` is
    that repository's own upstream: a submodule may name its upstream something
    the superproject does not. `prefix` prefixes every user-facing message
    (`""` for the superproject). `subpath` is the top-relative submodule path,
    used as the in-container cwd suffix for AI generation, and `None` for the
    superproject.
    """

    repo_root: Path
    remote: str
    prefix: str
    subpath: str | None

    def noun(self, pr_label: str | None) -> str:
        """How to name the PR under discussion in a message."""
        base = f"PR #{pr_label}" if pr_label else "the container's PR"
        return f"{self.prefix}{base}"


def reject_as_on_pr_update(scope: PrScope, as_name: str, pr_label: str | None) -> Never:
    """Exit 2: `--as` cannot retarget the head of an already-existing PR.

    The update path always pushes to the PR's recorded head branch, so an `--as`
    name would publish some other branch and leave the PR untouched. Applies on
    every run of a container that has a PR (jailbee-authored or adopted from
    `jailbee new --pr`), not just the first.
    """
    target = scope.noun(pr_label)
    error(
        f"--as cannot be combined with {target}: pushing to '{as_name}' would "
        f"update a different branch and leave {target} untouched. Drop --as to "
        f"update {target}."
    )
    raise typer.Exit(2)


def confirm_foreign_force_push(
    scope: PrScope, short: str, pr_label: str, head: str | None, *, yes: bool
) -> None:
    """Confirm a `--force` push onto the head of a PR jailbee did not create.

    Force-pushing an adopted container's head rewrites history on a branch
    someone else may own, so it takes its own confirmation on top of the
    one-time adoption. `--yes` skips it; without a TTY it is an error.
    """
    from jailbee.lifecycle import _stdin_is_interactive

    target = scope.noun(pr_label)
    head_desc = f"'{head}'" if head else "head branch"
    if yes:
        return
    if not _stdin_is_interactive():
        error(
            f"--force on '{short}' would overwrite {target}'s head {head_desc}, "
            f"a PR jailbee did not create. That needs confirmation — re-run with --yes "
            f"when there is no terminal to ask on."
        )
        raise typer.Exit(1)
    warn(
        f"--force will overwrite {target}'s head {head_desc} with this "
        f"container's history. Commits pushed there by anyone else are lost."
    )
    if not typer.confirm(f"Force-push over {target}'s head {head_desc}?", default=False):
        raise typer.Abort()


def confirm_pr_branch_name(proposed: str, source_branch: str) -> str:
    """Confirm/edit the proposed PR head name on a TTY; return it unchanged off-TTY.

    Enter accepts `proposed`; a typed value replaces it (re-prompts until it is a
    valid git ref). Never prompts when the proposal equals the branch the
    commits came from (nothing to review) or when stdin is not a TTY.
    """
    from jailbee import git as git_mod

    if proposed == source_branch or not sys.stdin.isatty():
        return proposed
    while True:
        chosen: str = typer.prompt("PR head branch name", default=proposed).strip()
        if git_mod.check_ref_format(chosen):
            return chosen
        warn(f"'{chosen}' is not a valid branch name.")


def resolve_pr_description_update(
    cfg: Config,
    incus: IncusType,
    full: str,
    scope: PrScope,
    *,
    branch: str,
    base: str,
    title: str | None,
    body: str | None,
    description: bool,
    ai_on: bool,
    offer_regen: bool = True,
) -> tuple[str | None, str | None] | None:
    """Decide the (title, body) to apply on a PR update, or None to skip.

    Explicit --title/--body win (either may stay None → left unchanged).
    Otherwise --description, or an interactive TTY confirmation, triggers a
    Claude regeneration of both fields. Returns None when nothing should change.

    `offer_regen=False` suppresses only the interactive offer — used on a PR
    jailbee did not create, where silently rewriting the author's description is
    never what the user asked for. Explicit --description/--title/--body still
    apply.
    """
    from jailbee import pr_ai
    from jailbee.lifecycle import _stdin_is_interactive

    if title is not None or body is not None:
        return (title, body)

    want_regen = description
    if not want_regen and offer_regen and _stdin_is_interactive() and ai_on:
        want_regen = typer.confirm(
            f"Update {scope.prefix}the PR description with Claude?",
            default=False,
        )
    if not want_regen:
        return None
    if not ai_on:
        warn(
            f"Cannot regenerate {scope.prefix}the description without Claude "
            "(needs claude.enabled + ai_pr_description, and no --no-ai). Skipping."
        )
        return None

    from jailbee.tui import console

    with console.status("Regenerating PR description with Claude…"):
        text = pr_ai.generate_pr_text(
            cfg,
            incus,
            full,
            branch=branch,
            base=base,
            fixed_title=None,
            fixed_body=None,
            subpath=scope.subpath,
        )
    if text is None:
        warn(f"Claude PR-text generation failed; {scope.prefix}description left unchanged.")
        return None
    return (text.title, text.body)
