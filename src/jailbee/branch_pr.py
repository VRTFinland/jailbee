"""Per-head PR bindings stored on a container."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.outbox.sent import Publisher
    from jailbee.pr import PrInfo
    from jailbee.pr_outbox import Manifest

BRANCH_PRS_LABEL = "user.jailbee.branch_prs"


@dataclass(frozen=True)
class PendingSource:
    manifest: str
    title: str
    head: str
    base: str | None


def pending_source_manifests(cfg: Config, incus: Incus, full: str) -> list[PendingSource]:
    """Read source proposals without letting an unavailable outbox block a push."""
    from jailbee import pr_outbox
    from jailbee.outbox_io import OutboxReadError

    try:
        snapshot = pr_outbox.read_outbox(incus, full, uid=cfg.container_user.uid, strict=True)
    except OutboxReadError:
        return []
    if snapshot.rejected or snapshot.identity is None:
        return []
    pending = []
    for name in snapshot.manifest_names:
        try:
            manifest = pr_outbox.parse_manifest(name, snapshot.files[name], snapshot.files)
            if not pr_outbox.is_source_manifest(manifest):
                continue
            if pr_outbox.source_publication_receipt(snapshot, name) is not None:
                continue
        except pr_outbox.ManifestError:
            continue
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
                name, action.title if action and action.title else name, head, manifest.base
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
    publisher: Publisher,
) -> int:
    """Publish a pinned container source without changing the container's own PR.

    The accepted PR create/update is recorded in the outbox Sent log (best
    effort) before the container-side bookkeeping; `publisher` says by whom.
    """
    from jailbee import git, lifecycle, pr, pr_flow, pr_outbox, sync
    from jailbee.incus import IncusError
    from jailbee.outbox.io import PrManagement
    from jailbee.outbox.models import OutboxChanged, OutboxError, ProposalId
    from jailbee.outbox_io import JournalError, OutboxReadError
    from jailbee.tui import error_plain, info_plain

    try:
        if own_branches_only and force:
            raise pr.PrError("force publishing is host-only")
        ProposalId("pr", manifest_name)
        full = lifecycle.resolve_container_name(cfg, incus, short)
        manager = PrManagement()
        with pr_flow.outbox_publication_guard(cfg, incus, full, enabled=True, management=manager):
            snapshot = pr_outbox.read_outbox(incus, full, uid=cfg.container_user.uid, strict=True)
            if snapshot.rejected or snapshot.identity is None:
                raise OutboxChanged("outbox snapshot incomplete; refresh required")
            if manager.identity is not None and snapshot.identity != manager.identity:
                raise OutboxChanged("container changed; refresh required")
            receipt = pr_outbox.source_publication_receipt(snapshot, manifest_name)
            if receipt is not None:
                pr_outbox.consume_manifest(
                    incus,
                    full,
                    manifest_name,
                    receipt["url"],
                    uid=cfg.container_user.uid,
                    preview=snapshot,
                    management=manager,
                )
                return 0
            if manifest_name not in snapshot.files:
                info_plain(f"{manifest_name}: nothing pending")
                return 0
            manifest = pr_outbox.parse_manifest(
                manifest_name, snapshot.files[manifest_name], snapshot.files
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
                bound = {str(item.pr) for item in entries.values()}
                for label in (pr_flow.PR_LABEL_PREFIX, pr_flow.STACKED_LABEL_PREFIX):
                    value = incus.config_get_checked(full, label)
                    if value:
                        bound.add(value)
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
            pr_outbox.preflight_source_progress(snapshot, manifest_name)
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
                fresh = pr_outbox.read_outbox(incus, full, uid=cfg.container_user.uid, strict=True)
                if (
                    fresh.identity != snapshot.identity
                    or fresh.files != snapshot.files
                    or fresh.rejected
                ):
                    raise OutboxChanged("source snapshot changed; refresh required")

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
            if target is None:
                title, body = pr_flow.resolve_create_text(
                    scope,
                    ai_on=False,
                    ai_text=None,
                    title=action.title if action else None,
                    body=action.body if action else None,
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
                authored = False
            from jailbee.outbox import sent

            sent.record(
                instance=full,
                container_prefix=cfg.container_prefix,
                container_created=sent.parse_created(snapshot.identity.created_at),
                publisher=publisher,
                action=sent.SentAction(
                    outbox="source",
                    manifest=manifest_name,
                    index=0,
                    kind="pr",
                    repo=repo,
                    target=number,
                    url=url,
                    title=title if target is None else (action.title if action else None),
                    content=_source_sent_content(
                        "create" if target is None else "update",
                        title if target is None else (action.title if action else None),
                        body if target is None else (action.body if action else None),
                        head,
                        manifest.source_sha,
                    ),
                    digest=pr_outbox.proposal_files_digest(snapshot.files, manifest_name),
                ),
                state="applied",
            )
            validate()
            bindings.put(
                BranchPr(
                    head, number, base, manifest.source, manifest.source_sha, authored, not authored
                )
            )
            pr_outbox.consume_manifest(
                incus,
                full,
                manifest_name,
                url,
                uid=cfg.container_user.uid,
                preview=snapshot,
                management=manager,
            )
            if result_sink is not None:
                result_sink(f"{'opened' if target is None else 'updated'} PR #{number}: {url}")
            return 0
    except (
        IncusError,
        JournalError,
        OutboxReadError,
        OutboxError,
        pr_outbox.ManifestError,
        pr_outbox.FinalizeError,
        pr.PrError,
        git.GitError,
        git.GitFetchError,
        sync.SyncError,
    ) as exc:
        if own_branches_only:
            raise pr.PrError(str(exc)) from exc
        error_plain(str(exc))
        return 1


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
        return True

    def numbers(self) -> frozenset[int]:
        return frozenset(entry.pr for entry in self.read().values())

    def _write(self, entries: dict[str, BranchPr]) -> None:
        self._incus.config_set(
            self._full,
            BRANCH_PRS_LABEL,
            json.dumps({head: asdict(entry) for head, entry in entries.items()}, sort_keys=True),
        )
