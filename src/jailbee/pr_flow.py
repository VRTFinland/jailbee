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

from jailbee.tui import error, info, success, warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus as IncusType
    from jailbee.pr import PrCreated
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


def reject_as_on_pr_update(
    scope: PrScope, as_name: str, pr_label: str | None, *, stacked_hint: bool = False
) -> Never:
    """Exit 2: `--as` cannot retarget the head of an already-existing PR.

    The update path always pushes to the PR's recorded head branch, so an `--as`
    name would publish some other branch and leave the PR untouched. Applies on
    every run of a container that has a PR (jailbee-authored or adopted from
    `jailbee new --pr`), not just the first.

    `stacked_hint` names `--stacked` as the way to publish under a different
    head after all. Set for a review container that has not yet decided where
    it publishes — there `--as` is a reasonable thing to have reached for, and
    `--stacked` is what makes it legal.
    """
    target = scope.noun(pr_label)
    message = (
        f"--as cannot be combined with {target}: pushing to '{as_name}' would "
        f"update a different branch and leave {target} untouched. Drop --as to "
        f"update {target}."
    )
    if stacked_hint:
        message += (
            f" To publish under '{as_name}' instead, add --stacked: that opens a "
            f"new PR based on {target}'s head."
        )
    error(message)
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
        # `None` here means two different things to the two callers: for
        # `jailbee pr` it defaults the push to the container branch (see
        # `sync.publish_branch_from_container`'s handling of a `None`
        # publish_name); for `jailbee submodule pr` — which has no
        # "container branch" fallback, only a submodule branch that may not
        # exist (detached HEAD) — it lands in cli.py's "no head branch name
        # was chosen" error instead.
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


class MalformedPrLabelError(RuntimeError):
    """`user.jailbee.pr` is set but is not a valid integer."""


PR_LABEL_PREFIX = "user.jailbee.pr"
"""Labels for the PR the container's branch itself publishes to."""

STACKED_LABEL_PREFIX = "user.jailbee.stacked_pr"
"""Labels for a PR opened *against* the reviewed PR's head (`jailbee pr --stacked`).

Deliberately a second namespace rather than a reuse of `PR_LABEL_PREFIX`:
`user.jailbee.pr` keeps naming the PR the container was created from, so
`jailbee ls`'s PR column and `jailbee git push --pr` ("refresh from PR head")
go on meaning the parent — which is exactly what a stacked container wants
when the PR author pushes new commits.
"""


class PrState(Protocol):
    """Where a target's PR decision is persisted.

    Two implementations: `ContainerLabelState` (the superproject, container
    labels) and `submodule_pr.SubmodulePrState` (one submodule, a JSON map).
    """

    def read(self) -> PrRecord: ...

    def record(
        self,
        *,
        head: str,
        author: bool,
        adopted: bool,
        number: int | None,
        context: str | None = None,
    ) -> None: ...


