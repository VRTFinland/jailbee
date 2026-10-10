"""Interactive submodule PR publishing shared by `jailbee submodule pr` and `jailbee pr`.

This flow may prompt and raise typer.Exit or typer.Abort, like pr_flow.
The CLI keeps argument parsing and target picking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import typer

from jailbee import (
    git,
    lifecycle,
    pr_ai,
    pr_flow,
    pr_outbox,
    prompting,
    submodule_pr,
    submodules,
    tui,
)
from jailbee import pr as pr_mod
from jailbee.incus import IncusError
from jailbee.outbox.models import OutboxError
from jailbee.tui import error, info, success, warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.outbox.io import PrManagement
    from jailbee.submodule_pr import SubCandidate, SubmodulePrPlan


@dataclass(frozen=True)
class SubPrOptions:
    title: str | None = None
    body: str | None = None
    base: str | None = None
    ready: bool | None = None
    description: bool = False
    no_ai: bool = False
    no_outbox: bool = False
    branch: str | None = None
    as_name: str | None = None
    pr_number: int | None = None
    force: bool = False
    yes: bool = False
    web: bool = False
    note_merge_order: bool = True


SubPrAction = Literal["created", "updated", "declined", "failed"]


@dataclass(frozen=True)
class SubPrOutcome:
    subpath: str
    action: SubPrAction
    number: int | None = None
    url: str | None = None
    outbox_failures: int = 0


OfferComments = Callable[[int, "PrManagement"], int]  # (pr number, management) -> failures
ConfirmPlan = Callable[["SubmodulePrPlan"], None]  # may raise typer.Abort


def _manifest_subpaths(cfg: Config, incus: Incus, full: str) -> set[str]:
    """Find pending descriptions without making a failed preview block publishing."""
    try:
        outbox = pr_outbox.read_outbox(incus, full, uid=cfg.container_user.uid)
    except pr_outbox.OutboxReadError:
        return set()
    if not outbox.manifest_names:
        return set()
    scopes = [
        (scope.subpath, pr_outbox.scope_slug(scope))
        for scope in pr_flow.candidate_scopes(
            cfg, extra_paths=submodule_pr.recorded_paths(incus, full)
        )
        if scope.subpath is not None
    ]
    paths: set[str] = set()
    for name in outbox.manifest_names:
        try:
            manifest = pr_outbox.parse_manifest(name, outbox.files[name], outbox.files)
        except pr_outbox.ManifestError:
            continue
        try:
            progress = pr_outbox._publication_progress(outbox, name, len(manifest.actions))
        except OutboxError as exc:
            warn(f"Ignoring outbox manifest {name}: {exc}")
            continue
        if any(
            isinstance(manifest.actions[index], pr_outbox.DescriptionAction)
            for index in pr_outbox.pending_indices(manifest, progress)
        ):
            paths.update(path for path, slug in scopes if slug == manifest.repo)
    return paths


def submodule_pr_candidates(
    cfg: Config, incus: Incus, full: str, short: str, *, repo_dir: str, base_branch: str
) -> list[SubCandidate]:
    candidates = submodule_pr.detect_candidates(
        cfg, incus, full, repo_dir=repo_dir, base_branch=base_branch, short=short
    )
    if not candidates:
        return []
    recorded = set(submodule_pr.recorded_paths(incus, full))
    pending = _manifest_subpaths(cfg, incus, full)
    return [
        c for c in candidates if (c.commits or 0) > 0 or c.path in recorded or c.path in pending
    ]


def choose_submodule_prs(candidates: list[SubCandidate], *, yes: bool) -> list[SubCandidate]:
    if not candidates or yes:
        return candidates
    if not prompting.is_interactive():
        warn(
            f"Submodule PR candidates: {', '.join(c.path for c in candidates)}. "
            "Use --yes to publish them first, or --no-submodules to silence this notice."
        )
        return []
    picked = tui.pick_submodules_multi(candidates)
    if picked is None:
        raise typer.Abort()
    return [c for c in candidates if c.path in picked]


def _render_summary(outcomes: list[SubPrOutcome]) -> None:
    for outcome in outcomes:
        label = f"Submodule '{outcome.subpath}': {outcome.action}"
        if outcome.action in ("created", "updated"):
            success(f"{label} #{outcome.number} {outcome.url}")
        elif outcome.action == "declined":
            info(label)
        else:
            warn(label)
        if outcome.outbox_failures:
            warn(
                f"Submodule '{outcome.subpath}': "
                f"{outcome.outbox_failures} outbox publication failures."
            )
    failed = [o.subpath for o in outcomes if o.action == "failed"]
    if failed:
        warn(
            f"The superproject PR's gitlink for {', '.join(failed)} may point at a commit "
            "that is not on that submodule's remote."
        )
    if any(o.number is not None for o in outcomes):
        info(
            "Merge the submodule PRs first; the superproject PR's gitlink bump "
            "then points at merged commits."
        )


def publish_submodule_prs_first(
    cfg: Config,
    incus: Incus,
    full: str,
    short: str,
    *,
    enabled: bool,
    yes: bool,
    no_ai: bool,
    no_outbox: bool,
    ready: bool | None,
    offer_comments: OfferComments,
    management: PrManagement | None = None,
) -> list[SubPrOutcome]:
    if not enabled:
        return []
    repo_dir = lifecycle.container_repo_dir(cfg, incus, full)
    base = incus.config_get(full, "user.jailbee.base_branch") or cfg.default_branch
    try:
        candidates = submodule_pr_candidates(
            cfg, incus, full, short, repo_dir=repo_dir, base_branch=base
        )
    except submodule_pr.SubmodulePrError as exc:
        warn(f"Could not inspect submodules: {exc}; publishing the superproject PR only.")
        return []
    if candidates and not yes and prompting.is_interactive():
        for candidate in candidates:
            record = submodule_pr.SubmodulePrState(incus, full, candidate.path).read()
            action = f"update PR #{record.number}" if record.number is not None else "create PR"
            info(f"Submodule '{candidate.path}': {action}")
    chosen = choose_submodule_prs(candidates, yes=yes)
    outcomes: list[SubPrOutcome] = []
    for candidate in sorted(chosen, key=lambda c: c.path):
        info(f"Submodule '{candidate.path}':")
        try:
            outcome = publish_submodule_pr(
                cfg,
                incus,
                full,
                short,
                candidate,
                SubPrOptions(
                    ready=ready, no_ai=no_ai, no_outbox=no_outbox, yes=yes, note_merge_order=False
                ),
                repo_dir=repo_dir,
                confirm_plan=None,
                offer_comments=offer_comments,
                management=management,
            )
        except typer.Abort:
            outcome = SubPrOutcome(candidate.path, "declined")
        except typer.Exit:
            outcome = SubPrOutcome(candidate.path, "failed")
        except (
            git.GitError,
            IncusError,
            submodule_pr.SubmodulePrError,
            submodules.SubmoduleError,
        ) as exc:
            warn(f"Submodule '{candidate.path}': {exc}")
            outcome = SubPrOutcome(candidate.path, "failed")
        outcomes.append(outcome)
    if outcomes:
        _render_summary(outcomes)
    return outcomes


def publish_submodule_pr(
    cfg: Config,
    incus: Incus,
    full: str,
    short: str,
    target: SubCandidate,
    opts: SubPrOptions,
    *,
    repo_dir: str,
    confirm_plan: ConfirmPlan | None,
    offer_comments: OfferComments,
    management: PrManagement | None = None,
) -> SubPrOutcome:
    subpath = target.path
    source_branch = opts.branch or target.branch

    # Read the recorded PR state here rather than after the transport: it is
    # one `incus config get`, it never touches the host sub-repo, and it
    # decides whether the plan says "create" or "update". Only these two lines
    # move up — `remote`, `resolved_base`, `scope`, the `--pr N` binding and
    # `pr_label` all stay below the transport, where they belong.
    state = submodule_pr.SubmodulePrState(incus, full, subpath)
    record = state.read()

    # base/remote only when the host sub-repo already exists: resolving them
    # for a submodule the host has never seen would both misreport (the
    # resolvers fall back to `origin`/`main`) and break the FIX 2 invariant
    # that the transport is the first thing to touch that directory. The
    # authoritative resolution stays after the transport, untouched.
    on_host = submodules.host_subrepo_exists(cfg.repo_root, subpath)
    plan_remote = submodule_pr.resolve_remote(cfg.repo_root, subpath) if on_host else None
    plan_base = opts.base or (
        submodule_pr.resolve_base_branch(cfg.repo_root, subpath, override=None) if on_host else None
    )

    notes: list[str] = []
    if target.dirty:
        notes.append("the submodule has uncommitted changes — they are NOT in the PR")
    if target.gitlink_stale:
        notes.append("the superproject's gitlink does not yet point at these commits")
    if target.commits is None:
        notes.append("the commit count could not be resolved (no base anchor)")
    if target.commits == 0:
        notes.append("this submodule has no commits ahead of its base")

    if confirm_plan is not None and not opts.yes:
        # `--pr N` binds to an existing PR *below*, after this confirmation,
        # so without it here the line the user approves would promise a new
        # PR and then update one.
        plan_action: Literal["create", "update"] = (
            "update" if (record.author or record.head or opts.pr_number is not None) else "create"
        )
        confirm_plan(
            submodule_pr.SubmodulePrPlan(
                container_short=short,
                container_full=full,
                subpath=subpath,
                source_branch=source_branch,
                commits=target.commits,
                action=plan_action,
                base=plan_base,
                remote=plan_remote,
                # Create path: a new PR is a draft unless --ready. Update
                # path: apply_pr_updates only touches draft state when
                # --ready/--draft was given, so `ready` itself (None included)
                # is the true outcome — anything else would misreport.
                draft=(opts.ready is not True) if plan_action == "create" else opts.ready,
                notes=tuple(notes),
            )
        )

    # Step 2 of the spec's pipeline: transport this submodule's objects to
    # the host BEFORE anything below reads the host sub-repo. For a
    # submodule the host has never seen (added inside the container, or a
    # host clone where `git submodule update --init` never ran for this
    # path), the sub-repo does not exist until this call clones it — see
    # `submodule_pr.transport_submodule_to_host`'s docstring.
    submodule_pr.transport_submodule_to_host(
        cfg, incus, full, short, subpath=subpath, repo_dir=repo_dir
    )

    scope = pr_flow.PrScope.for_submodule(cfg, subpath)
    remote = scope.remote
    resolved_base = submodule_pr.resolve_base_branch(cfg.repo_root, subpath, override=opts.base)
    if opts.pr_number is not None:
        # After the transport, not before: for a submodule the host has never
        # seen, `scope.repo_root` does not exist as a git repo until the
        # transport clones it — and `resolve_pr` runs `git remote get-url`
        # there.
        record = pr_flow.bind_pr_by_number(
            scope,
            state,
            number=opts.pr_number,
            record=record,
            yes=opts.yes,
            record_context=f"for submodule '{subpath}' on '{short}'",
        )
    pr_label = str(record.number) if record.number is not None else None

    if opts.as_name is not None and (record.author or record.head or pr_label):
        pr_flow.reject_as_on_pr_update(scope, opts.as_name, pr_label)

    if target.commits is None:
        warn(
            f"Could not count submodule '{subpath}''s commits (no base anchor and "
            f"no {remote}/HEAD); publishing what it has."
        )
    if target.gitlink_stale:
        info(
            f"Submodule '{subpath}''s commits are not yet in the superproject's "
            f"gitlink — commit the bump there when this PR is ready."
        )
    if target.dirty:
        warn(f"Submodule '{subpath}' has uncommitted changes — they are NOT in the PR.")

    is_update = bool(record.author or record.head)
    if not is_update and opts.as_name is None:
        found = pr_flow.adopt_existing_pr_for_branch(
            scope,
            state,
            branch=source_branch,
            yes=opts.yes,
            record_context=f"for submodule '{subpath}' on '{short}'",
        )
        if found is not None:
            pr_label = str(found[0])
            is_update = True
            # Build the record in-process rather than re-reading it: `state.record`
            # (called by `adopt_existing_pr_for_branch`) is best-effort, and a
            # failed write would otherwise make `state.read()` hand back a blank
            # `PrRecord` here — `record.head is None` then makes
            # `resolve_pr_text_and_head` treat this as a headless detached
            # submodule and fail with a nonsense usage error, even though the
            # user just confirmed adopting a real PR. Same anti-pattern
            # `jailbee pr`'s adoption path avoids above (see the "Use the value
            # in-process" comment near `_adopt_pr_head`).
            record = pr_flow.PrRecord(number=found[0], head=found[1], author=False, adopted=True)

    is_foreign = bool(pr_label) and not record.author
    if opts.force and pr_label and not record.author:
        pr_flow.confirm_foreign_force_push(scope, short, pr_label, record.head, yes=opts.yes)

    from jailbee.outbox.io import PrManagement
    from jailbee.outbox.models import OutboxError

    management = management if management is not None else PrManagement()
    try:
        with pr_flow.outbox_publication_guard(
            cfg, incus, full, enabled=not opts.no_outbox, management=management
        ):
            plan = pr_flow.resolve_pr_text_and_head(
                cfg,
                incus,
                full,
                scope,
                is_update=is_update,
                stored_head=record.head,
                source_branch=source_branch,
                base=resolved_base,
                title=opts.title,
                body=opts.body,
                as_name=opts.as_name,
                no_ai=opts.no_ai,
                status_label=f"Generating PR title/description with Claude in '{short}:{subpath}'…",
                use_outbox=not opts.no_outbox,
            )
            pr_flow.bind_outbox_source(management, plan.outbox_source)
            publish_name = plan.publish_name
            if publish_name is None:
                error(
                    f"Submodule '{subpath}' is detached in '{short}' and no head branch name "
                    f"was chosen. Name one with --as, or pass --branch to publish an "
                    f"existing submodule branch."
                )
                raise typer.Exit(2)

            # Publish step 4 of the spec: the submodule's own upstream must be a GitHub
            # one, checked BEFORE anything is pushed. `create_pr` validates too, but
            # only after the branch is already on the remote.
            try:
                pr_mod.assert_github_remote(scope.repo_root, remote, label="jailbee submodule pr")
            except pr_mod.PrError as exc:
                error(str(exc))
                raise typer.Exit(1) from exc

            try:
                published = submodule_pr.publish_submodule_branch(
                    cfg,
                    short,
                    subpath=subpath,
                    branch=source_branch,
                    publish_name=publish_name,
                    remote=remote,
                    force=opts.force,
                )
            except submodule_pr.SubmodulePrError as exc:
                error(str(exc))
                raise typer.Exit(1) from exc

            ai_on = pr_ai.ai_description_on(cfg, no_ai=opts.no_ai)
            text_on = ai_on or plan.outbox_source is not None
            resolved_title, resolved_body = ("", "")
            if not is_update:
                resolved_title, resolved_body = pr_flow.resolve_create_text(
                    scope,
                    ai_on=text_on,
                    ai_text=plan.ai_text,
                    title=opts.title,
                    body=opts.body,
                    fallback_ref=published.src_ref,
                    publish_name=published.publish_name,
                    origin_label=f"container '{short}' submodule '{subpath}'",
                )
            if not is_update:
                pr_flow.bind_outbox_source(management, plan.outbox_source)
                pr_flow.validate_outbox_source(cfg, incus, full, plan.outbox_source)
            try:
                created = pr_flow.create_or_view_pr(
                    scope,
                    state,
                    use_outbox=not opts.no_outbox,
                    is_update=is_update,
                    head=published.publish_name,
                    base=resolved_base,
                    title=resolved_title,
                    body=resolved_body,
                    draft=opts.ready is not True,
                    label="jailbee submodule pr",
                    record_context=(
                        f"failed to record the PR label for submodule '{subpath}' on '{short}'"
                    ),
                )
            except pr_mod.PrError as exc:
                error(str(exc))
                raise typer.Exit(1) from exc

            did_update = is_update or created.already_existed
            update = None
            if did_update and source_branch:
                update = pr_flow.apply_pr_updates(
                    cfg,
                    incus,
                    full,
                    scope,
                    number=created.number,
                    branch=source_branch,
                    base=resolved_base,
                    title=opts.title,
                    body=opts.body,
                    description=opts.description,
                    ready=opts.ready,
                    ai_on=ai_on,
                    foreign_head=is_foreign,
                    url=created.url,
                    use_outbox=not opts.no_outbox,
                    outbox_hint=plan.outbox_source,
                    management=management,
                )
            elif did_update:
                # The submodule is detached and no --branch resolved a source: there
                # is no branch to regenerate a description from or a state to toggle
                # against. `render_pr_outcome` defaults a missing `update` to a no-op
                # on the update path, so nothing further is needed here beyond the
                # user-facing warning — and only when the user actually asked for
                # something that needed the missing branch; a bare re-run with no
                # such flag has nothing to silently ignore.
                if (
                    opts.description
                    or opts.title is not None
                    or opts.body is not None
                    or opts.ready is not None
                ):
                    warn(
                        f"{scope.prefix}--description/--title/--body/--ready/--draft "
                        f"could not be applied to PR #{created.number}: the submodule "
                        f"is detached and no source branch was resolved. Pass --branch "
                        f"to select one."
                    )
            pr_flow.render_pr_outcome(
                scope,
                url=created.url,
                number=created.number,
                is_update=did_update,
                publish_name=published.publish_name,
                forced=published.forced,
                ready=opts.ready,
                update=update,
            )
            if not did_update:
                pr_flow.record_outbox_consumption(cfg, incus, full, plan.outbox_source, created.url)
            if opts.note_merge_order and incus.config_get(full, "user.jailbee.pr"):
                info(
                    "Merge this submodule PR first; the superproject PR's gitlink bump "
                    "then points at a merged commit."
                )
            outbox_failures = 0 if opts.no_outbox else offer_comments(created.number, management)
            if opts.web:
                pr_mod.open_pr_in_browser(scope.repo_root, created.number)
            return SubPrOutcome(
                subpath=subpath,
                action="updated" if did_update else "created",
                number=created.number,
                url=created.url,
                outbox_failures=outbox_failures,
            )
    except OutboxError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
