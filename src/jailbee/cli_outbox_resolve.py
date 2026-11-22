"""`jailbee outbox resolve` and its `review resolve` / `issue resolve` aliases.

Settling one PR or issue action whose GitHub outcome an earlier run left
uncertain. The commands in `cli.py` and `cli_outbox.py` only parse arguments;
the container choice, the questions and the reconciliation live here. Nothing
in this module imports `jailbee.cli`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from jailbee.tui import error_plain, info, success_plain

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.issue_outbox import OutboxSnapshot
    from jailbee.lifecycle import ContainerInfo
    from jailbee.outbox.models import ProposalId, ProposalView
    from jailbee.outbox_io import ContainerIdentity, IssueJournal
    from jailbee.pr_outbox import Outbox


def _existing_container(cfg: Config, incus: Incus, name: str | None) -> str:
    """``name`` resolved to a full container name (asked for when omitted)."""
    from jailbee.lifecycle import resolve_container_for_interactive

    try:
        return resolve_container_for_interactive(cfg, incus, name)
    except ValueError as e:
        error_plain(str(e))
        raise typer.Exit(1) from e


def _review_count(counts: dict[str, int] | None, uuid: str | None) -> int | None:
    """A container's PR proposals in the host outbox store; None when unknown."""
    if counts is None or uuid is None:
        return None
    return counts.get(uuid, 0)


def _pick_pending(cfg: Config, pending: list[ContainerInfo], what: str) -> str:
    """One container out of ``pending`` (``ContainerInfo`` rows); exit 2 off a terminal."""
    from jailbee import prompting
    from jailbee.tui import container_options, pick_container

    if not prompting.is_interactive() and len(pending) > 1:
        names = ", ".join(c.display_name for c in pending)
        error_plain(
            f"several containers may have {what} actions waiting; name one explicitly "
            f"(or run in a TTY): {names}"
        )
        raise typer.Exit(2)
    try:
        return prompting.choose_one(
            "container",
            container_options(pending),
            question="Select a container:",
            picker=lambda _opts: pick_container(pending),
        )
    except prompting.Cancelled:
        raise typer.Exit(1) from None


def _review_container(cfg: Config, incus: Incus, name: str | None) -> str | None:
    """The container to review; ``None`` when nothing anywhere is pending.

    Without a name the candidates are the containers, running or stopped,
    whose PR proposal count in the host outbox store is not zero. Only an
    explicit count of 0 means "nothing here": an unknown count makes the
    container a candidate, because inferring "nothing pending" from "the
    count is unknown" is how a written review gets silently lost.
    """
    from jailbee.lifecycle import list_containers, pr_outbox_counts
    from jailbee.outbox.service import parse_retained_ref

    if name is not None and parse_retained_ref(name) is not None:
        # A destroyed instance's kept proposals (`jb outbox` lists them as full@uuid).
        return name
    if name is not None:
        return _existing_container(cfg, incus, name)
    counts = pr_outbox_counts()
    pending = [
        c for c in list_containers(cfg, incus, fast=True) if _review_count(counts, c.uuid) != 0
    ]
    if not pending:
        return None
    if len(pending) == 1:
        return pending[0].name
    return _pick_pending(cfg, pending, "PR")


def _issue_container(cfg: Config, incus: Incus, name: str | None) -> str | None:
    """The container to act on; ``None`` when no container has staged issue proposals."""
    from jailbee import issue_outbox, prompting
    from jailbee.lifecycle import list_containers
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox.service import parse_retained_ref
    from jailbee.tui import container_options, pick_container

    if name is not None and parse_retained_ref(name) is not None:
        return name
    if name is not None:
        return _existing_container(cfg, incus, name)
    pending = []
    for ci in list_containers(cfg, incus):
        if not ci.uuid:
            continue
        try:
            outbox = issue_outbox.store_outbox(ci.uuid)
        except StoreError:
            continue
        if outbox.manifest_names:
            pending.append(ci)
    if not pending:
        return None
    return prompting.choose_one(
        "container",
        container_options(pending),
        question="Select a container:",
        picker=lambda _options: pick_container(pending),
    )