class ContainerLabelState:
    """`PrState` over one of the container's `user.jailbee.*pr*` label sets.

    `prefix` selects the set: `PR_LABEL_PREFIX` (the default — the PR the
    container's own branch publishes to) or `STACKED_LABEL_PREFIX` (a PR
    opened against the reviewed PR's head). The two are independent records
    on the same container.
    """

    def __init__(
        self,
        incus: IncusType,
        full: str,
        short: str | None = None,
        *,
        prefix: str = PR_LABEL_PREFIX,
    ) -> None:
        self._incus = incus
        self._full = full
        self._short = short or full
        self._prefix = prefix

    def read(self) -> PrRecord:
        """Read the labels into a `PrRecord`.

        Raises `MalformedPrLabelError` when `user.jailbee.pr` is set but does
        not parse as an integer, rather than silently treating it as "no
        PR": every guard keyed on `pr_label`'s truthiness (`--as` rejection,
        the foreign-force confirmation, `offer_regen`) would otherwise turn
        OFF for a container that plainly has a PR, just with corrupted
        state — a fail-open reading of a fail-closed guard. A label jailbee
        wrote itself always parses; anything else needs a human, not a
        silent fallback.
        """
        raw = self._incus.config_get(self._full, self._prefix)
        if raw:
            try:
                number = int(raw)
            except ValueError as exc:
                raise MalformedPrLabelError(
                    f"Container '{self._short}' has a malformed PR label ({self._prefix}={raw!r})."
                ) from exc
        else:
            number = None
        return PrRecord(
            number=number,
            head=self._incus.config_get(self._full, f"{self._prefix}_branch") or None,
            author=bool(self._incus.config_get(self._full, f"{self._prefix}_author")),
            adopted=bool(self._incus.config_get(self._full, f"{self._prefix}_adopted")),
        )

    def record(
        self,
        *,
        head: str,
        author: bool,
        adopted: bool,
        number: int | None,
        context: str | None = None,
    ) -> None:
        """Write the decision, best-effort.

        Write order matters for a partial (interrupted) write. `<prefix>_branch`
        goes FIRST so that even a partially-written container resolves the
        correct PR head on the re-run UPDATE path (a missing branch label would
        push to the container branch — the wrong head). `<prefix>` itself goes
        LAST because the entry guard keys on `user.jailbee.pr` present WITHOUT
        `pr_author` (== a `jailbee new --pr` review container).

        `context` replaces the generic failure warning with a caller-supplied
        one — used on the create path, where a label-write failure means the
        PR already exists on GitHub and the next run would otherwise open a
        second one; the generic message doesn't convey that.
        """
        from jailbee.incus import IncusError

        try:
            self._incus.config_set(self._full, f"{self._prefix}_branch", head)
            if author:
                self._incus.config_set(self._full, f"{self._prefix}_author", "1")
            if adopted:
                self._incus.config_set(self._full, f"{self._prefix}_adopted", "1")
            if number is not None:
                self._incus.config_set(self._full, self._prefix, str(number))
        except IncusError as exc:
            warn(f"{context or 'Could not record the PR decision'}: {exc}")


@dataclass(frozen=True)
class ReviewTarget:
    """What a `jailbee new --pr` container's run publishes.

    Two outcomes, settled once per container: push the container's commits to
    the reviewed PR's own head (`stacked=False` — the PR is updated), or open a
    new PR *based on* that head (`stacked=True` — a stacked PR, whose own head
    branch is named later by `--as` or by Claude).

    `parent_head` is the reviewed PR's head branch: the branch to publish to
    when adopting, the base branch of the new PR when stacking.
    `parent_head_ref` is the host ref holding its tip — jailbee's own
    `refs/jailbee/pr/<N>/head`, since the head deliberately lives in no branch
    on the host — and is what anchors the container's base after a retarget.
    """

    stacked: bool
    parent_number: int
    parent_head: str
    parent_head_ref: str


def _pick_review_action(number: int, head: str) -> str | None:
    """Open a questionary.select for a review container's publish target.

    Returns 'adopt' / 'stacked', or None if the user cancels.
    """
    import questionary

    # Explicit sentinel: `questionary.Choice` treats `value=None` as *unset* and
    # falls back to the title, so a cancel entry with `value=None` would answer
    # the string "cancel" and get published as an action.
    cancel = "__cancel__"
    choices = [
        questionary.Choice(
            title=f"push these commits to PR #{number}'s head '{head}'", value="adopt"
        ),
        questionary.Choice(title=f"open a NEW PR based on '{head}'  (stacked)", value="stacked"),
        questionary.Choice(title="cancel", value=cancel),
    ]
    result = questionary.select("What should jailbee publish?", choices=choices).ask()
    # `None` is Ctrl-C; `cancel` is the menu entry. Both abort.
    if result is None or result == cancel:
        return None
    return str(result)


