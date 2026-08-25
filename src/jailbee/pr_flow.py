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
from typing import TYPE_CHECKING, Never, Protocol

import typer

from jailbee.tui import error, info, warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus as IncusType
    from jailbee.pr_ai import PrText


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

    @property
    def command(self) -> str:
        """The CLI invocation to point users at for follow-up commands."""
        return "jailbee submodule pr" if self.subpath else "jailbee pr"


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


@dataclass(frozen=True)
class HeadPlan:
    """The publish name and the AI text a create/update run decided on."""

    publish_name: str | None
    ai_text: PrText | None


def resolve_pr_text_and_head(
    cfg: Config,
    incus: IncusType,
    full: str,
    scope: PrScope,
    *,
    is_update: bool,
    stored_head: str | None,
    source_branch: str | None,
    base: str,
    title: str | None,
    body: str | None,
    as_name: str | None,
    no_ai: bool,
    status_label: str,
) -> HeadPlan:
    """Decide the PR head name and (on create) generate the title/body.

    `ai_pr_branch` and `ai_pr_description` are INDEPENDENT toggles.
    `generate_pr_text` is a single call that yields title, body AND branch, so
    it runs when EITHER feature needs it and each part is applied only if its
    own flag is on.

    On the update path the stored external name is reused and the branch is
    never regenerated — `publish_name=None` lets the publish step default to
    the source branch.
    """
    from jailbee import git as git_mod
    from jailbee import pr_ai

    if is_update:
        return HeadPlan(publish_name=stored_head or None, ai_text=None)

    if as_name is not None and not git_mod.check_ref_format(as_name):
        error(f"--as '{as_name}' is not a valid branch name.")
        raise typer.Exit(2)

    ai_on = cfg.claude.enabled and cfg.claude.ai_pr_description and not no_ai
    branch_ai_on = cfg.claude.enabled and cfg.claude.ai_pr_branch and not no_ai
    need_desc_ai = ai_on and not (title and body)
    need_branch_ai = branch_ai_on and as_name is None
    ai_text: PrText | None = None
    if (need_desc_ai or need_branch_ai) and source_branch:
        from jailbee.tui import console

        with console.status(status_label):
            ai_text = pr_ai.generate_pr_text(
                cfg,
                incus,
                full,
                branch=source_branch,
                base=base,
                fixed_title=title,
                fixed_body=body,
                subpath=scope.subpath,
            )
        if ai_text is None:
            warn(
                f"{scope.prefix}Claude PR-text generation failed; using a "
                f"placeholder. Edit the PR later with `{scope.command} --description`."
            )

    if as_name is not None:
        return HeadPlan(publish_name=as_name, ai_text=ai_text)
    if need_branch_ai and ai_text is not None and source_branch:
        return HeadPlan(
            publish_name=confirm_pr_branch_name(ai_text.branch, source_branch),
            ai_text=ai_text,
        )
    return HeadPlan(publish_name=source_branch, ai_text=ai_text)


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


@dataclass(frozen=True)
class PrRecord:
    """What is remembered about a target's PR between runs."""

    number: int | None
    head: str | None
    author: bool
    adopted: bool


class PrState(Protocol):
    """Where a target's PR decision is persisted.

    Two implementations: `ContainerLabelState` (the superproject, container
    labels) and `submodule_pr.SubmodulePrState` (one submodule, a JSON map).
    """

    def read(self) -> PrRecord: ...

    def record(self, *, head: str, author: bool, adopted: bool, number: int | None) -> None: ...