def _read_pr_outbox_or_exit(cfg: Config, incus: Incus, container: str) -> Outbox:
    """Read ``container``'s PR outbox, or exit 2 saying what to do about it."""
    from jailbee import pr_outbox

    try:
        return pr_outbox.read_outbox(incus, container, uid=cfg.container_user.uid)
    except pr_outbox.OutboxReadError as e:
        error_plain(str(e))
        raise typer.Exit(2) from e


def _read_issue_store_or_exit(
    incus: Incus, container: str
) -> tuple[OutboxSnapshot, ContainerIdentity, str, dict[str, IssueJournal]]:
    """``container``'s staged issue proposals and journals from the host outbox store.

    Exits 1 when the instance's identity or the store cannot be read.
    """
    from jailbee import issue_outbox
    from jailbee.incus import IncusError
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox.service import instance_identity
    from jailbee.outbox_io import JournalError

    try:
        identity, uuid = instance_identity(incus, container)
        return (
            issue_outbox.store_outbox(uuid),
            identity,
            uuid,
            issue_outbox.store_journals(identity, uuid),
        )
    except (JournalError, IncusError, StoreError) as e:
        error_plain(str(e))
        raise typer.Exit(1) from e


def _ask_mode(*, applied: bool, retry: bool) -> tuple[bool, bool]:
    """``(applied, retry)``, asking which when neither was given."""
    from jailbee import prompting

    if applied or retry:
        return applied, retry
    mode = prompting.choose_one(
        "resolution",
        [
            prompting.Option("applied", "applied: it did land on GitHub", "--applied"),
            prompting.Option("retry", "retry: forget the outcome and attempt it again", "--retry"),
        ],
    )
    return mode == "applied", mode == "retry"


def _ask_url(url: str | None, what: str) -> str:
    """``url``, asking for it when omitted."""
    from jailbee import prompting

    if url is not None:
        return url
    return prompting.ask_text(
        what,
        validate=lambda s: None if s.startswith("https://") else "enter an https:// URL",
        alternative="--url",
    )


def resolve_pr(
    cfg: Config,
    incus: Incus,
    container: str,
    manifest: str | None,
    action: int | None,
    *,
    applied: bool,
    url: str | None,
    retry: bool,
) -> None:
    """Resolve one uncertain PR action on ``container``."""
    from jailbee import branch_pr, pr_outbox, prompting
    from jailbee.lifecycle import short_name
    from jailbee.outbox.models import OutboxError

    if applied and retry:
        error_plain("Exactly one of --applied URL or --retry is required.")
        raise typer.Exit(2)
    if retry and url is not None:
        error_plain("--retry accepts neither --url nor --issue.")
        raise typer.Exit(2)
    short = short_name(cfg, container)
    outbox = _read_pr_outbox_or_exit(cfg, incus, container)
    if manifest is None:
        uncertain = [m for m in outbox.manifest_names if pr_outbox.uncertain_actions(outbox, m)]
        manifest = prompting.choose_one(
            "manifest",
            [prompting.Option(m, m, m) for m in uncertain],
            destructive=True,
            empty_reason=f"nothing uncertain to resolve in {short}",
        )
    if action is None:
        action = prompting.choose_one(
            "action",
            [
                prompting.Option(i, f"action {i}", str(i))
                for i in pr_outbox.uncertain_actions(outbox, manifest)
            ],
            destructive=True,
            empty_reason=f"{manifest} has no uncertain action",
        )
    applied, retry = _ask_mode(applied=applied or url is not None, retry=retry)
    landed = _ask_url(url, "GitHub URL the action produced") if applied else None
    try:
        removed = branch_pr.resolve_uncertain_source(
            cfg, incus, container, manifest, action, landed_url=landed
        )
    except OutboxError as e:
        error_plain(str(e))
        raise typer.Exit(1) from e
    if landed is not None:
        success_plain(
            f"recorded {manifest} action {action} as published"
            + ("; the proposal is complete" if removed else "")
        )
    else:
        success_plain(f"{manifest} action {action} will be attempted again")


def run_review_resolve(
    cfg: Config,
    name: str | None,
    manifest: str | None,
    action: int | None,
    *,
    url: str | None,
    retry: bool,
) -> None:
    """`review resolve`: resolve the container among those with PR proposals, then the action."""
    from jailbee.incus import Incus

    incus = Incus()
    container = _review_container(cfg, incus, name)
    if container is None:
        info("Nothing pending: no container in this repo has PR actions waiting.")
        return
    resolve_pr(
        cfg, incus, container, manifest, action, applied=url is not None, url=url, retry=retry
    )