def resolve_review_target(
    scope: PrScope,
    state: PrState,
    short: str,
    record: PrRecord,
    *,
    yes: bool,
    stacked: bool,
    as_name: str | None = None,
) -> ReviewTarget | None:
    """Settle what a `jailbee new --pr` container publishes; None when moot.

    A review container carries `user.jailbee.pr` without `user.jailbee.pr_author`.
    Publishing its commits to PR #N's head is a legitimate move — the PR is
    often the user's own, opened from another container or machine — but it
    mutates a PR jailbee did not create, so it is confirmed once and then
    recorded on `user.jailbee.pr_adopted`. Later runs take the ordinary update
    path silently, which is what `None` means here.

    `stacked` (the `--stacked` flag) skips the question and settles on a new PR
    based on the reviewed head. Nothing is recorded for that outcome: the
    stacked PR does not exist until `pr.create_pr` opens it, and its labels are
    written from the create path under `STACKED_LABEL_PREFIX`.

    Off a TTY the choice must come from a flag: `--yes` adopts (its meaning
    before `--stacked` existed, and the only one that keeps a script updating
    the reviewed PR), `--stacked` opens the new PR. Exits non-zero on a fork
    PR, on a `gh` failure, and on a declined or unavailable choice.
    """
    from jailbee import pr as pr_module
    from jailbee.lifecycle import _stdin_is_interactive

    if record.number is None:
        if stacked:
            error(
                f"--stacked needs a review container: '{short}' was not created "
                f"from a PR (no user.jailbee.pr label), so there is no PR head to "
                f"stack onto. Open an ordinary PR with '{scope.command} {short}', or "
                f"base the container on the branch you want with "
                f"'jailbee new <branch> <base>'."
            )
            raise typer.Exit(2)
        return None
    # `pr_branch` alone does NOT count as decided: a lone branch label means a
    # previous adoption was interrupted between the two label writes, and
    # asking again is the safe outcome.
    if record.author or record.adopted:
        if stacked:
            error(
                f"--stacked cannot be combined with {scope.noun(str(record.number))}: "
                f"'{short}' already publishes to that PR's head, which is fixed. A "
                f"stacked PR needs a head branch of its own, so the two are "
                f"mutually exclusive on one container."
            )
            raise typer.Exit(2)
        return None

    number = record.number

    # Settle the flag-driven action before the `gh` call, so a usage error
    # costs no round-trip: adopting publishes to the reviewed PR's own head,
    # which `--as` cannot rename.
    action: str | None = "stacked" if stacked else ("adopt" if yes else None)
    if action == "adopt" and as_name is not None:
        reject_as_on_pr_update(scope, as_name, str(number), stacked_hint=True)

    try:
        pr_info = pr_module.resolve_pr(scope.repo_root, number, remote=scope.remote)
    except pr_module.PrError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    # Unconditional — a `--yes` or `--stacked` run must also state which PR it
    # is about to act on, BEFORE the push rather than after it.
    author = f"@{pr_info.author_login}" if pr_info.author_login else "an unknown author"
    info(
        f"Container '{short}' was created from PR #{number} by {author} "
        f"({pr_info.state}); head '{pr_info.head_ref}' → base '{pr_info.base_ref}'."
    )

    if action is None:
        if not _stdin_is_interactive():
            error(
                f"Container '{short}' was created from PR #{number}. Publishing needs "
                f"a choice: --yes pushes its commits to that PR's head, --stacked opens "
                f"a new PR based on it. Pass one when there is no terminal to ask on."
            )
            raise typer.Exit(1)
        action = _pick_review_action(number, pr_info.head_ref)
        if action is None:
            raise typer.Abort()
        # The same rule as above, now that the menu has settled the action —
        # still before anything is recorded or pushed.
        if action == "adopt" and as_name is not None:
            reject_as_on_pr_update(scope, as_name, str(number), stacked_hint=True)

    if pr_info.is_cross_repository:
        owner = pr_info.head_repo_owner or "<fork-owner>"
        if action == "stacked":
            error(
                f"PR #{number}'s head branch lives in the fork '{owner}', so it is "
                f"not a branch in this origin and cannot be the base of a PR opened "
                f"here. Open the stacked PR in the fork instead, against "
                f"'{owner}:{pr_info.head_ref}'."
            )
        else:
            error(
                f"PR #{number}'s head branch lives in the fork '{owner}'. Pushing to "
                f"origin would create an unrelated branch there and leave the PR "
                f"untouched, so jailbee refuses.\n"
                f"To update a fork PR you need push access to the fork (the PR's "
                f"'maintainer can modify' box); then push by hand:\n"
                f"  jailbee git fetch {short}\n"
                f"  git push git@github.com:{owner}/<repo>.git "
                f"refs/jailbee/{short}/<branch>:refs/heads/{pr_info.head_ref}"
            )
        raise typer.Exit(1)

    if pr_info.state != "OPEN":
        if action == "stacked":
            warn(
                f"PR #{number} is {pr_info.state}; basing a new PR on "
                f"'{pr_info.head_ref}' anyway (a merged PR's head branch is often "
                f"deleted, which GitHub will reject as a base)."
            )
        else:
            warn(f"PR #{number} is {pr_info.state}; updating its head anyway.")

    target = ReviewTarget(
        stacked=action == "stacked",
        parent_number=number,
        parent_head=pr_info.head_ref,
        parent_head_ref=pr_module.pr_head_ref(number),
    )
    if target.stacked:
        return target

    # pr_branch FIRST — see `ContainerLabelState.record`: an adopted flag
    # without a recorded head name would make the next run publish to the
    # container branch. Best-effort, like the create path's label writes.
    # `number=None` leaves the recorded PR number alone — `new` already wrote it.
    state.record(
        head=pr_info.head_ref,
        author=False,
        adopted=True,
        number=None,
        context=f"Could not record the PR-head decision on '{short}'",
    )
    return target


