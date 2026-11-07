"""Publish one whole manifest through its existing domain authorization gates."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from jailbee import issue_manifest, issue_outbox, pr, pr_outbox, prompting
from jailbee.incus import IncusError
from jailbee.orchestration.store import StoreError
from jailbee.outbox import sent
from jailbee.outbox.inspect import build_views, safe_text
from jailbee.outbox.io import PrManagement
from jailbee.outbox.markdown_view import print_lines
from jailbee.outbox.models import (
    ContainerView,
    OutboxChanged,
    OutboxError,
    OutboxExecutionError,
    ProposalId,
    StoreSnapshot,
)
from jailbee.outbox.publication_db import open_publications
from jailbee.outbox.service import _identity, instance_identity, load_container
from jailbee.outbox_io import (
    ContainerIdentity,
    JournalError,
    JournalStore,
    OutboxReadError,
)
from jailbee.tui import error_plain, info_plain, success_plain

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


@dataclass(frozen=True)
class PublishOptions:
    # Who is publishing, recorded in the Sent log. Required so no entry point
    # silently records an orchestrator publish as "user" (or the reverse).
    publisher: sent.Publisher
    dry_run: bool = False
    force: bool = False
    # Consent to a PR the container does not own (in any repository, its own
    # or not), or to an issue action on a foreign repository
    # (`github:<owner>/<name>`); the plan warns about each.
    foreign: bool = False
    draft: bool = False


def ask_publish_mode(total: int, preview: pr_outbox.DraftPreview) -> pr_outbox.PublishMode | None:
    """Offer modes using the aggregate draft count, or a single-preview fallback."""
    draft_total = (
        preview.draft_total
        if preview.draft_total is not None
        else preview.pending + int(preview.description)
    )
    options: list[prompting.Option[pr_outbox.PublishMode | None]] = [
        prompting.Option("publish", "Publish", "publish"),
        prompting.Option(
            "draft",
            "Publish as draft review — only you see it until you submit it on GitHub"
            f" ({draft_total} pending actions; {preview.held_back} general comments held back)",
            "draft",
        ),
        prompting.Option(None, "Cancel", "cancel"),
    ]
    return prompting.choose(f"Publish {total} pending actions?", options)


def _checked(
    cfg: Config,
    incus: Incus,
    container: str,
    proposal: ProposalId,
    identity: ContainerIdentity,
    journal_store: JournalStore,
    expected_revision: str | None,
    *,
    raise_errors: bool = False,
) -> tuple[ContainerView, str]:
    fresh = load_container(
        cfg, incus, container, journal_store=journal_store, raise_errors=raise_errors
    )
    if not fresh.available or fresh.identity != identity:
        raise OutboxChanged(fresh.error or "container changed; refresh required")
    view = next((v for v in fresh.proposals if v.id == proposal), None)
    if view is None or (expected_revision is not None and view.revision != expected_revision):
        raise OutboxChanged("proposal changed; refresh required")
    if view.error:
        raise OutboxError(f"{proposal}: {view.error}")
    return fresh, view.revision


@dataclass(frozen=True)
class OutcomeLine:
    severity: Literal["success", "error", "info"]
    text: str


def issue_outcome_lines(
    batch: issue_outbox.PreparedBatch,
    report: issue_outbox.ApplyReport,
) -> tuple[OutcomeLine, ...]:
    """Preserve domain receipt order, recovery advice and output severity for all callers."""
    lines = []
    attempted = set()
    for name, receipt in (*report.applied, *report.skipped):
        attempted.add((name, receipt.index))
        detail = f" ({receipt.url})" if receipt.url else ""
        lines.append(OutcomeLine("success", f"{name} action {receipt.index}: applied{detail}"))
    failure = report.failure
    if failure is None:
        lines.extend(
            OutcomeLine("success", f"{name}: fully applied and removed from the outbox")
            for name in report.cleaned
        )
    else:
        label = "uncertain" if failure.uncertain else "failed"
        if failure.manifest is None:
            text = f"apply stopped: {failure.detail}"
        elif failure.index is None:
            text = f"{failure.manifest}: {label} — {failure.detail}"
        else:
            attempted.add((failure.manifest, failure.index))
            text = f"{failure.manifest} action {failure.index}: {label} — {failure.detail}"
        lines.append(OutcomeLine("error", text))
        if failure.uncertain and failure.manifest is not None and failure.index is not None:
            lines.append(
                OutcomeLine(
                    "info",
                    f"  resolve: jailbee issue resolve {batch.container} {failure.manifest} "
                    f"{failure.index} (--applied --url <url> [--issue <n>] | --retry)",
                )
            )
        lines.extend(
            OutcomeLine("info", f"{prepared.manifest.name} action {resolved.index}: pending")
            for prepared in batch.manifests
            for resolved in prepared.actions
            if (prepared.manifest.name, resolved.index) not in attempted
        )
    return tuple(lines)


def print_issue_outcome(
    batch: issue_outbox.PreparedBatch, report: issue_outbox.ApplyReport
) -> None:
    """Render the shared pure formatter through the established stdout/stderr helpers."""
    for line in issue_outcome_lines(batch, report):
        if line.severity == "error":
            error_plain(safe_text(line.text))
        elif line.severity == "success":
            success_plain(safe_text(line.text))
        else:
            info_plain(safe_text(line.text))


def issue_plan_json(
    batch: issue_outbox.PreparedBatch, *, proposal: str, revision: str
) -> dict[str, object]:
    """The machine-readable plan of an issue batch; mirrors `pr_outbox.pr_plan_json`.

    Text fields come from the container and are emitted as-is; consumers
    sanitize them before display.
    """
    warnings = [
        f"foreign repository {slug}: not this repo or one of its submodules"
        for slug in batch.foreign_slugs
    ]
    actions: list[dict[str, object]] = []
    pending = 0
    for prepared in batch.manifests:
        for resolved in prepared.actions:
            action = resolved.action
            kind = (
                "create"
                if isinstance(action, issue_manifest.CreateAction)
                else "edit"
                if isinstance(action, issue_manifest.EditAction)
                else "comment"
                if isinstance(action, issue_manifest.CommentAction)
                else "labels"
                if isinstance(action, issue_manifest.LabelsAction)
                else "assignees"
                if isinstance(action, issue_manifest.AssigneesAction)
                else "state"
            )
            if resolved.issue.number is not None:
                target = f"{resolved.repo.slug}#{resolved.issue.number}"
            else:
                target = f"{resolved.repo.slug} (new: {resolved.issue.ref})"
            will = {"pending": "publish", "applied": "already_published"}.get(
                resolved.status, "refused"
            )
            pending += resolved.status == "pending"
            actions.append(
                {
                    "index": resolved.index,
                    "kind": kind,
                    "target": target,
                    "will": will,
                    "will_draft": None,
                }
            )
    return {
        "schema": 1,
        "proposal": proposal,
        "kind": "issue",
        "revision": revision,
        "login": batch.login,
        "foreign": bool(batch.foreign_slugs),
        "draftable": False,
        "total": pending,
        "total_draft": 0,
        "warnings": warnings,
        "refusals": [],
        "notes": [],
        "actions": actions,
    }


def _gate_refusal_plan(proposal: ProposalId, revision: str, exc: Exception) -> dict[str, object]:
    return {
        "schema": 1,
        "proposal": str(proposal),
        "kind": proposal.kind,
        "revision": revision,
        "refusals": [{"reason": str(exc), "stale": isinstance(exc, pr_outbox.StaleError)}],
        "actions": [],
        "warnings": [],
        "notes": [],
        "foreign": False,
        "draftable": False,
        "total": 0,
        "total_draft": 0,
        "login": None,
    }


def publish_selected(
    cfg: Config,
    incus: Incus,
    container: str,
    proposal: ProposalId,
    *,
    journal_store: JournalStore,
    options: PublishOptions,
    confirm: Callable[[int], bool],
    confirm_foreign: Callable[[str], bool] | None = None,
    expected_revision: str | None = None,
    raise_errors: bool = False,
    choose_mode: pr_outbox.ChooseMode | None = None,
    plan_sink: pr_outbox.PlanSink | None = None,
    policy: Callable[[issue_outbox.PreparedBatch], None]
    | Callable[[pr_outbox.Manifest], None]
    | None = None,
    result_sink: Callable[[str], None] | None = None,
    receipt_sink: Callable[[dict[str, object]], None] | None = None,
    action_limit: int | None = None,
    audit_sink: Callable[[dict[str, str]], None] | None = None,
) -> int:
    """Publish all pending actions of one manifest; the callback owns TTY policy.

    The shared UI revision is distinct from a domain proposal digest. Hold the
    same host lock from the first freshness check through domain apply/cleanup;
    the domain's own content, target and recovery gates remain mandatory.
    raise_errors preserves typed validation/execution failures for UI callers;
    the default retains legacy printed diagnostics and integer failure counts.
    plan_sink (dry runs only) receives the plan as JSON instead of any text;
    a gate refusal while planning is reported to it as a refusal-only plan.
    """
    # Known once the first freshness check passed; a refusal plan carries it.
    plan_revision = expected_revision or ""
    try:
        if plan_sink is not None and not options.dry_run:
            raise OutboxError("plan output requires a dry run")
        if proposal.kind == "issue" and options.draft:
            raise OutboxError("draft is only valid for PR publication")
        if proposal.kind == "issue" and options.force:
            raise OutboxError("force is only valid for PR publication")
        manager = PrManagement() if proposal.kind == "pr" else None
        publications = None
        lock: AbstractContextManager[object]
        if manager is not None:
            identity = _identity(incus, container)
            lock = manager.lock(identity)
        else:
            # Issue publication is single-flight per proposal through its claim
            # in the host outbox store, held from the first freshness check on.
            identity, uuid = instance_identity(incus, container)
            publications = open_publications()
            lock = publications.claim(uuid, "issue", proposal.name)
        with lock:
            fresh, revision = _checked(
                cfg,
                incus,
                container,
                proposal,
                identity,
                journal_store,
                expected_revision,
                raise_errors=raise_errors,
            )
            plan_revision = revision
            snapshot = next(s for s in fresh.stores if s.kind == proposal.kind)
            if manager is not None:
                return pr_outbox.offer_pending_comments(
                    cfg,
                    incus,
                    container,
                    container,
                    pr_number=None,
                    confirm=confirm,
                    # The store-backed fresh read inside is checked against the
                    # shared revision (`expected_revision`), not a second preview.
                    outbox=None,
                    force=options.force,
                    draft=options.draft,
                    choose_mode=choose_mode,
                    dry_run=options.dry_run,
                    manifest_names=(proposal.name,),
                    management=manager,
                    expected_revision=revision,
                    raise_errors=raise_errors,
                    plan_sink=plan_sink,
                    allow_foreign=options.foreign,
                    # The outbox alone relaxes the repository lock, under the
                    # same consent: the plan names the repository it is not.
                    allow_foreign_repo=options.foreign,
                    policy=cast("Callable[[pr_outbox.Manifest], None] | None", policy),
                    result_sink=result_sink,
                    action_limit=action_limit,
                    audit_sink=audit_sink,
                    publisher=options.publisher,
                )

            batch = issue_outbox.prepare_batch(cfg, incus, container, (proposal.name,))
            # Prepare reads independently: do not approve a newer domain proposal
            # merely because its own digest will pass apply_batch's later check.
            # Row versions come from the checked view: the content must match it.
            prepared_store = StoreSnapshot(
                "issue",
                tuple(sorted(batch.outbox.files.items())),
                snapshot.rejected,
                (),
                snapshot.versions,
            )
            prepared = next(
                (
                    v
                    for v in build_views(
                        batch.identity,
                        (prepared_store,),
                        journal_store=journal_store,
                        issue_journals=issue_outbox.store_journals(batch.identity, batch.uuid),
                    )
                    if v.id == proposal
                ),
                None,
            )
            if batch.identity != identity or prepared is None or prepared.revision != revision:
                raise OutboxChanged("proposal changed during preparation; refresh required")
            _checked(
                cfg,
                incus,
                container,
                proposal,
                identity,
                journal_store,
                revision,
                raise_errors=raise_errors,
            )
            if plan_sink is not None:
                plan_sink(issue_plan_json(batch, proposal=str(proposal), revision=revision))
                return 0
            print_lines(issue_outbox.plan_lines(batch))
            if options.dry_run:
                info_plain("Dry run: nothing was published.")
                return 0
            total = issue_outbox.pending_action_count(batch)
            # One question under the plan: when it reaches foreign repositories
            # interactively, the publish question names them and is the consent.
            combined = (
                issue_outbox.foreign_question(batch)
                if total and not options.foreign and confirm_foreign is not None
                else None
            )
            if combined is not None and confirm_foreign is not None:
                if not confirm_foreign(combined):
                    info_plain("Nothing published.")
                    return 0
            else:
                if total and not confirm(total):
                    info_plain("Nothing published.")
                    return 0
                try:
                    if not issue_outbox.foreign_consent(
                        batch, allowed=options.foreign, ask=confirm_foreign
                    ):
                        info_plain("Nothing published.")
                        return 0
                except issue_outbox.ForeignRepoRefused as exc:
                    raise OutboxError(str(exc)) from exc
            _checked(
                cfg,
                incus,
                container,
                proposal,
                identity,
                journal_store,
                revision,
                raise_errors=raise_errors,
            )
            issue_outbox.revalidate_batch(batch)
            _checked(
                cfg,
                incus,
                container,
                proposal,
                identity,
                journal_store,
                revision,
                raise_errors=raise_errors,
            )
            if policy is not None:
                cast("Callable[[issue_outbox.PreparedBatch], None]", policy)(batch)
            from jailbee.publish_progress import reporting

            with reporting(total) as tick:
                report = issue_outbox.apply_batch(
                    batch,
                    incus=incus,
                    publications=publications,
                    before_action=tick,
                    container_prefix=cfg.container_prefix,
                    publisher=options.publisher,
                )
            if audit_sink is not None:
                for name, receipt in report.applied:
                    applied_manifest = next(m for m in batch.manifests if m.manifest.name == name)
                    applied_action = next(
                        a for a in applied_manifest.actions if a.index == receipt.index
                    )
                    audit_sink(
                        {
                            "kind": type(applied_action.action)
                            .__name__.removesuffix("Action")
                            .lower(),
                            "repo": applied_action.repo.slug,
                        }
                    )
            if receipt_sink is not None:
                for name, receipt in sorted(
                    (*report.applied, *report.skipped), key=lambda pair: pair[1].index
                ):
                    applied_manifest = next(m for m in batch.manifests if m.manifest.name == name)
                    applied_action = next(
                        a for a in applied_manifest.actions if a.index == receipt.index
                    )
                    receipt_sink(
                        {
                            "index": receipt.index,
                            "kind": type(applied_action.action)
                            .__name__.removesuffix("Action")
                            .lower(),
                            "repo": applied_action.repo.slug,
                            "issue": receipt.issue,
                            "url": receipt.url,
                        }
                    )
            print_issue_outcome(batch, report)
            if result_sink is not None:
                for name, receipt in report.applied:
                    number = f" issue #{receipt.issue}" if receipt.issue is not None else ""
                    result_sink(
                        f"{name} action {receipt.index}: applied{number} "
                        f"({receipt.url or 'no URL'})"
                    )
            if raise_errors and report.failure is not None:
                if report.failure.kind == "validation":
                    raise OutboxError(report.failure.detail)
                raise OutboxExecutionError(report.failure.detail)
            return int(report.failure is not None)
    except (
        OutboxError,
        OutboxExecutionError,
        JournalError,
        OutboxReadError,
        IncusError,
        StoreError,
        issue_outbox.IssueGateError,
        pr.PrError,
        pr_outbox.GateError,
    ) as exc:
        # A failed read or transport (IssueExecutionError) is not a refusal of
        # the plan: it takes the normal error path.
        if (
            plan_sink is not None
            and isinstance(exc, (pr_outbox.GateError, issue_outbox.IssueGateError))
            and not isinstance(exc, issue_outbox.IssueExecutionError)
        ):
            plan_sink(_gate_refusal_plan(proposal, plan_revision, exc))
            return 1
        if raise_errors:
            if isinstance(exc, (OutboxError, OutboxExecutionError)):
                raise
            if isinstance(
                exc,
                (
                    issue_outbox.IssueExecutionError,
                    JournalError,
                    OutboxReadError,
                    IncusError,
                    StoreError,
                    pr.PrError,
                ),
            ):
                raise OutboxExecutionError(str(exc)) from exc
            raise OutboxError(str(exc)) from exc
        error_plain(safe_text(str(exc)))
        return 1