def run_issue_resolve(
    cfg: Config,
    name: str | None,
    manifest: str | None,
    action: int | None,
    *,
    applied: bool,
    url: str | None,
    issue: int | None,
    retry: bool,
    yes: bool,
) -> None:
    """`issue resolve`: resolve the container among those with issue proposals, then the action."""
    from jailbee.incus import Incus

    if applied and retry:
        error_plain("Exactly one of --applied or --retry is required.")
        raise typer.Exit(2)
    if retry and (url is not None or issue is not None):
        error_plain("--retry accepts neither --url nor --issue.")
        raise typer.Exit(2)
    incus = Incus()
    container = _issue_container(cfg, incus, name)
    if container is None:
        info("Nothing pending: no container in this repo has issue actions waiting.")
        return
    resolve_issue(
        cfg,
        incus,
        container,
        manifest,
        action,
        applied=applied,
        url=url,
        issue=issue,
        retry=retry,
        yes=yes,
    )


def resolve_issue(
    cfg: Config,
    incus: Incus,
    container: str,
    manifest: str | None,
    action: int | None,
    *,
    applied: bool,
    url: str | None,
    issue: int | None,
    retry: bool,
    yes: bool,
) -> None:
    """Resolve one uncertain issue action on ``container``."""
    from jailbee import issue_outbox, prompting
    from jailbee.issue_manifest import CreateAction, IssueManifestError, parse_manifest
    from jailbee.lifecycle import short_name
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox.markdown_view import print_lines
    from jailbee.outbox_io import JournalError, journal_has_uncertainty, proposal_digest
    from jailbee.tui import console

    if applied and retry:
        error_plain("Exactly one of --applied or --retry is required.")
        raise typer.Exit(2)
    if retry and (url is not None or issue is not None):
        error_plain("--retry accepts neither --url nor --issue.")
        raise typer.Exit(2)

    short = short_name(cfg, container)
    outbox, _identity, uuid, journals = _read_issue_store_or_exit(incus, container)

    if manifest is None:
        uncertain = [
            m
            for m in outbox.manifest_names
            if (j := journals.get(m)) is not None and journal_has_uncertainty(j)
        ]
        manifest = prompting.choose_one(
            "manifest",
            [prompting.Option(m, m, m) for m in uncertain],
            destructive=True,
            empty_reason=f"nothing uncertain to resolve in {short}",
        )
    text = outbox.files.get(manifest)
    if text is None:
        error_plain(f"{short} has no pending manifest named {manifest}")
        if outbox.manifest_names:
            error_plain(f"Pending there: {', '.join(outbox.manifest_names)}")
        raise typer.Exit(2)
    try:
        parsed = parse_manifest(manifest, text, outbox.files)
    except IssueManifestError as e:
        error_plain(str(e))
        raise typer.Exit(1) from e
    journal = journals.get(manifest)
    if action is None:
        open_actions = sorted(
            a.index for a in (journal.actions if journal else ()) if a.state == "uncertain"
        )
        action = prompting.choose_one(
            "action",
            [prompting.Option(i, f"action {i}", str(i)) for i in open_actions],
            destructive=True,
            empty_reason=f"{manifest} has no uncertain action",
        )
    if not 0 <= action < len(parsed.actions):
        error_plain(f"{manifest} has no action {action}")
        raise typer.Exit(2)
    resolved_action = parsed.actions[action]
    applied, retry = _ask_mode(applied=applied, retry=retry)
    if applied and not isinstance(resolved_action, CreateAction) and issue is not None:
        error_plain("--issue is only valid for a create action.")
        raise typer.Exit(2)

    if applied:
        url = _ask_url(url, "GitHub URL the mutation produced")

    if applied and isinstance(resolved_action, CreateAction) and issue is None:

        def _number_problem(s: str) -> str | None:
            s = s.strip()
            return None if s.isascii() and s.isdecimal() and int(s) > 0 else "enter a number"

        issue = int(
            prompting.ask_text(
                "created issue number", validate=_number_problem, alternative="--issue"
            )
        )

    body_files = {body: outbox.files[body] for body in parsed.body_files if body in outbox.files}
    digest = proposal_digest(manifest, text, body_files)

    console.print()
    print_lines(issue_outbox.show_lines(parsed, journal, short))

    resolution: issue_outbox.Resolution
    if retry:
        resolution = issue_outbox.RetryResolution()
    else:
        assert url is not None, "validated above: --applied requires --url"
        resolution = issue_outbox.AppliedResolution(url=url, issue=issue)

    if not yes:
        if not prompting.is_interactive():
            error_plain(
                "Refusing to resolve without a confirmation. Re-run with -y, or from a terminal."
            )
            raise typer.Exit(2)
        verb = "Retry" if retry else "Mark applied"
        if not prompting.ask_confirm(f"{verb} {manifest} action {action}?", default=False):
            info("Nothing changed.")
            return

    try:
        targets = issue_outbox.resolve_repo_targets(cfg)
    except ValueError as e:
        error_plain(str(e))
        raise typer.Exit(1) from e
    repo = issue_outbox.target_for(targets, resolved_action.repo, cfg.repo_root)
    if repo is None:
        error_plain(f"{manifest} action {action}: forbidden repo path {resolved_action.repo!r}")
        raise typer.Exit(1)

    try:
        removed = issue_outbox.reconcile_action(
            uuid=uuid,
            index=action,
            resolution=resolution,
            repo=repo,
            manifest=parsed,
            digest=digest,
        )
    except (JournalError, StoreError) as e:
        error_plain(str(e))
        raise typer.Exit(1) from e

    success_plain(f"{manifest} action {action}: {'retried' if retry else 'marked applied'}")
    if removed:
        success_plain(f"{manifest}: fully applied and removed from the outbox")