class ContainerLabelState:
    """`PrState` over the container's `user.jailbee.pr*` labels."""

    def __init__(self, incus: IncusType, full: str) -> None:
        self._incus = incus
        self._full = full

    def read(self) -> PrRecord:
        raw = self._incus.config_get(self._full, "user.jailbee.pr")
        try:
            number = int(raw) if raw else None
        except ValueError:
            number = None
        return PrRecord(
            number=number,
            head=self._incus.config_get(self._full, "user.jailbee.pr_branch") or None,
            author=bool(self._incus.config_get(self._full, "user.jailbee.pr_author")),
            adopted=bool(self._incus.config_get(self._full, "user.jailbee.pr_adopted")),
        )

    def record(self, *, head: str, author: bool, adopted: bool, number: int | None) -> None:
        """Write the decision, best-effort.

        Write order matters for a partial (interrupted) write. `pr_branch` goes
        FIRST so that even a partially-written container resolves the correct
        PR head on the re-run UPDATE path (a missing `pr_branch` would push to
        the container branch — the wrong head). `user.jailbee.pr` goes LAST
        because the entry guard keys on `pr` present WITHOUT `pr_author` (== a
        `jailbee new --pr` review container).
        """
        from jailbee.incus import IncusError

        try:
            self._incus.config_set(self._full, "user.jailbee.pr_branch", head)
            if author:
                self._incus.config_set(self._full, "user.jailbee.pr_author", "1")
            if adopted:
                self._incus.config_set(self._full, "user.jailbee.pr_adopted", "1")
            if number is not None:
                self._incus.config_set(self._full, "user.jailbee.pr", str(number))
        except IncusError as exc:
            warn(f"Could not record the PR decision: {exc}")


def adopt_existing_pr_for_branch(
    scope: PrScope,
    state: PrState,
    *,
    branch: str | None,
    yes: bool,
) -> tuple[int, str] | None:
    """Adopt the PR that already exists for the container's branch, if any.

    A container made with `jailbee new <existing-branch>` carries no PR label, yet
    that branch may already have a PR open — and without this lookup the create
    path would propose a fresh head branch name and open a *second* PR for the
    same work. (When the proposed name happens to equal the branch, `gh pr
    create` fails with "already exists" and `pr.create_pr` recovers, but that
    fallback also records `user.jailbee.pr_author`, claiming a PR jailbee never opened.)

    Returns `(number, head_ref)` for the adopted PR, or None when the create
    path should proceed untouched: no PR for the branch, a closed/merged one
    (not a target for further work), or a fork PR (whose head lives in the
    fork, so our branch is a genuinely different thing). Exits non-zero on a
    declined or unavailable confirmation.

    Records the head branch and `adopted` — but deliberately not `author`:
    jailbee found this PR, it did not create it, and the foreign-head guards
    (`--force` confirmation, no description regeneration) must stay on.
    """
    from jailbee import pr as pr_module
    from jailbee.lifecycle import _stdin_is_interactive

    if not branch:
        return None

    found = pr_module.find_pr_for_branch(scope.repo_root, branch)
    if found is None:
        return None

    if found.state != "OPEN":
        info(
            f"{scope.prefix}Branch '{branch}' had PR #{found.number} ({found.state}); "
            f"opening a new one."
        )
        return None
    if found.is_cross_repository:
        owner = found.head_repo_owner or "<fork-owner>"
        info(
            f"{scope.prefix}PR #{found.number} matches this branch by name but its "
            f"head lives in the fork '{owner}', so it is a different branch; "
            f"opening a new PR."
        )
        return None

    author = f"@{found.author_login}" if found.author_login else "an unknown author"
    info(
        f"{scope.prefix}Branch '{branch}' already has PR #{found.number} by {author} "
        f"(OPEN); head '{found.head_ref}' → base '{found.base_ref}'."
    )

    if not yes:
        if not _stdin_is_interactive():
            error(
                f"{scope.prefix}Branch '{branch}' already has PR #{found.number}. "
                f"Pushing this container's commits to it needs confirmation — "
                f"re-run with --yes when there is no terminal to ask on."
            )
            raise typer.Exit(1)
        if not typer.confirm(
            f"Push this container's commits to PR #{found.number} instead of opening a new one?",
            default=True,
        ):
            info(
                "Aborted. To open a separate PR from this container, re-run with "
                "'--as <other-branch-name>'."
            )
            raise typer.Abort()

    state.record(head=found.head_ref, author=False, adopted=True, number=found.number)

    return found.number, found.head_ref
