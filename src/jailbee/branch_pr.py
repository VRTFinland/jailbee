"""Per-head PR bindings stored on a container."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.outbox.publication_db import Journal
    from jailbee.outbox.sent import Publisher
    from jailbee.pr import PrInfo
    from jailbee.pr_outbox import Manifest

BRANCH_PRS_LABEL = "user.jailbee.branch_prs"
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingSource:
    manifest: str
    title: str
    head: str
    base: str | None


@dataclass(frozen=True)
class StoredSource:
    """One PR source proposal as the host outbox store holds it."""

    uuid: str
    name: str
    text: str
    bodies: Mapping[str, str]
    digest: str
    manifest: Manifest


# A source publication is journaled as one action: the PR it opens or updates
# (with the description, when the manifest carries one).
SOURCE_ACTIONS = 1


def stored_sources(uuid: str) -> list[StoredSource]:
    """The instance's PR source proposals from the host outbox store, by number.

    Read-only on ``broker.db``; no Incus call. A row that no longer parses as a
    source manifest is skipped. Raises `StoreError`.
    """
    from jailbee import pr_outbox
    from jailbee.outbox import publication_db, store_db
    from jailbee.outbox_io import proposal_digest

    found = []
    for row in store_db.read_proposals(publication_db.default_root(), uuid, "pr"):
        try:
            manifest = pr_outbox.parse_manifest(row.name, row.manifest, row.bodies)
        except pr_outbox.ManifestError:
            continue
        if not pr_outbox.is_source_manifest(manifest):
            continue
        digest = proposal_digest(row.name, row.manifest, row.bodies)
        found.append(StoredSource(uuid, row.name, row.manifest, row.bodies, digest, manifest))
    return found


def stored_source(uuid: str, name: str) -> StoredSource | None:
    """One PR proposal from the host store; `ManifestError` unless it is a valid source."""
    from jailbee import pr_outbox
    from jailbee.outbox import publication_db, store_db
    from jailbee.outbox_io import proposal_digest

    row = next(
        (
            p
            for p in store_db.read_proposals(publication_db.default_root(), uuid, "pr")
            if p.name == name
        ),
        None,
    )
    if row is None:
        return None
    manifest = pr_outbox.parse_manifest(row.name, row.manifest, row.bodies)
    if not pr_outbox.is_source_manifest(manifest):
        raise pr_outbox.ManifestError(f"{name} is not a source manifest")
    digest = proposal_digest(row.name, row.manifest, row.bodies)
    return StoredSource(uuid, row.name, row.manifest, row.bodies, digest, manifest)


def source_journal(uuid: str, name: str) -> Journal | None:
    """The active publication journal of one PR source proposal, read-only."""
    from jailbee.outbox import publication_db

    journals = publication_db.read_journals(publication_db.default_root(), uuid, "pr")
    return journals.get(("pr", name))


def pending_source_manifests(cfg: Config, incus: Incus, full: str) -> list[PendingSource]:
    """Source proposals from the host store, never letting an unreadable store block a push."""
    from jailbee import pr_outbox
    from jailbee.incus import IncusError
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox.service import instance_identity
    from jailbee.outbox_io import JournalError

    try:
        _, uuid = instance_identity(incus, full)
        sources = stored_sources(uuid)
    except (IncusError, JournalError, StoreError):
        return []
    pending = []
    for item in sources:
        manifest = item.manifest
        action = next(
            (a for a in manifest.actions if isinstance(a, pr_outbox.DescriptionAction)), None
        )
        source = manifest.source or ""
        head = (
            source[:7]
            if len(source) == 40 and all(c in "0123456789abcdefABCDEF" for c in source)
            else source
        )
        pending.append(
            PendingSource(
                item.name,
                action.title if action and action.title else item.name,
                head,
                manifest.base,
            )
        )
    return pending


def choose_pr_source(pending: list[PendingSource], *, own_label: str) -> str | None:
    """Choose a proposal or the own branch; cancellation never means own branch."""
    from jailbee import prompting

    # None is reserved for cancellation by choose_one and questionary.
    options = [prompting.Option("", own_label, own_label)]
    options.extend(
        prompting.Option(
            p.manifest,
            f"{p.title} — {p.head} → {p.base or 'default base'} ({p.manifest})",
            p.manifest,
        )
        for p in pending
    )
    return prompting.choose_one("PR source", options, destructive=True) or None


def pending_sources_note(pending: list[PendingSource], short: str) -> str:
    count = len(pending)
    subject = "1 source PR proposal is" if count == 1 else f"{count} source PR proposals are"
    return (
        f"{subject} pending in the outbox; publish one with --source-manifest <file> "
        f"or `jailbee outbox {short}`."
    )


def default_head(source: str) -> str:
    if source.startswith("refs/jailbee/from/"):
        parts = source.split("/", 4)
        if len(parts) == 5:
            return parts[4]
    if source.startswith("refs/heads/"):
        return source.removeprefix("refs/heads/")
    return source.rsplit("/", 1)[-1]


def _check_own_source(
    cfg: Config,
    manifest: Manifest,
    head: str,
    base: str,
    target: PrInfo | None,
    bound: set[str],
) -> None:
    from jailbee import git, pr

    resolved_head = target.head_ref if target else head
    resolved_base = target.base_ref if target else base
    if head in {cfg.default_branch, base} or resolved_head in {cfg.default_branch, resolved_base}:
        raise pr.PrError("source head must not be the default or base branch")
    if manifest.pr is not None and str(manifest.pr) not in bound:
        raise pr.PrError(f"PR #{manifest.pr} is not bound to this container")
    if target is not None:
        if str(target.number) not in bound:
            raise pr.PrError(f"PR #{target.number} is not bound to this container")
        if base != target.base_ref:
            raise pr.PrError(f"source base {base} differs from bound PR base {target.base_ref}")
    else:
        if base != cfg.default_branch:
            raise pr.PrError("a new source PR must use the default branch as base")
        if git.remote_branch_sha_strict(cfg.repo_root, cfg.upstream_remote, head) is not None:
            raise pr.PrError(
                f"branch `{head}` already exists upstream and is not bound to this container"
            )


def _source_sent_content(
    verb: str, title: str | None, body: str | None, head: str, head_sha: str
) -> str:
    """Sent-log text of a source publication: header, title, body, head branch and SHA."""
    lines = [f"{verb} pull request", f"head: {head} @ {head_sha}"]
    if title:
        lines.append(f"title: {title}")
    if body:
        lines.extend(["", body])
    return "\n".join(lines)


def publish_source_manifest(
    cfg: Config,
    incus: Incus,
    short: str,
    manifest_name: str,
    *,
    force: bool,
    yes: bool,
    confirm: Callable[[str], bool],
    own_branches_only: bool = False,
    result_sink: Callable[[str], None] | None = None,
    audit_sink: Callable[[dict[str, str]], None] | None = None,
    publisher: Publisher,
    clock: Callable[[], float] = time.time,
) -> int:
    """Publish a pinned container source without changing the container's own PR.

    The proposal is read from the host outbox store, keyed by the instance's
    uuid; no ``incus exec`` runs (the source commits still come over the git
    bridge). Single flight is the store's publication claim. The PR create or
    update is one journal action: ``prepared`` is committed just before it, and
    its outcome, sent record and — on success — the proposal's removal in one
    transaction after it. A crash between the two leaves ``prepared``, which
    reads back as ``uncertain``: a later run asks before publishing again
    (an orchestrator run refuses). `publisher` says who published.
    """
    from jailbee import git, lifecycle, pr, pr_flow, pr_outbox, sync
    from jailbee.incus import IncusError
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db, sent
    from jailbee.outbox.models import OutboxChanged, OutboxError, ProposalId
    from jailbee.outbox.service import instance_identity
    from jailbee.outbox_io import JournalError
    from jailbee.tui import error_plain, info_plain, warn_plain

    try:
        if own_branches_only and force:
            raise pr.PrError("force publishing is host-only")
        ProposalId("pr", manifest_name)
        full = lifecycle.resolve_container_name(cfg, incus, short)
        identity, uuid = instance_identity(incus, full)
        store = publication_db.open_publications()
        with store.claim(uuid, "pr", manifest_name) as claim:
            item = stored_source(uuid, manifest_name)
            if item is None:
                info_plain(f"{manifest_name}: nothing pending")
                return 0
            manifest, digest = item.manifest, item.digest
            journal = store.load(uuid, "pr", manifest_name)
            if journal is not None and journal.digest == digest and journal.complete:
                # Published by an earlier run whose cleanup did not finish.
                settled = store.finalize(claim, digest, now=clock())
                if not settled.finalized:
                    raise OutboxChanged(f"{manifest_name}: changed after publication")
                info_plain(f"{manifest_name}: already published")
                return 0
            if journal is not None and journal.uncertain:
                question = (
                    f"{manifest_name}: an earlier publication's outcome is uncertain. "
                    "Check the PR on GitHub; publish again?"
                )
                if own_branches_only or not confirm(question):
                    raise OutboxChanged(
                        f"{manifest_name}: outcome uncertain; resolve on the host with "
                        f"`jailbee pr push {short} --source-manifest {manifest_name}`"
                    )
                store.resolve(
                    claim, journal.digest, 0, applied=None, sent_outbox="source", now=clock()
                )
            store.begin(
                claim,
                digest=digest,
                action_count=SOURCE_ACTIONS,
                container_full=full,
                now=clock(),
            )
            if manifest.source is None or manifest.source_sha is None:
                raise pr_outbox.ManifestError(f"{manifest_name} is not a source manifest")
            scope = pr_flow.PrScope.for_repo(cfg)
            repo = pr_outbox.scope_slug(scope)
            if repo is None or repo.casefold() != manifest.repo.casefold():
                raise pr_outbox.ManifestError(
                    f"manifest {manifest_name} targets {manifest.repo}, "
                    f"not {repo or 'a configured GitHub repository'}"
                )
            action = manifest.actions[0] if manifest.actions else None
            if action is not None and not isinstance(action, pr_outbox.DescriptionAction):
                raise pr_outbox.ManifestError("source manifest contains an unsupported action")
            head = default_head(manifest.source)
            if action is not None:
                head = pr_outbox._outbox_branch(action, head, manifest_name)
            base = manifest.base or cfg.default_branch
            if not git.check_ref_format(head) or not git.check_ref_format(base):
                raise pr_outbox.ManifestError("invalid destination head or base")
            bindings = BranchPrMap(incus, full)
            entries = bindings.read()
            entry = entries.get(head)
            number = manifest.pr if manifest.pr is not None else (entry.pr if entry else None)
            target = (
                pr.resolve_pr(cfg.repo_root, number, remote=cfg.upstream_remote, repo=repo)
                if number is not None
                else None
            )
            if number is None:
                target = pr.find_open_pr_for_branch_strict(cfg.repo_root, head, repo=repo)
            if target is not None and target.state != "OPEN":
                if manifest.pr is not None:
                    raise pr.PrError(f"PR #{number} is {target.state.lower()}")
                bindings.drop(head)
                entries.pop(head, None)
                target = pr.find_open_pr_for_branch_strict(cfg.repo_root, head, repo=repo)
            if own_branches_only:
                bound = {
                    str(item.pr) for item in entries.values() if item.author and not item.adopted
                }
                _check_own_source(cfg, manifest, head, base, target, bound)
            if target is not None:
                if (
                    action is not None
                    and action.branch is not None
                    and action.branch != target.head_ref
                ):
                    raise pr.PrError(
                        f"manifest branch {action.branch} conflicts with PR head {target.head_ref}"
                    )
                if target.is_cross_repository:
                    owner = target.head_repo_owner or "unknown"
                    raise pr.PrError(
                        f"head lives in {owner}/{repo.split('/', 1)[-1]}; "
                        "push to it from that repository"
                    )
                own = {str(item.pr) for item in entries.values()}
                for label in (pr_flow.PR_LABEL_PREFIX, pr_flow.STACKED_LABEL_PREFIX):
                    value = incus.config_get_checked(full, label)
                    if value:
                        if manifest.pr is None and value == str(target.number):
                            raise pr.PrError(
                                f"source manifest collides with container PR #{target.number}; "
                                "specify the PR explicitly"
                            )
                        own.add(value)
                if str(target.number) not in own and not confirm(
                    f"PR #{target.number} is not bound to container {short}: "
                    "check that this is the PR you mean to publish to"
                ):
                    return 1
                head, base = target.head_ref, target.base_ref
                if not git.check_ref_format(head) or not git.check_ref_format(base):
                    raise pr.PrError("PR has an invalid head or base")
            host_ref = sync.fetch_ref_from_container(
                cfg, incus, short, manifest.source, manifest.source_sha
            )
            base_sha = pr.fetch_base_ref(cfg.repo_root, base, remote=cfg.upstream_remote)
            if base_sha is None:
                raise pr.PrError(f"Cannot refresh base branch {base} from {cfg.upstream_remote}")
            count = git.count_commits_between(cfg.repo_root, base_sha, host_ref)
            if count is None:
                raise git.GitError(f"Cannot count commits against {base}")
            info_plain(
                f"source {manifest.source} @ {manifest.source_sha[:7]} → {head} "
                f"(base {base}, {count} commits)"
            )
            if not yes and not confirm("Publish?"):
                return 1

            def validate() -> None:
                # The same instance incarnation, and the exact proposal that was shown.
                if instance_identity(incus, full) != (identity, uuid):
                    raise OutboxChanged("container changed; refresh required")
                try:
                    fresh = stored_source(uuid, manifest_name)
                except pr_outbox.ManifestError as exc:
                    raise OutboxChanged("source proposal changed; refresh required") from exc
                if fresh is None or fresh.digest != digest:
                    raise OutboxChanged("source proposal changed; refresh required")

            validate()
            remote_sha = git.remote_branch_sha_strict(cfg.repo_root, cfg.upstream_remote, head)
            if own_branches_only and target is None and remote_sha is not None:
                raise pr.PrError(
                    f"branch `{head}` already exists upstream and is not bound to this container"
                )
            if remote_sha is None or remote_sha.lower() != manifest.source_sha:
                if (
                    target is not None
                    and sync.is_fast_forward(cfg, host_ref, head) is False
                    and not force
                ):
                    error_plain(
                        f"non-fast-forward update of {head}; run: jailbee pr push {short} "
                        f"--source-manifest {manifest_name} --force"
                    )
                    return 1
                # Pushing the pinned SHA is repeatable, so it is not journaled.
                sync.push_ref_to_remote(
                    cfg,
                    host_ref,
                    head,
                    force=force,
                    expected_sha=manifest.source_sha,
                    on_before_push=validate,
                    **({"require_absent": True} if own_branches_only and target is None else {}),
                )
            validate()
            source_sha = manifest.source_sha
            verb = "create" if target is None else "update"

            def sent_entry(
                url: str | None, number: int | None, title: str | None, body: str | None
            ) -> publication_db.SentEntry | None:
                """The sent record, or None: the outcome is recorded without it
                rather than lost after GitHub already acted."""
                try:
                    content = _source_sent_content(verb, title, body, head, source_sha)
                except Exception as exc:
                    _log.warning("could not build the sent record of %s: %s", manifest_name, exc)
                    return None
                return sent.entry(
                    sent.SentAction(
                        outbox="source",
                        manifest=manifest_name,
                        index=0,
                        kind="pr",
                        repo=repo,
                        target=number,
                        url=url,
                        title=title,
                        content=content,
                        digest=digest,
                    ),
                    instance=full,
                    uuid=uuid,
                    container_prefix=cfg.container_prefix,
                    publisher=publisher,
                )

            store.mark_prepared(claim, digest, 0, repo=repo, now=clock())
            title = action.title if action else None
            body = action.body if action else None
            cleared = False
            try:
                if target is None:
                    title, body = pr_flow.resolve_create_text(
                        scope,
                        ai_on=False,
                        ai_text=None,
                        title=title,
                        body=body,
                        fallback_ref=host_ref,
                        publish_name=head,
                        origin_label=f"container '{short}'",
                    )
                    created = pr.create_pr(
                        cfg.repo_root,
                        head=head,
                        base=base,
                        title=title,
                        body=body,
                        remote=cfg.upstream_remote,
                        repo=repo,
                    )
                    if created.already_existed:
                        store.clear_prepared(claim, digest, 0)
                        cleared = True
                        raise pr.PrError(
                            "A PR appeared during publication; rerun to inspect and confirm it"
                        )
                    number, url = created.number, created.url
                    authored = True
                else:
                    number = target.number
                    url = f"https://github.com/{repo}/pull/{number}"
                    if action is not None:
                        pr.edit_pr(
                            cfg.repo_root, number, title=action.title, body=action.body, repo=repo
                        )
                    authored = own_branches_only
            except Exception as exc:
                if not cleared:
                    # Left `prepared` (read as uncertain) when even this cannot be recorded.
                    with contextlib.suppress(JournalError, StoreError):
                        store.settle(
                            claim,
                            digest,
                            0,
                            outcome="uncertain",
                            detail=str(exc),
                            sent=sent_entry(None, target.number if target else None, title, body),
                            now=clock(),
                        )
                raise
            try:
                settled = store.settle(
                    claim,
                    digest,
                    0,
                    outcome="applied",
                    url=url,
                    issue=number,
                    sent=sent_entry(url, number, title, body),
                    now=clock(),
                )
            except (JournalError, StoreError) as exc:
                raise OutboxError(
                    f"PR #{number} was published ({url}) but its receipt could not be "
                    f"recorded ({exc}); a re-run treats it as uncertain"
                ) from exc
            if audit_sink is not None:
                audit_sink({"kind": "source", "repo": manifest.repo})
            if not settled.finalized:
                warn_plain(
                    f"{manifest_name} changed during publication; it stays in the outbox "
                    "(drop it once checked)"
                )
            bindings.put(
                BranchPr(
                    head, number, base, manifest.source, manifest.source_sha, authored, not authored
                )
            )
            if result_sink is not None:
                result_sink(f"{'opened' if target is None else 'updated'} PR #{number}: {url}")
            return 0
    except (
        IncusError,
        JournalError,
        StoreError,
        OutboxError,
        pr_outbox.ManifestError,
        pr.PrError,
        git.GitError,
        git.GitFetchError,
        sync.SyncError,
    ) as exc:
        if own_branches_only:
            raise pr.PrError(str(exc)) from exc
        error_plain(str(exc))
        return 1


def resolve_uncertain_source(
    cfg: Config,
    incus: Incus,
    full: str,
    name: str,
    index: int,
    *,
    landed_url: str | None,
) -> bool:
    """`jailbee review resolve` for any PR proposal, source manifests included.

    Settles the uncertain action through `pr_outbox.resolve_uncertain` (which
    files a source's sent record under ``source``). When a source publication
    is resolved as landed, also records the branch -> PR binding its successful
    publication would have written, from the PR the landed URL names. The
    binding is a best effort after the journal is settled: a failure warns.
    Returns whether the proposal was removed. Raises `OutboxError`.
    """
    from jailbee import pr, pr_outbox
    from jailbee.incus import IncusError
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox.service import instance_identity
    from jailbee.outbox_io import JournalError
    from jailbee.tui import warn_plain

    source: StoredSource | None = None
    if landed_url is not None:
        try:
            _, uuid = instance_identity(incus, full)
            source = stored_source(uuid, name)
        except (pr_outbox.ManifestError, IncusError, JournalError, StoreError):
            source = None
    removed = pr_outbox.resolve_uncertain(incus, full, name, index, landed_url=landed_url)
    if source is None or landed_url is None:
        return removed
    manifest = source.manifest
    number = pr_outbox.landed_pr_number(landed_url) or manifest.pr
    if number is None or manifest.source is None or manifest.source_sha is None:
        warn_plain(f"{name}: no PR number in {landed_url}; its branch binding was not recorded")
        return removed
    try:
        target = pr.resolve_pr(
            cfg.repo_root, number, remote=cfg.upstream_remote, repo=manifest.repo
        )
        # Whether the run that landed created the PR is unknown: record it as
        # adopted, which grants an orchestrator no authorship of it.
        BranchPrMap(incus, full).put(
            BranchPr(
                target.head_ref,
                number,
                target.base_ref,
                manifest.source,
                manifest.source_sha,
                False,
                True,
            )
        )
    except (pr.PrError, IncusError) as exc:
        warn_plain(f"{name}: PR #{number}'s branch binding was not recorded ({exc})")
    return removed


def unbind_head(
    incus: Incus, full: str, head: str, *, yes: bool, confirm: Callable[[str], bool]
) -> int:
    from jailbee.incus import IncusError
    from jailbee.tui import error_plain, info_plain

    try:
        bindings = BranchPrMap(incus, full)
        if bindings.get(head) is None:
            error_plain(f"No PR binding for head {head}")
            return 1
        if not yes and not confirm(f"Forget PR binding for {head}?"):
            return 1
        bindings.drop(head)
        info_plain(f"Forgot PR binding for {head}")
        return 0
    except IncusError as exc:
        error_plain(str(exc))
        return 1


def list_pr_bindings(cfg: Config, incus: Incus) -> int:
    from jailbee import lifecycle, pr, pr_flow, pr_outbox
    from jailbee.incus import IncusError
    from jailbee.tui import error_plain, info_plain

    try:
        repo = pr_outbox.scope_slug(pr_flow.PrScope.for_repo(cfg))
        if repo is None:
            raise pr.PrError("Cannot resolve the GitHub repository for PR bindings")
        for container in lifecycle.list_containers(cfg, incus, fast=True):
            full = container.name
            short = lifecycle.short_name(cfg, full)
            for label in (pr_flow.PR_LABEL_PREFIX, pr_flow.STACKED_LABEL_PREFIX):
                number = incus.config_get_checked(full, label)
                if number:
                    info_plain(
                        f"{short}  #{number}  "
                        f"{'stacked' if label == pr_flow.STACKED_LABEL_PREFIX else 'own'}"
                    )
            bindings = BranchPrMap(incus, full)
            for head, entry in bindings.read().items():
                target = pr.resolve_pr(
                    cfg.repo_root, entry.pr, remote=cfg.upstream_remote, repo=repo
                )
                if target.state != "OPEN":
                    bindings.drop(head)
                    info_plain(
                        f"{short}  #{entry.pr}  {head}: removed {target.state.lower()} binding"
                    )
                else:
                    info_plain(f"{short}  #{entry.pr}  {head} ← {entry.source}")
        return 0
    except (IncusError, pr.PrError) as exc:
        error_plain(str(exc))
        return 1


@dataclass(frozen=True)
class BranchPr:
    head: str
    pr: int
    base: str
    source: str
    source_sha: str
    author: bool = True
    adopted: bool = False


class BranchPrMap:
    def __init__(self, incus: Incus, full: str) -> None:
        self._incus = incus
        self._full = full

    def read(self) -> dict[str, BranchPr]:
        raw = self._incus.config_get_checked(self._full, BRANCH_PRS_LABEL)
        try:
            data = json.loads(raw or "{}")
        except (ValueError, TypeError, RecursionError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, BranchPr] = {}
        for head, entry in data.items():
            if not isinstance(head, str) or not head or not isinstance(entry, dict):
                continue
            if entry.get("head") != head:
                continue
            number = entry.get("pr")
            if type(number) is not int or number <= 0:
                continue
            if any(
                not isinstance(entry.get(k), str) or not entry[k]
                for k in ("base", "source", "source_sha")
            ):
                continue
            author, adopted = entry.get("author", True), entry.get("adopted", False)
            if not isinstance(author, bool) or not isinstance(adopted, bool):
                continue
            result[head] = BranchPr(
                head, number, entry["base"], entry["source"], entry["source_sha"], author, adopted
            )
        return result

    def get(self, head: str) -> BranchPr | None:
        return self.read().get(head)

    def put(self, entry: BranchPr) -> None:
        entries = self.read()
        entries[entry.head] = entry
        self._write(entries)

    def drop(self, head: str) -> bool:
        entries = self.read()
        if head not in entries:
            return False
        del entries[head]
        if entries:
            self._write(entries)
        else:
            self._incus.config_unset_checked(self._full, BRANCH_PRS_LABEL)
            from jailbee.container_bindings import refresh

            refresh(self._incus, self._full)
        return True

    def numbers(self) -> frozenset[int]:
        return frozenset(entry.pr for entry in self.read().values())

    def _write(self, entries: dict[str, BranchPr]) -> None:
        self._incus.config_set(
            self._full,
            BRANCH_PRS_LABEL,
            json.dumps({head: asdict(entry) for head, entry in entries.items()}, sort_keys=True),
        )
        from jailbee.container_bindings import refresh

        refresh(self._incus, self._full)