def record_stacked_base(incus: IncusType, full: str, short: str, base: str) -> None:
    """Remember which branch a stacked PR was opened against, best-effort.

    `ContainerLabelState` records the head, the number and the authorship; the
    base is jailbee's own, so it gets its own label. Later runs take the update
    path, where the base is no longer settled from the reviewed PR — they read
    it here instead of paying a `gh` call, and it stays correct even when the
    user declined the retarget and `user.jailbee.base_branch` still names the
    reviewed PR's own base.
    """
    from jailbee.incus import IncusError

    try:
        incus.config_set(full, f"{STACKED_LABEL_PREFIX}_base", base)
    except IncusError as exc:
        warn(
            f"Could not record the stacked PR's base branch on '{short}': {exc}. "
            f"A later run will fall back to the container's base branch label."
        )


def maybe_retarget_to_parent(
    cfg: Config,
    incus: IncusType,
    full: str,
    short: str,
    target: ReviewTarget,
    *,
    retarget: bool | None,
) -> None:
    """Offer to re-point the container's base branch at the stacked PR's base.

    A review container's base anchor is the reviewed PR's *base* (e.g. `dev`),
    so `jailbee ls`'s AHEAD and `jailbee git diff` fold the reviewed PR's own
    commits into this container's numbers. Moving the anchor to the PR head
    makes them count the stacked work alone, and points `jailbee git pull` at
    the branch this work belongs on.

    `retarget` is the `--retarget/--no-retarget` flag: None asks on a TTY and
    otherwise skips with the command printed. Never raises — the PR is already
    published by the time this runs, so a transport failure is a warning.
    """
    from jailbee import sync
    from jailbee.lifecycle import _stdin_is_interactive

    if retarget is False:
        return
    old_base = incus.config_get(full, "user.jailbee.base_branch")
    if old_base == target.parent_head:
        return
    old_desc = f"'{old_base}'" if old_base else "unset"
    if retarget is None:
        if not _stdin_is_interactive():
            info(
                f"Base branch left at {old_desc}; AHEAD still counts PR "
                f"#{target.parent_number}'s own commits. To move it: "
                f"git fetch {cfg.upstream_remote} && jailbee git retarget {short} "
                f"{target.parent_head}"
            )
            return
        if not typer.confirm(
            f"Retarget '{short}' base branch {old_desc} → '{target.parent_head}', "
            f"so AHEAD and diffs count only this container's own commits?",
            default=True,
        ):
            return
    try:
        result = sync.retarget_container(
            cfg, incus, short, target.parent_head, source_ref=target.parent_head_ref
        )
    except sync.SyncError as exc:
        warn(f"Could not retarget '{short}': {exc}")
        return
    success(f"Retargeted '{short}' base: {result.old_base or 'unset'} → {result.new_base}")