def run_outbox_resolve(
    cfg: Config,
    incus: Incus,
    container: str | None,
    proposal: str | None,
    action: int | None,
    *,
    applied: bool,
    url: str | None,
    issue: int | None,
    retry: bool,
    yes: bool,
) -> int:
    """`outbox resolve`: the container first, then one picker over its uncertain proposals.

    A container and a proposal both named need no discovery. Otherwise the
    container's proposals are inspected and the uncertain ones offered, `pr/`
    and `issue/` together. With nothing uncertain anywhere this is success.
    """
    from jailbee import prompting
    from jailbee.outbox.commands import discover
    from jailbee.outbox.models import OutboxError, ProposalId
    from jailbee.outbox.service import parse_retained_ref

    pid: ProposalId | None = None
    if proposal is not None:
        try:
            pid = ProposalId.parse(proposal)
        except OutboxError as exc:
            error_plain(str(exc))
            raise typer.Exit(2) from exc

    if container is not None and pid is not None:
        full = (
            container
            if parse_retained_ref(container) is not None
            else _existing_container(cfg, incus, container)
        )
    else:
        views = [v for v in discover(cfg, incus, container, all_repos=False) if v.available]
        uncertain: dict[str, tuple[ProposalView, ...]] = {
            v.name: tuple(p for p in v.proposals if p.state == "uncertain") for v in views
        }
        candidates = [name for name, rows in uncertain.items() if rows]
        if container is not None:
            full = views[0].name if views else container
            if not candidates and pid is None:
                info(f"Nothing pending: {container} has no uncertain actions.")
                return 0
        else:
            if not candidates:
                info("Nothing pending: no container in this repo has uncertain actions.")
                return 0
            full = prompting.choose_one(
                "container",
                [prompting.Option(n, n, n) for n in candidates],
                destructive=True,
            )
        if pid is None:
            pid = prompting.choose_one(
                "proposal",
                [
                    prompting.Option(p.id, f"{p.id}  {p.state}", str(p.id))
                    for p in uncertain.get(full, ())
                ],
                destructive=True,
                empty_reason=f"nothing uncertain to resolve in {full}",
            )

    if pid.kind == "pr":
        if issue is not None:
            error_plain("--issue is only valid for an issue create action.")
            raise typer.Exit(2)
        resolve_pr(cfg, incus, full, pid.name, action, applied=applied, url=url, retry=retry)
    else:
        resolve_issue(
            cfg,
            incus,
            full,
            pid.name,
            action,
            applied=applied,
            url=url,
            issue=issue,
            retry=retry,
            yes=yes,
        )
    return 0
