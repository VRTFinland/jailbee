"""The bodies of `jailbee pr push` and `jailbee submodule pr push`.

`cli.py` stays argument parsing and delegation: it loads the config and resolves
the container, then calls these. `outbox apply` calls them the same way for a PR
creation, so neither goes through a Typer command function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from jailbee.tui import error, info, info_plain, success, warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus as IncusType
    from jailbee.outbox.io import PrManagement
    from jailbee.pr import PrInfo
    from jailbee.submodule_pr import SubCandidate, SubmodulePrPlan
    from jailbee.sync import FetchResult, PublishResult


def print_fetch_summary(cfg: Config, short: str, result: FetchResult) -> None:
    """Print the same summary `jailbee git fetch` prints, reused by checkout/pull.

    Every line here embeds a ref (which embeds the branch name) or a commit
    subject — both arbitrary, uncontrolled text — so all of it goes through
    `info_plain`, not `info`: Rich's markup parser silently deletes bracketed
    content, so a branch named `feat/[wip]` (or a commit subject containing
    `[...]`) would otherwise render with the bracketed part gone.
    """
    from jailbee import git as git_helpers

    ref = f"refs/jailbee/{short}/{result.branch}"
    if result.commits_added == 0:
        info_plain(f"{ref}: already up to date ({result.new_oid[:7]}).")
        return

    if result.old_oid is None:
        if result.base_oid is not None:
            info_plain(
                f"{ref}: new ref at {result.new_oid[:7]} "
                f"({result.commits_added} commit(s) ahead of HEAD)."
            )
            range_spec = f"{result.base_oid}..{result.new_oid}"
        else:
            info_plain(f"{ref}: fetched {result.commits_added} commit(s).")
            range_spec = result.new_oid
    else:
        info_plain(
            f"{ref}: {result.old_oid[:7]}..{result.new_oid[:7]} "
            f"({result.commits_added} new commits)"
        )
        range_spec = f"{result.old_oid}..{result.new_oid}"

    for line in git_helpers.log_oneline(cfg.repo_root, range_spec):
        info_plain(f"  {line}")


def print_publish_progress(cfg: Config, short: str, publish: PublishResult) -> None:
    """Report the container fetch, then announce the push about to run.

    Wired into `sync.publish_branch_from_container` as its `on_before_push`
    hook, so everything `jailbee pr` did before the push is on screen before the
    push starts. `git push` inherits its output and prints nothing until the
    remote answers, so without this the terminal's last line is git's own fetch
    output and a push waiting on remote authentication looks like a hung fetch.
    """
    print_fetch_summary(cfg, short, publish.fetch)
    if publish.dirty:
        warn(f"Container '{short}' has uncommitted changes — they are NOT included in the PR.")
    info(f"Pushing '{publish.publish_name}' to {cfg.upstream_remote}…")


def offer_outbox_comments(
    cfg: Config,
    incus: IncusType,
    container: str,
    short: str,
    *,
    number: int,
    management: PrManagement | None = None,
) -> int:
    """Offer to publish the PR comments the container still has pending.

    Called once `jailbee pr` has pushed and the PR exists, so the plan, the
    single confirmation and the publishing loop are `jailbee review apply`'s —
    `pr_outbox.offer_pending_comments`, which both commands share. Returns the
    number of failures for the caller to exit on rather than raising: the PR's
    URL is already on screen, and nothing a container wrote into its outbox may
    retract that.

    Off a TTY the offer degrades to a hint naming the count and the command.
    The predicate is `prompting.is_interactive` — the one
    `pr_flow._can_prompt` already consults for this command's other prompts,
    not a new one of its own.
    """
    from jailbee import pr_outbox, prompting
    from jailbee.outbox.publish import ask_publish_mode

    def _confirm(count: int) -> bool:
        plural = "" if count == 1 else "s"
        return prompting.ask_confirm(f"Post {count} pending PR comment{plural} now?", default=False)

    return pr_outbox.offer_pending_comments(
        cfg,
        incus,
        container,
        short,
        pr_number=number,
        confirm=_confirm,
        can_prompt=prompting.is_interactive(),
        choose_mode=ask_publish_mode if prompting.is_interactive() else None,
        management=management,
        publisher="user",
    )


def print_submodule_pr_candidates(candidates: list[SubCandidate]) -> None:
    """List the submodules that have commits to publish, one per line.

    Renders through `submodule_pr.describe_candidate`, the same function the
    TTY picker uses, so the two cannot drift. `markup=False` because a commit
    subject is user data and may contain Rich markup characters.
    """
    from jailbee.submodule_pr import describe_candidate
    from jailbee.tui import console

    width = max((len(c.path) for c in candidates), default=0)
    for c in candidates:
        console.print(f"  {describe_candidate(c, width=width)}", markup=False, highlight=False)


def confirm_submodule_pr_plan(plan: SubmodulePrPlan) -> None:
    """On a TTY, show a submodule-PR plan and ask whether to go ahead.

    ``markup=False`` because branch names and commit subjects are user data
    and may contain Rich markup characters. Declining raises
    ``typer.Abort()``; nothing has been mutated at that point, which is why
    this runs before the transport rather than after it.

    Off a TTY this prints *nothing* and returns — deliberately unlike
    :func:`_confirm_bridge_plan`, which shows its block either way and skips
    only the prompt. `jailbee submodule pr` is specified to leave every
    behaviour, message and exit code unchanged off a TTY, so that a script
    parsing its output does not suddenly find a new block in it. Nothing is
    lost: the plan block is a confirmation artifact, there is no one to
    confirm, and the facts that matter (a dirty submodule, a stale gitlink, an
    unresolved commit count) are still reported by the command's own
    ``warn``/``info`` calls further down. Do not "fix" this back.
    """
    from jailbee import prompting
    from jailbee.tui import console, render_submodule_pr_plan

    if not prompting.is_interactive():
        return
    console.print(render_submodule_pr_plan(plan), markup=False, highlight=False)
    if not prompting.ask_confirm("Continue?", default=True):
        raise typer.Abort()


def run_pr_push(
    cfg: Config,
    incus: IncusType,
    full: str,
    *,
    title: str | None = None,
    body: str | None = None,
    base: str | None = None,
    ready: bool | None = None,
    description: bool = False,
    web: bool = False,
    ai: bool = False,
    no_ai: bool = False,
    no_outbox: bool = False,
    branch: str | None = None,
    as_name: str | None = None,
    pr_number: int | None = None,
    force: bool = False,
    yes: bool = False,
    stacked: bool = False,
    retarget: bool | None = None,
    source_manifest: str | None = None,
    no_submodules: bool = False,
) -> None:
    """Publish the container's branch (or a pinned source manifest) and open/update its PR."""
    from jailbee import git as git_mod
    from jailbee import pr as pr_mod
    from jailbee import pr_flow, sync
    from jailbee.lifecycle import short_name

    consent = pr_flow.AiConsent(ai=ai, no_ai=no_ai)

    from jailbee import branch_pr, prompting

    selected_own: str | None = None

    def publish_source(manifest: str) -> bool:
        nonlocal selected_own, branch
        from jailbee.outbox.service import instance_identity
        from jailbee.tui import warn_plain

        _, uuid = instance_identity(incus, full)
        source = branch_pr.stored_source(uuid, manifest)
        own = branch_pr.own_source_branch(cfg, incus, full, source.manifest) if source else None
        if own is not None and source is not None and source.manifest.actions:
            selected_own, branch = manifest, own[0]
            if own[1]:
                warn_plain(f"{manifest}: {own[1]} newer commits; pushing the tip of {branch}")
            return False
        result = branch_pr.publish_source_manifest(
            cfg,
            incus,
            short_name(cfg, full),
            manifest,
            force=force,
            yes=yes,
            confirm=lambda question: prompting.ask_confirm(question, default=False),
            publisher="user",
        )
        if result:
            raise typer.Exit(result)
        return True

    if source_manifest is not None and publish_source(source_manifest):
        return

    short = short_name(cfg, full)
    pending = (
        [] if source_manifest is not None else branch_pr.pending_source_manifests(cfg, incus, full)
    )
    if (
        len(pending) == 1
        and pending[0].own_branch
        and not no_outbox
        and branch is None
        and pr_number is None
        and as_name is None
        and not stacked
        and title is None
        and body is None
    ):
        if publish_source(pending[0].manifest):
            return
        pending = []
    if pending:
        own_options = any(
            (
                title is not None,
                body is not None,
                base is not None,
                ready is not None,
                description,
                web,
                no_ai,
                ai,
                no_outbox,
                branch is not None,
                as_name is not None,
                pr_number is not None,
                stacked,
                retarget is not None,
                no_submodules,
            )
        )
        if not own_options and prompting.is_interactive():
            picked = branch_pr.choose_pr_source(pending, own_label="container branch")
            if picked is not None and publish_source(picked):
                return
        else:
            from rich.text import Text

            from jailbee.tui import hint_console

            hint_console.print(
                Text(branch_pr.pending_sources_note(pending, short)), highlight=False
            )
    scope = pr_flow.PrScope.for_repo(cfg)
    pr_flow.assert_pr_inheritance_complete(incus, full)
    state = pr_flow.ContainerLabelState(incus, full, short=short)
    stacked_state = pr_flow.ContainerLabelState(
        incus, full, short=short, prefix=pr_flow.STACKED_LABEL_PREFIX
    )

    # Fail fast on a mount-mode/stopped/no-clone container BEFORE any expensive
    # or interactive work (the gh call, the adoption prompt, AI generation, the
    # branch-name prompt). Same SyncError messages the publish path would raise.
    try:
        sync.assert_container_publishable(cfg, incus, short)
    except sync.SyncError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    try:
        record = state.read()
        stacked_record = stacked_state.read()
    except pr_flow.MalformedPrLabelError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    # A container that already has a stacked PR of its own publishes to THAT
    # PR from here on: its labels replace the reviewed PR's for the rest of the
    # run, which puts it on the ordinary update path (`user.jailbee.pr` keeps
    # naming the reviewed PR for `jailbee ls` and `jailbee git push --pr`).
    active_state = state
    stacked_base: str | None = None
    if stacked_record.number is not None or stacked_record.head:
        active_state = stacked_state
        record = stacked_record
        stacked_base = incus.config_get(full, f"{pr_flow.STACKED_LABEL_PREFIX}_base")
    record = pr_flow.recover_pr_head(scope, active_state, record)
    is_author = record.author
    stored_pr_branch = record.head
    pr_label = str(record.number) if record.number is not None else None

    # `--as` names the head of a PR still to be created. A container that
    # already has one (authored, adopted, or about to be adopted below — a PR
    # label always leads to the update path) can only push to that PR's head.
    # Checked BEFORE the adoption prompt so a usage error never adopts a PR.
    # `--stacked` is the exception: the PR it opens is a new one, so it needs a
    # head name of its own and `--as` is how it is given.
    # An undecided review container is the exception: `resolve_review_target`
    # owns the rule there, because whether `--as` is legal depends on the
    # adopt-or-stack choice it is about to settle.
    # "Undecided" mirrors `resolve_review_target`'s own None condition, `adopted`
    # included: a container adopted without a recorded head (an interrupted
    # label write) is decided, and `--as` must still be refused for it here.
    undecided_review = bool(pr_label) and not (is_author or record.adopted or stored_pr_branch)
    if as_name is not None and (is_author or stored_pr_branch or pr_label) and not undecided_review:
        pr_flow.reject_as_on_pr_update(scope, as_name, pr_label)

    def adopt_base(found: PrInfo) -> None:
        if stacked:
            # The stacked flow sets its own base (the parent's head).
            return
        pr_flow.sync_container_base_to_pr(cfg, incus, full, short=short, base_ref=found.base_ref)

    if pr_number is not None:
        # An explicitly named PR settles the head outright, so it replaces both
        # adoption paths below rather than racing them.
        record = pr_flow.bind_pr_by_number(
            scope,
            state,
            number=pr_number,
            record=record,
            yes=yes,
            record_context=f"on '{short}'",
            on_resolved=adopt_base,
        )
        is_author = record.author
        stored_pr_branch = record.head
        pr_label = str(record.number) if record.number is not None else None
    review_target = None
    if pr_number is None and active_state is state:
        # A `jailbee new --pr` container already has a PR. Publishing its commits
        # to that PR's head is allowed once the user confirms (recorded, so from
        # then on this behaves like an authored-PR container); `--stacked` opens
        # a new PR based on that head instead.
        review_target = pr_flow.resolve_review_target(
            scope, state, short, record, yes=yes, stacked=stacked, as_name=as_name
        )
        if review_target is not None and review_target.stacked:
            # The stacked PR is jailbee's own, recorded under its own labels.
            # The reviewed PR's number must not leak into `pr_label`: it would
            # turn on the foreign-head guards for a PR we are about to create.
            active_state = stacked_state
            stacked_base = review_target.parent_head
            pr_label = None
        elif review_target is not None:
            # Use the value in-process rather than re-reading the label: the run
            # must publish to the right head even if the label write failed.
            stored_pr_branch = review_target.parent_head

    # The container branch that will be fetched/published (also feeds the
    # existing-PR lookup below, the AI prompt, and the local-branch reconcile).
    container_branch = branch or incus.config_get(full, "user.jailbee.branch")

    # A container jailbee knows nothing about (`jailbee new <existing-branch>`) can
    # still sit on a branch that already has a PR — adopt it instead of opening
    # a duplicate. `--as` is a deliberate request for a different head, so it
    # skips the lookup entirely.
    if not (is_author or stored_pr_branch or pr_label) and as_name is None:
        found_pr = pr_flow.adopt_existing_pr_for_branch(
            scope,
            state,
            branch=container_branch,
            yes=yes,
            record_context=f"on '{short}'",
            on_resolved=adopt_base,
        )
        if found_pr is not None:
            found_number, found_head = found_pr
            # Both in-process, like the adoption above: `pr_label` turns on the
            # foreign-head guards and `stored_pr_branch` selects the update path.
            pr_label = str(found_number)
            stored_pr_branch = found_head

    if (
        not (is_author or stored_pr_branch or pr_label)
        and pr_number is None
        and not stacked
        and as_name is None
        and not no_outbox
        and not (review_target is not None and review_target.stacked)
    ):
        record = pr_flow.bind_outbox_reference(
            cfg, incus, full, scope, state, record, short=short, yes=yes
        )
        is_author = record.author
        stored_pr_branch = record.head
        pr_label = str(record.number) if record.number is not None else None

    # A container whose PR jailbee did not create (`jailbee new --pr`, or a PR
    # adopted by number or by branch name). Three things follow: --force needs
    # its own confirmation (it rewrites a branch the PR author may own), the
    # interactive "regenerate the description?" offer is suppressed — adoption
    # only ever promised commits — and an outbox description is narrowed to one
    # that names this PR, then confirmed once before it replaces the body.
    is_foreign_pr_head = bool(pr_label) and not is_author
    if force and pr_label and not is_author:
        pr_flow.confirm_foreign_force_push(scope, short, pr_label, stored_pr_branch, yes=yes)

    # A stacked PR's base is the reviewed PR's head, not the container's base
    # branch label — which still names the reviewed PR's own base whenever the
    # user declined the retarget offer.
    resolved_base = base or stacked_base or incus.config_get(full, "user.jailbee.base_branch")
    if not resolved_base:
        error(
            f"Container '{short}' has no recorded base branch "
            "(user.jailbee.base_branch). Pass --base <branch>."
        )
        raise typer.Exit(1)

    from jailbee import pr_ai, pr_links, pr_submodule_flow, prompting

    ai_on = pr_ai.ai_description_on(cfg, no_ai=no_ai)
    from jailbee.outbox.io import PrManagement
    from jailbee.outbox.models import OutboxError

    management = PrManagement()
    try:
        with pr_flow.outbox_publication_guard(
            cfg, incus, full, enabled=not no_outbox, management=management
        ):
            own_text = None
            if selected_own is not None:
                from jailbee import pr_outbox
                from jailbee.outbox.models import OutboxChanged

                own_text = pr_outbox.pending_pr_text(
                    cfg,
                    incus,
                    full,
                    scope=scope,
                    source_branch=container_branch,
                    uid=cfg.container_user.uid,
                    for_pr=record.number,
                    numbered_only=is_foreign_pr_head,
                    source_manifest=selected_own,
                )
                if own_text is None:
                    raise OutboxChanged(
                        f"{selected_own}: staged description unavailable for this PR"
                    )
            plan = pr_flow.resolve_pr_text_and_head(
                cfg,
                incus,
                full,
                scope,
                is_update=bool(record.author or stored_pr_branch),
                stored_head=stored_pr_branch,
                source_branch=container_branch,
                base=resolved_base,
                title=title,
                body=body,
                as_name=as_name,
                no_ai=no_ai,
                consent=consent,
                status_label=f"Generating PR title/description with Claude in '{short}'…",
                use_outbox=not no_outbox,
                **({"outbox_hint": own_text} if own_text is not None else {}),
            )
            pr_flow.bind_outbox_source(management, plan.outbox_source)
            publish_name, ai_text = plan.publish_name, plan.ai_text
            # A container-written description is text that already exists, so `--no-ai`
            # (which clears `ai_on`) must not discard it. `ai_on` itself stays as it is:
            # it still governs the update path's Claude offer below.
            text_on = ai_on or plan.outbox_source is not None

            # A stacked PR under the reviewed head's own name would not be a stacked PR
            # at all: the push would update the reviewed PR and this run would then try
            # to open a PR from that branch onto itself. Reached with `--no-ai` (the
            # name defaults to the container branch, which IS the reviewed head) or an
            # AI proposal that echoed it.
            if (
                review_target is not None
                and review_target.stacked
                and publish_name == (review_target.parent_head)
            ):
                error(
                    f"A stacked PR needs a head branch of its own: '{publish_name}' is PR "
                    f"#{review_target.parent_number}'s own head, so publishing there would "
                    f"update that PR instead. Name one with --as <branch>."
                )
                raise typer.Exit(2)

            # Share the instance-reentrant identity lock with each submodule so
            # description selection remains guarded through its receipt.
            sub_outcomes = pr_submodule_flow.publish_submodule_prs_first(
                cfg,
                incus,
                full,
                short,
                enabled=not no_submodules,
                yes=yes,
                no_ai=no_ai,
                ai=ai,
                no_outbox=no_outbox,
                ready=ready,
                management=management,
                offer_comments=lambda number, management: offer_outbox_comments(
                    cfg, incus, full, short, number=number, management=management
                ),
            )
            sub_failed = any(o.action == "failed" or o.outbox_failures > 0 for o in sub_outcomes)
            failed_subs = [o.subpath for o in sub_outcomes if o.action == "failed"]
            # A failed submodule's commits are not on its remote, so the gitlink
            # bump points nowhere, and a host with push.recurseSubmodules=check
            # refuses the push outright. Ask before anything is pushed.
            if failed_subs and not yes and prompting.is_interactive():
                if not prompting.ask_confirm(
                    f"Submodule PR failed for {', '.join(failed_subs)}. "
                    "Publish the superproject PR anyway?",
                    default=False,
                ):
                    raise typer.Exit(1)

            # --- Publish (fetch + push under the chosen name) ---
            # On a foreign PR head the generic push-failure hint's "--as" advice does
            # not apply, so `is_foreign_pr_head` (resolved above) tailors it.
            try:
                publish = sync.publish_branch_from_container(
                    cfg,
                    incus,
                    short,
                    branch=branch,
                    publish_name=publish_name,
                    force=force,
                    on_before_push=lambda result: print_publish_progress(cfg, short, result),
                    tags=cfg.pull.tags,
                )
            except sync.SyncError as exc:
                error(str(exc))
                if is_foreign_pr_head:
                    info(
                        "This container publishes to an existing PR's head branch. A "
                        "rejected push usually means the PR author pushed in the "
                        f"meantime — bring their commits in with `jailbee git push {short} "
                        "--pr --rebase`, then re-run `jailbee pr push`. It can also mean you lack "
                        "write access to the repository."
                    )
                raise typer.Exit(1) from exc

            # The fetch summary and the dirty-tree warning were printed by
            # `_print_publish_progress` before the push, not here.

            # --- Reconcile a local branch to the external name (create path) ---
            # Never on the stacked path: the container's branch is the reviewed PR's
            # head, and the host's own copy of it belongs to that PR, not to this run —
            # renaming it to the stacked PR's head would hijack the author's branch.
            is_stacked_create = review_target is not None and review_target.stacked
            if (
                not (is_author or stored_pr_branch or is_stacked_create)
                and publish.publish_name != publish.fetch.branch
            ):
                if git_mod.local_branch_exists(cfg.repo_root, publish.fetch.branch):
                    if git_mod.local_branch_exists(cfg.repo_root, publish.publish_name):
                        warn(
                            f"Local branch '{publish.publish_name}' already exists; leaving "
                            f"'{publish.fetch.branch}' as-is (no rename)."
                        )
                    else:
                        try:
                            git_mod.rename_branch(
                                cfg.repo_root, publish.fetch.branch, publish.publish_name
                            )
                            success(
                                f"Renamed local branch '{publish.fetch.branch}' → "
                                f"'{publish.publish_name}' to match the PR head."
                            )
                            if git_mod.remote_ref_exists(
                                cfg.repo_root, cfg.upstream_remote, publish.publish_name
                            ):
                                git_mod.set_upstream(
                                    cfg.repo_root,
                                    publish.publish_name,
                                    f"origin/{publish.publish_name}",
                                )
                        except git_mod.GitError as exc:
                            warn(f"Could not rename local branch: {exc}")

            is_update_path = bool(is_author or stored_pr_branch)
            resolved_title, resolved_body = ("", "")
            if not is_update_path:
                resolved_title, resolved_body = pr_flow.resolve_create_text(
                    scope,
                    ai_on=text_on,
                    ai_text=ai_text,
                    title=title,
                    body=body,
                    fallback_ref=f"refs/jailbee/{short}/{publish.fetch.branch}",
                    publish_name=publish.publish_name,
                    origin_label=f"container '{short}'",
                )
            if not is_update_path:
                pr_flow.bind_outbox_source(management, plan.outbox_source)
                pr_flow.validate_outbox_source(cfg, incus, full, plan.outbox_source)
            image_creator = pr_flow.image_description_creator(
                cfg,
                incus,
                full,
                plan.outbox_source,
                resolved_body,
                title=resolved_title,
                explicit_body=body is not None or is_update_path,
                confirm=lambda question: prompting.ask_confirm(question, default=False),
            )
            try:
                created = pr_flow.create_or_view_pr(
                    scope,
                    active_state,
                    create_description=image_creator,
                    use_outbox=not no_outbox,
                    is_update=is_update_path,
                    head=publish.publish_name,
                    base=resolved_base,
                    title=resolved_title,
                    body=resolved_body,
                    draft=ready is not True,
                    label="jailbee pr",
                    record_context=f"failed to record the PR label on '{short}'",
                )
            except pr_mod.PrError as exc:
                error(str(exc))
                raise typer.Exit(1) from exc

            is_update = is_author or created.already_existed
            update = None
            if is_update:
                update = pr_flow.apply_pr_updates(
                    cfg,
                    incus,
                    full,
                    scope,
                    number=created.number,
                    branch=publish.fetch.branch,
                    base=resolved_base,
                    title=title,
                    body=body,
                    description=description,
                    ready=ready,
                    ai_on=ai_on,
                    consent=consent,
                    generated_text=plan.ai_text,
                    foreign_head=is_foreign_pr_head,
                    url=created.url,
                    use_outbox=not no_outbox,
                    **({"strict_description": True} if selected_own is not None else {}),
                    # What the create path already resolved this run, when `gh pr
                    # create` turned out to find an existing PR. Passing it forward is
                    # what keeps the "which pending description?" question to one
                    # asking; `None` (the plain update path) makes the update path do
                    # its own, first, lookup.
                    outbox_hint=plan.outbox_source,
                    management=management,
                )
            pr_flow.render_pr_outcome(
                scope,
                url=created.url,
                number=created.number,
                is_update=is_update,
                publish_name=publish.publish_name,
                forced=publish.forced,
                ready=ready,
                update=update,
            )
            if not is_update and body is None and image_creator is None:
                # Only the create path published *this* text. When `gh pr create` found
                # a PR that already existed, this run's create-path text never landed:
                # `apply_pr_updates` above decided the description instead, did its own
                # outbox lookup (against the PR's real number) and recorded whatever it
                # consumed. Recording here as well would burn the action twice.
                pr_flow.record_outbox_consumption(cfg, incus, full, plan.outbox_source, created.url)
            if is_stacked_create and review_target is not None:
                info(
                    f"PR #{created.number} is stacked on PR #{review_target.parent_number} "
                    f"(base '{review_target.parent_head}'). Merge that one first."
                )
                pr_flow.record_stacked_base(incus, full, short, review_target.parent_head)
                pr_flow.maybe_retarget_to_parent(
                    cfg, incus, full, short, review_target, retarget=retarget
                )
            elif retarget is not None:
                warn(
                    "--retarget/--no-retarget is only acted on when a stacked PR is opened; "
                    f"ignored. To move the base later: jailbee git retarget {short} <branch>"
                )
            pr_links.link_pr_family(cfg, incus, full, short)
            # The offer to publish what else the container wrote, deliberately *not*
            # adjacent to `render_pr_outcome`: the description this run consumed is
            # recorded just above (here on the create path, inside `apply_pr_updates`
            # on the update path), and re-reading the outbox before that record exists
            # would show the spent description as still pending and publish it twice.
            outbox_failures = (
                0
                if no_outbox
                else offer_outbox_comments(
                    cfg, incus, full, short, number=created.number, management=management
                )
            )
            if web:
                pr_mod.open_pr_in_browser(cfg.repo_root, created.number)
            if outbox_failures or sub_failed:
                # The PR itself landed and its URL is already on screen; this says only
                # that the comments did not follow it or a submodule PR failed.
                raise typer.Exit(1)
    except OutboxError as exc:
        from jailbee.tui import error_plain

        error_plain(str(exc))
        raise typer.Exit(1) from exc