def adopt_existing_pr_for_branch(
    scope: PrScope,
    state: PrState,
    *,
    branch: str | None,
    yes: bool,
    record_context: str,
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

    `record_context` names where this landed (e.g. ``"on 'feat-foo'"`` or
    ``"for submodule 'lib/a' on 'feat-foo'"``) — required, not optional, so a
    label-write failure here always tells the user the PR number and where to
    look, not just a generic "could not record" with no way to fix it by hand.
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

    state.record(
        head=found.head_ref,
        author=False,
        adopted=True,
        number=found.number,
        context=f"Could not record PR #{found.number} {record_context}",
    )

    return found.number, found.head_ref


def bind_pr_by_number(
    scope: PrScope,
    state: PrState,
    *,
    number: int,
    record: PrRecord,
    yes: bool,
    record_context: str,
) -> PrRecord:
    """Bind this target to PR ``number``, and return the record to run under.

    `adopt_existing_pr_for_branch` can only find a PR whose head branch is
    *named* like the container's branch. A container made with `jailbee new
    <branch>` where that branch has nothing to do with the PR head — the
    common case when the PR was opened from a differently-named branch — is
    invisible to that lookup, and the create path would open a second PR for
    the same work. Naming the number is the only thing that identifies it.

    Records the resolved head and ``adopted`` but deliberately not ``author``,
    exactly like name-based adoption: jailbee did not open this PR, so the
    foreign-head guards (`--force` confirmation, no description regeneration)
    must stay on.

    Already bound to ``number`` -> returns ``record`` untouched, without a `gh`
    call or a prompt; re-running with the same ``--pr`` is not a decision.
    Bound to a *different* number -> confirms a retarget, which is also the
    only way back out of a mistyped number.

    Exits non-zero on an unresolvable, closed/merged, or fork PR, and on a
    declined or unavailable confirmation.
    """
    from jailbee import pr as pr_module
    from jailbee.lifecycle import _stdin_is_interactive

    if record.number == number and record.head:
        return record

    try:
        found = pr_module.resolve_pr(scope.repo_root, number, remote=scope.remote)
    except pr_module.PrError as exc:
        error(f"{scope.prefix}{exc}")
        raise typer.Exit(1) from exc

    if found.state != "OPEN":
        error(
            f"{scope.prefix}PR #{number} is {found.state}. Reopen it, or drop --pr "
            f"to open a new PR from this container."
        )
        raise typer.Exit(1)
    if found.is_cross_repository:
        owner = found.head_repo_owner or "<fork-owner>"
        error(
            f"{scope.prefix}PR #{number}'s head '{found.head_ref}' lives in the fork "
            f"'{owner}', which jailbee cannot push to. Push to the fork yourself, or "
            f"drop --pr to open a PR from this container."
        )
        raise typer.Exit(1)

    author = f"@{found.author_login}" if found.author_login else "an unknown author"
    info(
        f"{scope.prefix}PR #{number} by {author} (OPEN); head '{found.head_ref}' → "
        f"base '{found.base_ref}'."
    )

    if record.number is not None and record.number != number:
        question = (
            f"Already bound to PR #{record.number}; retarget to #{number} ('{found.head_ref}')?"
        )
        refusal = (
            f"{scope.prefix}Retargeting from PR #{record.number} to #{number} needs "
            f"confirmation — re-run with --yes when there is no terminal to ask on."
        )
    else:
        question = f"Push this container's commits to PR #{number} ('{found.head_ref}')?"
        refusal = (
            f"{scope.prefix}Pushing this container's commits to PR #{number} needs "
            f"confirmation — re-run with --yes when there is no terminal to ask on."
        )

    if not yes:
        if not _stdin_is_interactive():
            error(refusal)
            raise typer.Exit(1)
        if not typer.confirm(question, default=True):
            raise typer.Abort()

    state.record(
        head=found.head_ref,
        author=False,
        adopted=True,
        number=number,
        context=f"Could not record PR #{number} {record_context}",
    )
    # In-process, not a re-read: `state.record` is best-effort, and the run
    # must publish to the right head even when the write failed.
    return PrRecord(number=number, head=found.head_ref, author=False, adopted=True)


def resolve_create_text(
    scope: PrScope,
    *,
    ai_on: bool,
    ai_text: PrText | None,
    title: str | None,
    body: str | None,
    fallback_ref: str,
    publish_name: str,
    origin_label: str,
) -> tuple[str, str]:
    """Resolve the create path's title/body.

    Explicit `title`/`body` win. Otherwise, when `ai_on` and AI text was
    generated, it fills whichever side was not given explicitly. A title still
    missing falls back to the commit subject at `fallback_ref`, then to
    `publish_name`. A body still missing falls back to a placeholder naming
    `origin_label` (e.g. ``container 'feat-foo'``).
    """
    from jailbee import git as git_mod

    resolved_title = title
    resolved_body = body
    if ai_on and ai_text is not None:
        if title is None:
            resolved_title = ai_text.title
        if body is None:
            resolved_body = ai_text.body
    if resolved_title is None:
        resolved_title = git_mod.commit_subject(scope.repo_root, fallback_ref) or publish_name
    if resolved_body is None:
        resolved_body = f"Draft PR created by jailbee from {origin_label}. Description pending."
    return resolved_title, resolved_body


def create_or_view_pr(
    scope: PrScope,
    state: PrState,
    *,
    is_update: bool,
    head: str,
    base: str,
    title: str,
    body: str,
    draft: bool,
    label: str,
    record_context: str | None = None,
) -> PrCreated:
    """Return the container's PR: an existing one on update, else a new one.

    On update, `head`'s existing PR is looked up with `pr.view_existing_pr` —
    `base`/`title`/`body`/`draft`/`label` are unused on that path. On create,
    `pr.create_pr` opens the PR and the authorship is recorded via
    `state.record`. Raises `pr.PrError` for the caller to map to a CLI exit.

    `record_context` is a description of what a label-write failure would mean
    (e.g. ``"failed to record the PR label on 'feat-foo'"``); when given, the
    PR number (only known after creation) is prepended and forwarded as
    `state.record`'s `context=` so the resulting warning still tells the user
    the PR already exists on GitHub. Omitted, `state.record` falls back to its
    own generic message.
    """
    from jailbee import pr as pr_module

    if is_update:
        return pr_module.view_existing_pr(scope.repo_root, head)

    created = pr_module.create_pr(
        scope.repo_root,
        head=head,
        base=base,
        title=title,
        body=body,
        remote=scope.remote,
        draft=draft,
        label=label,
    )
    if record_context is not None:
        state.record(
            head=head,
            author=True,
            adopted=False,
            number=created.number,
            context=f"PR #{created.number} created, but {record_context}",
        )
    else:
        state.record(head=head, author=True, adopted=False, number=created.number)
    return created


@dataclass(frozen=True)
class PrUpdate:
    """What changed while applying updates to an existing PR."""

    title_changed: bool
    body_changed: bool
    state_note: str


def apply_pr_updates(
    cfg: Config,
    incus: IncusType,
    full: str,
    scope: PrScope,
    *,
    number: int,
    branch: str,
    base: str,
    title: str | None,
    body: str | None,
    description: bool,
    ready: bool | None,
    ai_on: bool,
    offer_regen: bool,
) -> PrUpdate:
    """Apply description and ready/draft updates to an already-existing PR.

    Callers only invoke this on the update path (an authored container, or a
    create call that turned out to already exist). Each side is best-effort:
    a failure warns and leaves the corresponding `*_changed`/`state_note`
    untouched rather than raising.
    """
    from jailbee import pr as pr_module

    title_changed = False
    body_changed = False
    edit = resolve_pr_description_update(
        cfg,
        incus,
        full,
        scope,
        branch=branch,
        base=base,
        title=title,
        body=body,
        description=description,
        ai_on=ai_on,
        offer_regen=offer_regen,
    )
    if edit is not None:
        try:
            pr_module.edit_pr(scope.repo_root, number, title=edit[0], body=edit[1])
            title_changed = edit[0] is not None
            body_changed = edit[1] is not None
        except pr_module.PrError as exc:
            warn(f"{scope.prefix}Updating the PR description failed: {exc}")

    state_note = ""
    if ready is not None:
        try:
            pr_module.set_ready(scope.repo_root, number, ready)
            state_note = " (marked ready)" if ready else " (marked draft)"
        except pr_module.PrError as exc:
            warn(f"{scope.prefix}Toggling PR draft state failed: {exc}")

    return PrUpdate(title_changed=title_changed, body_changed=body_changed, state_note=state_note)


def render_pr_outcome(
    scope: PrScope,
    *,
    url: str,
    number: int,
    is_update: bool,
    publish_name: str,
    forced: bool,
    ready: bool | None,
    update: PrUpdate | None,
) -> None:
    """Print the final success line for a create or update outcome.

    `update` is ignored on the create path (and may be None there). On the
    update path a missing `update` (e.g. a detached submodule with no source
    branch to regenerate a description from or a state to toggle) defaults to
    a no-op — nothing changed, so the rendered line says exactly that — rather
    than requiring every caller to construct one just to satisfy this
    function.
    """
    if not is_update:
        kind = "PR" if ready else "Draft PR"
        success(f"{scope.prefix}{kind} #{number} created for '{publish_name}': {url}")
        return

    update = update or PrUpdate(title_changed=False, body_changed=False, state_note="")
    head_note = "head force-pushed (--force-with-lease)" if forced else "head moved"
    if update.title_changed and update.body_changed:
        detail = f"{head_note}, title and description refreshed"
    elif update.body_changed:
        detail = f"{head_note}, description refreshed"
    elif update.title_changed:
        detail = f"{head_note}, title updated"
    else:
        detail = f"{head_note}; description unchanged"
    success(f"{scope.prefix}PR #{number} updated — {detail}.{update.state_note} {url}")