def run_submodule_pr_push(
    cfg: Config,
    incus: IncusType,
    full: str,
    *,
    path: str | None = None,
    title: str | None = None,
    body: str | None = None,
    base: str | None = None,
    ready: bool | None = None,
    description: bool = False,
    web: bool = False,
    ai: bool = False,
    no_ai: bool = False,
    no_outbox: bool = False,
    branch: str | None = None,
    as_name: str | None = None,
    pr_number: int | None = None,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Publish one submodule's commits to its own repository and open/update its PR."""
    from jailbee import pr_flow, prompting, submodule_pr, sync
    from jailbee.lifecycle import container_repo_dir, short_name

    pr_flow.AiConsent(ai=ai, no_ai=no_ai)
    short = short_name(cfg, full)

    try:
        sync.assert_container_publishable(cfg, incus, short)
    except sync.SyncError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    pr_flow.assert_pr_inheritance_complete(incus, full)
    super_base = incus.config_get(full, "user.jailbee.base_branch") or cfg.default_branch
    repo_dir = container_repo_dir(cfg, incus, full)
    try:
        subs = submodule_pr.detect_candidates(
            cfg, incus, full, repo_dir=repo_dir, base_branch=super_base, short=short
        )
    except submodule_pr.SubmodulePrError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if not subs and path is None:
        # "Name one with PATH" is unactionable advice when there is nothing to
        # name — distinguish "no submodules at all" from "submodules exist,
        # none are ahead". With an explicit PATH this is not that case: the
        # user named something that does not exist, which `select_target`
        # below still reports as an unknown path (exit 2).
        info(f"Container '{short}' has no submodules.")
        return

    if path is None and prompting.is_interactive():
        # The picker replaces both the silent single-candidate auto-target and
        # the ambiguity error: it offers every submodule, ahead ones first, so
        # a submodule with nothing to publish no longer has to be typed from
        # memory. Off a TTY `select_target` below keeps today's exact
        # behaviour, messages and exit codes.
        from jailbee.submodule_pr import describe_candidate
        from jailbee.tui import pick_submodule

        ordered = submodule_pr.order_candidates(subs)
        width = max((len(c.path) for c in ordered), default=0)
        try:
            path = prompting.choose_one(
                "submodule",
                [
                    prompting.Option(c.path, describe_candidate(c, width=width), c.path)
                    for c in ordered
                ],
                question="Which submodule should the PR be for?",
                always_ask=True,
                picker=lambda _opts: pick_submodule(ordered),
            )
        except prompting.Cancelled:
            raise typer.Abort() from None

    try:
        target = submodule_pr.select_target(subs, path)
    except submodule_pr.NoSubmoduleCandidatesError:
        info(
            f"No submodule in '{short}' has commits ahead of its base — nothing to "
            f"open a PR for. Name one with PATH to publish it anyway."
        )
        return
    except submodule_pr.AmbiguousSubmoduleTargetError as exc:
        error(f"Several submodules in '{short}' have commits to publish:")
        print_submodule_pr_candidates(exc.candidates)
        info("Pick one: `jailbee submodule pr push <container> <path>`.")
        raise typer.Exit(2) from exc
    except submodule_pr.SubmodulePrError as exc:
        error(str(exc))
        raise typer.Exit(
            2 if isinstance(exc, submodule_pr.UnknownSubmodulePathError) else 1
        ) from exc

    from jailbee import pr_submodule_flow

    outcome = pr_submodule_flow.publish_submodule_pr(
        cfg,
        incus,
        full,
        short,
        target,
        pr_submodule_flow.SubPrOptions(
            title=title,
            body=body,
            base=base,
            ready=ready,
            description=description,
            no_ai=no_ai,
            ai=ai,
            no_outbox=no_outbox,
            branch=branch,
            as_name=as_name,
            pr_number=pr_number,
            force=force,
            yes=yes,
            web=web,
        ),
        repo_dir=repo_dir,
        confirm_plan=confirm_submodule_pr_plan,
        offer_comments=lambda number, management: offer_outbox_comments(
            cfg, incus, full, short, number=number, management=management
        ),
    )
    from jailbee import pr_links

    pr_links.link_pr_family(cfg, incus, full, short)
    if outcome.outbox_failures:
        raise typer.Exit(1)
