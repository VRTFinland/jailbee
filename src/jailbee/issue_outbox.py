"""Host-side orchestration for GitHub issue outbox actions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, assert_never

from jailbee import git, issue_github, submodules
from jailbee.config import CONTAINER_USERNAME
from jailbee.github_repo import github_slug, resolve_submodule_url
from jailbee.incus import Incus, IncusError
from jailbee.issue_github import (
    IssueGithubMutationError,
    IssueGithubReadError,
    IssueSnapshot,
    MutationReceipt,
)
from jailbee.issue_manifest import (
    MAX_MANIFEST_BYTES,
    MAX_MANIFESTS,
    MAX_OFFER_ACTIONS,
    CommentAction,
    CreateAction,
    CreatedIssue,
    EditAction,
    ExistingIssue,
    IssueAction,
    IssueManifest,
    IssueManifestError,
    LabelsAction,
    StateAction,
    parse_manifest,
)
from jailbee.outbox_io import (
    ContainerIdentity,
    IssueJournal,
    JournalAction,
    JournalError,
    JournalKey,
    JournalStore,
    OutboxReadError,
    append_applied_log,
    container_identity,
    delete_outbox_files,
    journal_has_uncertainty,
    journal_key,
    proposal_digest,
    read_text_outbox,
    safe_mutation_detail,
)

if TYPE_CHECKING:
    from jailbee.config import Config


@dataclass(frozen=True)
class RepoTarget:
    """A host-authorized repository an issue manifest may name."""

    path: str
    repo_root: Path
    slug: str

    @property
    def identity(self) -> str:
        """Case-insensitive GitHub identity, separate from display spelling."""
        return self.slug.casefold()


def resolve_repo_targets(cfg: Config) -> Mapping[str, RepoTarget]:
    """Return ``.`` plus safe host-declared submodule targets keyed by path."""
    root_url = git.get_remote_url(cfg.repo_root, cfg.upstream_remote)
    root_slug = github_slug(root_url or "")
    if root_url is None or root_slug is None:
        raise ValueError("superproject upstream remote must resolve to a GitHub repository")

    targets: dict[str, RepoTarget] = {
        ".": RepoTarget(path=".", repo_root=cfg.repo_root, slug=root_slug)
    }
    remote_urls = {".": root_url}
    for declared in submodules.declared_submodule_remotes(cfg.repo_root):
        repo_root = cfg.repo_root / declared.path
        if submodules.host_subrepo_exists(cfg.repo_root, declared.path):
            remote = git.detect_upstream_remote(repo_root)
            if remote is None:
                raise ValueError(f"submodule '{declared.path}' has no detectable upstream remote")
            remote_url = git.get_remote_url(repo_root, remote)
            if remote_url is None:
                raise ValueError(
                    f"submodule '{declared.path}' has no URL for upstream remote '{remote}'"
                )
        else:
            parent_url = remote_urls.get(declared.parent_path)
            if parent_url is None:
                raise ValueError(
                    f"submodule '{declared.path}' has unresolved declaring parent "
                    f"'{declared.parent_path}'"
                )
            remote_url = resolve_submodule_url(parent_url, declared.url)

        slug = github_slug(remote_url or "")
        if remote_url is None or slug is None:
            raise ValueError(
                f"submodule '{declared.path}' remote must resolve to a GitHub repository"
            )
        targets[declared.path] = RepoTarget(declared.path, repo_root, slug)
        remote_urls[declared.path] = remote_url

    return targets


class IssueGateError(Exception):
    """One or more read-only checks refuse a proposed batch."""


class IssueStaleError(IssueGateError):
    """An expected field changed after the approval plan was prepared."""


@dataclass(frozen=True)
class OutboxSnapshot:
    files: Mapping[str, str]

    @property
    def manifest_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                name
                for name in self.files
                if name.endswith(".json") and not name.endswith(".progress.json")
            )
        )


@dataclass(frozen=True)
class ResolvedIssue:
    """An existing number or a manifest-local create dependency."""

    number: int | None
    ref: str | None = None


@dataclass(frozen=True)
class ResolvedAction:
    index: int
    action: IssueAction
    repo: RepoTarget
    issue: ResolvedIssue
    status: Literal["pending", "applied", "uncertain"]
    labels: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PreparedManifest:
    manifest: IssueManifest
    digest: str
    journal: IssueJournal | None
    actions: tuple[ResolvedAction, ...]


@dataclass(frozen=True)
class PreparedBatch:
    container: str
    host_repo_root: Path
    identity: ContainerIdentity
    login: str
    outbox: OutboxSnapshot
    manifests: tuple[PreparedManifest, ...]
    initial_issues: Mapping[tuple[str, int], IssueSnapshot]


def _outbox_directory() -> str:
    """Absolute issue outbox path inside a container."""
    return f"/home/{CONTAINER_USERNAME}/.jailbee/issue-outbox"


def read_issue_outbox(incus: Incus, container: str, *, uid: int | None) -> OutboxSnapshot:
    """Read proposal text without accepting container-owned progress."""
    files = read_text_outbox(
        incus,
        container,
        _outbox_directory(),
        uid=uid,
        max_file_bytes=MAX_MANIFEST_BYTES,
    )
    return OutboxSnapshot(MappingProxyType(files))


def _status(receipt: JournalAction | None) -> Literal["pending", "applied", "uncertain"]:
    if receipt is None:
        return "pending"
    return "applied" if receipt.state == "applied" else "uncertain"


def _expected_fields(action: IssueAction) -> dict[str, object]:
    fields: dict[str, object] = {}
    if isinstance(action, EditAction):
        if action.has_expected_title:
            fields["title"] = action.expected_title
        if action.has_expected_body:
            fields["body"] = action.expected_body or ""
    elif isinstance(action, LabelsAction):
        fields["labels"] = frozenset(label.casefold() for label in action.expected_labels)
    elif isinstance(action, StateAction):
        fields["state"] = action.expected_state
    return fields


def _differences(action: IssueAction, snapshot: IssueSnapshot, context: str) -> list[str]:
    differences = []
    for field, expected in _expected_fields(action).items():
        actual = (
            frozenset(label.casefold() for label in snapshot.labels)
            if field == "labels"
            else getattr(snapshot, field)
        )
        if actual != expected:
            differences.append(f"{context}: expected.{field} differs from the current issue")
    return differences


def _load_proposals(
    outbox: OutboxSnapshot,
    names: Sequence[str],
    targets: Mapping[str, RepoTarget],
    identity: ContainerIdentity,
    store: JournalStore,
    refusals: list[str],
) -> list[tuple[IssueManifest, str, IssueJournal | None]]:
    proposals = []
    if len(outbox.manifest_names) > MAX_MANIFESTS or len(names) > MAX_MANIFESTS:
        refusals.append(f"issue outbox contains more than {MAX_MANIFESTS} manifests")
    if len(set(names)) != len(names):
        refusals.append("selected manifest names must be unique")
    count = 0
    for name in names:
        if name not in outbox.manifest_names:
            refusals.append(f"{name}: manifest file is missing from the outbox")
            continue
        try:
            manifest = parse_manifest(name, outbox.files[name], outbox.files)
        except IssueManifestError as exc:
            refusals.append(str(exc))
            continue
        count += len(manifest.actions)
        for index, action in enumerate(manifest.actions):
            if action.repo not in targets:
                known = ", ".join(repr(path) for path in sorted(targets)) or "[none]"
                refusals.append(
                    f"{name} action {index}: forbidden repo path {action.repo!r}; "
                    f"known paths: {known}"
                )
        digest = proposal_digest(
            name, outbox.files[name], {body: outbox.files[body] for body in manifest.body_files}
        )
        try:
            journal = store.load(journal_key(identity, name))
            if journal is not None:
                if journal.digest != digest:
                    if journal.actions:
                        raise JournalError("proposal changed after recorded progress")
                    journal = None
                elif journal.action_count != len(manifest.actions):
                    raise JournalError("journal action count differs from proposal")
                if journal is not None:
                    for receipt in journal.actions:
                        action = manifest.actions[receipt.index]
                        target = targets.get(action.repo)
                        if target is not None and target.identity != receipt.repo.casefold():
                            raise JournalError("journal repo differs from the host-resolved repo")
                        if (
                            isinstance(action, CreateAction)
                            and receipt.state == "applied"
                            and receipt.issue is None
                        ):
                            raise JournalError("applied create journal receipt has no issue number")
        except JournalError as exc:
            refusals.append(f"{name}: {exc}")
            continue
        proposals.append((manifest, digest, journal))
    if count > MAX_OFFER_ACTIONS:
        refusals.append(f"selected manifests contain more than {MAX_OFFER_ACTIONS} actions")
    return proposals


def _resolve_labels(
    action: CreateAction | LabelsAction,
    canonical: Mapping[str, str] | None,
    context: str,
    refusals: list[str],
) -> tuple[str, ...]:
    additions = action.labels if isinstance(action, CreateAction) else action.add
    for label in additions:
        if canonical is not None and label.casefold() not in canonical:
            refusals.append(f"{context}: unknown label {label!r}")
    canonical = canonical or {}
    added = tuple(canonical.get(label.casefold(), label) for label in additions)
    if isinstance(action, CreateAction):
        return added
    expected = {label.casefold() for label in action.expected_labels}
    removed = {label.casefold() for label in action.remove}
    for label in action.remove:
        if label.casefold() not in expected:
            refusals.append(f"{context}: removal {label!r} is absent from expected labels")
    for label in action.add:
        if label.casefold() in expected:
            refusals.append(f"{context}: addition {label!r} is already present in expected labels")
    return (
        tuple(
            canonical.get(label.casefold(), label)
            for label in action.expected_labels
            if label.casefold() not in removed
        )
        + added
    )


def prepare_batch(
    cfg: Config,
    incus: Incus,
    container: str,
    selected_names: Sequence[str],
    *,
    uid: int | None,
    journal_store: JournalStore,
) -> PreparedBatch:
    """Resolve and validate an offer using only host-authorized, read-only inputs."""
    outbox = read_issue_outbox(incus, container, uid=uid)
    refusals: list[str] = []
    try:
        identity = container_identity(incus, container)
    except (JournalError, IncusError) as exc:
        raise IssueGateError(str(exc)) from exc
    try:
        targets = resolve_repo_targets(cfg)
    except ValueError as exc:
        refusals.append(str(exc))
        targets = {}
    proposals = _load_proposals(outbox, selected_names, targets, identity, journal_store, refusals)
    if refusals:
        raise IssueGateError("\n".join(refusals))

    try:
        login = issue_github.current_login(cfg.repo_root)
    except IssueGithubReadError as exc:
        refusals.append(str(exc))
        login = ""
    issues: dict[tuple[str, int], IssueSnapshot] = {}
    issue_errors: dict[tuple[str, int], str] = {}
    label_maps: dict[str, Mapping[str, str]] = {}
    label_errors: dict[str, str] = {}
    mutations: dict[tuple[str, int | str, str], str] = {}
    prepared = []
    for manifest, digest, journal in proposals:
        receipts = {receipt.index: receipt for receipt in journal.actions} if journal else {}
        refs: dict[str, ResolvedIssue] = {}
        created: dict[str, IssueSnapshot] = {}
        actions = []
        for index, action in enumerate(manifest.actions):
            context = f"{manifest.name} action {index}"
            repo = targets[action.repo]
            receipt = receipts.get(index)
            status = _status(receipt)
            if isinstance(action, CreateAction):
                number = receipt.issue if receipt and status == "applied" else None
                issue = ResolvedIssue(number, action.ref)
                refs[action.ref] = issue
                if status == "pending":
                    created[action.ref] = IssueSnapshot(
                        0, action.title, action.body, action.labels, "open", "", False
                    )
            elif isinstance(action.target, ExistingIssue):
                issue = ResolvedIssue(action.target.number)
            else:
                issue = refs[action.target.ref]

            snapshot = None
            if not isinstance(action, CreateAction) and status == "pending":
                if issue.number is not None:
                    key = (repo.identity, issue.number)
                    if key not in issues and key not in issue_errors:
                        try:
                            issues[key] = issue_github.get_issue(
                                cfg.repo_root, repo.slug, issue.number
                            )
                        except IssueGithubReadError as exc:
                            issue_errors[key] = str(exc)
                    if key in issue_errors:
                        refusals.append(f"{context}: {issue_errors[key]}")
                    snapshot = issues.get(key)
                elif issue.ref is not None:
                    snapshot = created.get(issue.ref)
                if snapshot is not None:
                    if snapshot.is_pull_request:
                        refusals.append(f"{context}: target is a pull request, not an issue")
                    refusals.extend(_differences(action, snapshot, context))

            labels = None
            if isinstance(action, (CreateAction, LabelsAction)) and status == "pending":
                if repo.identity not in label_maps and repo.identity not in label_errors:
                    try:
                        label_maps[repo.identity] = issue_github.list_labels(
                            cfg.repo_root, repo.slug
                        )
                    except IssueGithubReadError as exc:
                        label_errors[repo.identity] = str(exc)
                if repo.identity in label_errors:
                    refusals.append(f"{context}: {label_errors[repo.identity]}")
                labels = _resolve_labels(action, label_maps.get(repo.identity), context, refusals)

            if status == "pending":
                for field in _expected_fields(action):
                    mutation_key = (
                        (repo.identity, issue.number, field)
                        if issue.number is not None
                        else (manifest.name, issue.ref or "", field)
                    )
                    if mutation_key in mutations:
                        refusals.append(
                            f"{context}: conflict on {field} with {mutations[mutation_key]}"
                        )
                    mutations[mutation_key] = context
            actions.append(ResolvedAction(index, action, repo, issue, status, labels))
        prepared.append(PreparedManifest(manifest, digest, journal, tuple(actions)))
    if refusals:
        raise IssueGateError("\n".join(refusals))
    return PreparedBatch(
        container, cfg.repo_root, identity, login, outbox, tuple(prepared), MappingProxyType(issues)
    )


def revalidate_batch(batch: PreparedBatch) -> None:
    """Refetch only pending existing mutations and compare their declared fields."""
    fresh: dict[tuple[str, int], IssueSnapshot] = {}
    errors: dict[tuple[str, int], str] = {}
    refusals = []
    for manifest in batch.manifests:
        for resolved in manifest.actions:
            if (
                resolved.status != "pending"
                or resolved.issue.number is None
                or not _expected_fields(resolved.action)
            ):
                continue
            key = (resolved.repo.identity, resolved.issue.number)
            context = f"{manifest.manifest.name} action {resolved.index}"
            if key not in fresh and key not in errors:
                try:
                    fresh[key] = issue_github.get_issue(
                        batch.host_repo_root, resolved.repo.slug, resolved.issue.number
                    )
                except IssueGithubReadError as exc:
                    errors[key] = str(exc)
            if key in errors:
                refusals.append(f"{context}: {errors[key]}")
                continue
            snapshot = fresh[key]
            if snapshot.is_pull_request:
                refusals.append(f"{context}: target is a pull request, not an issue")
            refusals.extend(_differences(resolved.action, snapshot, context))
    if refusals:
        raise IssueStaleError("\n".join(refusals))


def _prose(label: str, text: str | None) -> list[str]:
    return [f"  {label}:", *(f"    {line}" for line in (text or "[empty]").split("\n"))]


def _action_lines(
    name: str,
    index: int,
    action: IssueAction,
    issue: ResolvedIssue,
    receipt: JournalAction | None,
    labels: tuple[str, ...] | None,
) -> list[str]:
    kind = (
        "create"
        if isinstance(action, CreateAction)
        else "edit"
        if isinstance(action, EditAction)
        else "comment"
        if isinstance(action, CommentAction)
        else "labels"
        if isinstance(action, LabelsAction)
        else "state"
    )
    target = f"#{issue.number}" if issue.number is not None else f"ref {issue.ref}"
    status = _status(receipt)
    display = "applied; skip" if status == "applied" else status
    lines = [f"{name} action {index}: {kind} {target} [{display}]"]
    if receipt and receipt.url:
        lines.append(f"  receipt: {receipt.url}")
    if receipt and receipt.detail:
        lines.append(f"  detail: {receipt.detail}")
    if not isinstance(action, CreateAction) and issue.ref is not None:
        lines.append(f"  depends on create ref {issue.ref}")
    if isinstance(action, CreateAction):
        if issue.number is not None:
            lines.append(f"  create ref: {action.ref}")
        lines.extend(_prose("title", action.title))
        lines.extend(_prose("body", action.body))
        lines.append(
            f"  labels: {', '.join(action.labels if labels is None else labels) or '[empty]'}"
        )
    elif isinstance(action, EditAction):
        if action.has_expected_title:
            lines.extend(_prose("title before", action.expected_title))
            lines.extend(_prose("title after", action.title))
        if action.has_expected_body:
            lines.extend(_prose("body before", action.expected_body))
            lines.extend(_prose("body after", action.body))
    elif isinstance(action, CommentAction):
        lines.extend(_prose("body", action.body))
    elif isinstance(action, LabelsAction):
        added = (
            action.add
            if labels is None
            else tuple(
                label for label in labels if label.casefold() in {x.casefold() for x in action.add}
            )
        )
        lines.append(f"  remove: {', '.join(action.remove) or '[empty]'}")
        lines.append(f"  add: {', '.join(added) or '[empty]'}")
        if labels is not None:
            lines.append(f"  labels after: {', '.join(labels) or '[empty]'}")
    else:
        lines.append(f"  state: {action.expected_state} -> {action.state}")
        if action.reason is not None:
            lines.append(f"  reason: {action.reason}")
    return lines


def plan_lines(batch: PreparedBatch) -> list[str]:
    """Return the complete approval text in execution order, without markup."""
    lines = [f"Host GitHub login: {batch.login}", f"Container: {batch.container}"]
    for prepared in batch.manifests:
        lines.append(f"Manifest: {prepared.manifest.name}")
        receipts = {r.index: r for r in prepared.journal.actions} if prepared.journal else {}
        previous = None
        for resolved in prepared.actions:
            if resolved.repo != previous:
                lines.append(f"Repository: {resolved.repo.path} ({resolved.repo.slug})")
                previous = resolved.repo
            lines.extend(
                _action_lines(
                    prepared.manifest.name,
                    resolved.index,
                    resolved.action,
                    resolved.issue,
                    receipts.get(resolved.index),
                    resolved.labels,
                )
            )
    return lines


def show_lines(manifest: IssueManifest, journal: IssueJournal | None) -> list[str]:
    """Show proposal text and host progress without needing GitHub reads."""
    lines = [f"Manifest: {manifest.name}"]
    receipts = {r.index: r for r in journal.actions} if journal else {}
    refs: dict[str, ResolvedIssue] = {}
    previous = None
    for index, action in enumerate(manifest.actions):
        receipt = receipts.get(index)
        if action.repo != previous:
            lines.append(f"Repository: {action.repo}")
            previous = action.repo
        if isinstance(action, CreateAction):
            issue = ResolvedIssue(
                receipt.issue if receipt and receipt.state == "applied" else None, action.ref
            )
            refs[action.ref] = issue
        elif isinstance(action.target, ExistingIssue):
            issue = ResolvedIssue(action.target.number)
        else:
            issue = refs[action.target.ref]
        lines.extend(_action_lines(manifest.name, index, action, issue, receipt, None))
    return lines


# --------------------------------------------------------------------------
# Applying a batch
#
# Every pending action is journaled *before* its GitHub mutation is
# dispatched (`mark_prepared`), and the mutation's own outcome is journaled
# again (`mark_applied`/`mark_uncertain`/`clear_prepared`) before the next
# action is even considered — both journal writes for one action happen
# inside a single exclusive lock held across the mutation itself, so a
# concurrent reconciliation or drop can never observe a half-finished
# action. A GitHub outcome jailbee cannot durably record is never treated as
# "done" or "safe to retry": the run stops, and the next `load()` recovers
# a leftover `prepared` record as `uncertain` (see `outbox_io.py`).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyFailure:
    """Why `apply_batch` stopped, and how much of it is known for certain.

    `manifest`/`index` name the action that stopped the run — both `None`
    for a batch-wide refusal (the container's identity changed, the outbox
    could not be read) that never reached a single action. `uncertain` is
    true whenever the *journal* itself is now ambiguous — either because the
    GitHub outcome genuinely was (a timeout, a dropped connection), or
    because the definite outcome could not be durably recorded and the next
    `load()` will recover it as uncertain regardless. `detail` is always
    safe to show a human: never raw `gh` output, a manifest body, or a
    token.
    """

    manifest: str | None
    index: int | None
    uncertain: bool
    detail: str


@dataclass(frozen=True)
class ApplyReport:
    """What one `apply_batch` call did, up to and including its first failure.

    `applied` and `skipped` are `(manifest name, receipt)` pairs in the exact
    order they were encountered — `skipped` for an action `apply_batch`
    found already `applied` from an earlier run (a restored create ref
    included), `applied` for one this call itself dispatched. `cleaned`
    lists the manifests that became fully applied *and* were logged,
    deleted, and archived, in that order. `failure` is `None` only when
    every manifest in the batch reached that state.
    """

    applied: tuple[tuple[str, JournalAction], ...]
    skipped: tuple[tuple[str, JournalAction], ...]
    cleaned: tuple[str, ...]
    failure: ApplyFailure | None


@dataclass(frozen=True)
class AppliedResolution:
    """A human's confirmation that an uncertain mutation actually landed.

    `issue` is required, and must agree with `url`, for a create — a
    create's issue number cannot be inferred from anywhere else. For every
    other action kind it is forbidden: the target issue is already known
    from the manifest (or from an earlier create's own receipt), so a human
    supplying one here could only ever contradict it.
    """

    url: str
    issue: int | None


@dataclass(frozen=True)
class RetryResolution:
    """A human's instruction to forget an uncertain action and retry it."""


Resolution = AppliedResolution | RetryResolution


_SAFE_DEFINITE_DETAILS = frozenset(
    {
        "GitHub mutation was rejected",
        "GitHub mutation could not start because 'gh' is unavailable",
    }
)
_DEFAULT_DEFINITE_DETAIL = "GitHub mutation failed"


def _apply_failure_detail(message: str, *, uncertain: bool) -> str:
    """Map a mutation error's own message to one of a known-safe, fixed set.

    Mirrors `outbox_io.safe_mutation_detail` (used for the *persisted*
    uncertain detail) for the in-memory `ApplyFailure.detail`, which must be
    just as safe to show even though nothing here is written to disk. A
    definite rejection has its own distinct safe set and generic default —
    reusing the uncertain-flavored default would misdescribe a definite
    failure as uncertain.
    """
    if uncertain:
        return safe_mutation_detail(message)
    return message if message in _SAFE_DEFINITE_DETAILS else _DEFAULT_DEFINITE_DETAIL


def _now_iso() -> str:
    """Current UTC time as `2026-09-18T12:34:56Z`, for `applied.log` lines."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _referenced_elsewhere(outbox: OutboxSnapshot, exclude_name: str) -> frozenset[str] | None:
    """Body files other manifests in `outbox` still reference, or None if unknown.

    Returns `None` — "cannot tell, so keep everything" — the instant any
    *other* manifest in the outbox fails to parse, rather than risk deleting
    a file a broken manifest still names. The container is untrusted input;
    an unparseable manifest is exactly the hostile-or-corrupt case that must
    fail safe, not silently drop what might be someone else's still-pending
    proposal text.
    """
    referenced: set[str] = set()
    for name in outbox.manifest_names:
        if name == exclude_name:
            continue
        try:
            other = parse_manifest(name, outbox.files[name], outbox.files)
        except IssueManifestError:
            return None
        referenced.update(other.body_files)
    return frozenset(referenced)


def _resolved_number(issue: ResolvedIssue, created: Mapping[str, int]) -> int:
    """The issue number `issue` targets: its own, or a ref created this run."""
    if issue.number is not None:
        return issue.number
    assert issue.ref is not None, "a resolved issue always carries a number or a create ref"
    return created[issue.ref]


def _execute_one(
    host_repo_root: Path, resolved: ResolvedAction, created: dict[str, int]
) -> MutationReceipt:
    """Dispatch one pending action's exact GitHub mutation; return its receipt.

    A successful create records its own issue number into `created` (keyed
    by its manifest-local `ref`) before returning, so a later action in the
    same call that targets that ref by `issue_ref` can resolve it — the ref
    is otherwise not known until this very call returns.
    """
    action = resolved.action
    repo = resolved.repo.slug
    if isinstance(action, CreateAction):
        receipt = issue_github.create_issue(
            host_repo_root,
            repo,
            title=action.title,
            body=action.body,
            labels=resolved.labels or (),
        )
        created[action.ref] = receipt.issue
        return receipt
    number = _resolved_number(resolved.issue, created)
    if isinstance(action, EditAction):
        return issue_github.edit_issue(
            host_repo_root, repo, number, title=action.title, body=action.body
        )
    if isinstance(action, CommentAction):
        return issue_github.add_comment(host_repo_root, repo, number, body=action.body)
    if isinstance(action, LabelsAction):
        return issue_github.replace_labels(
            host_repo_root, repo, number, labels=resolved.labels or ()
        )
    if isinstance(action, StateAction):
        return issue_github.set_state(
            host_repo_root, repo, number, state=action.state, reason=action.reason
        )
    assert_never(action)


def _create_or_replace_journal(
    journal_store: JournalStore, key: JournalKey, digest: str, action_count: int
) -> IssueJournal:
    """Create this proposal's journal, replacing a stale, untouched old one.

    A changed proposal digest is only ever allowed to displace an old
    journal that recorded no progress at all (Task 7 decision 4) — anything
    else is a real conflict, left for `journal_store.create` itself to
    reject. Archiving the stale journal and creating the new one happen
    under one lock, so a concurrent reader never observes the old digest's
    journal gone with the new one not yet in place.
    """
    with journal_store.lock(key):
        existing = journal_store.load(key)
        if existing is not None and existing.digest != digest and not existing.actions:
            journal_store.archive(key)
        return journal_store.create(key, digest, action_count)


def _log_line(manifest_name: str, action: JournalAction) -> str:
    """One `applied.log` entry: identifiers and a receipt URL, never body text."""
    return json.dumps(
        {
            "timestamp": _now_iso(),
            "manifest": manifest_name,
            "index": action.index,
            "repo": action.repo,
            "issue": action.issue,
            "url": action.url,
        },
        sort_keys=True,
    )


def _cleanup_manifest(
    incus: Incus,
    container: str,
    prepared: PreparedManifest,
    *,
    uid: int | None,
    journal_store: JournalStore,
    identity: ContainerIdentity,
) -> str | None:
    """Log, delete, and archive a manifest that just became fully applied.

    Returns `None` on success, or a human-readable failure detail otherwise.
    Each step is attempted only if the previous one succeeded, and the host
    journal is left exactly as it was — still active, still showing every
    action applied — the instant a container write fails, so a re-run
    repeats only the cleanup, never a GitHub mutation. Deletes the manifest
    itself unconditionally, but only the body files no *other* manifest
    still in the (freshly re-read) outbox references; a shared Markdown
    file is kept. Archiving the journal is the last step, after both the
    log and the delete succeed, so the same manifest filename can safely
    carry a new proposal afterward.
    """
    name = prepared.manifest.name
    key = journal_key(identity, name)
    journal = journal_store.load(key)
    if journal is None:
        return f"{name}: applied, but its journal disappeared before cleanup could run"
    directory = _outbox_directory()

    lines = [_log_line(name, action) for action in sorted(journal.actions, key=lambda a: a.index)]
    try:
        append_applied_log(incus, container, directory, lines, uid=uid)
    except IncusError as exc:
        return (
            f"{name}: applied to GitHub, but the applied.log entry could not be "
            f"recorded ({exc}); a re-run will retry safely"
        )

    try:
        fresh_outbox = read_issue_outbox(incus, container, uid=uid)
    except OutboxReadError as exc:
        return f"{name}: logged, but the outbox could not be re-read to clean it up ({exc})"
    referenced = _referenced_elsewhere(fresh_outbox, name)
    names = [name]
    if referenced is not None:
        names.extend(sorted(f for f in prepared.manifest.body_files if f not in referenced))
    try:
        delete_outbox_files(incus, container, directory, names, uid=uid)
    except IncusError as exc:
        return (
            f"{name}: applied and logged, but its outbox files could not be "
            f"deleted ({exc}); a re-run will retry safely"
        )

    try:
        with journal_store.lock(key):
            journal_store.archive(key)
    except JournalError as exc:
        return (
            f"{name}: applied, logged, and deleted, but its journal could not be archived ({exc})"
        )
    return None


def apply_batch(
    batch: PreparedBatch, *, incus: Incus, uid: int | None, journal_store: JournalStore
) -> ApplyReport:
    """Apply every pending action of `batch`, in the exact order it was shown.

    Two passes. The first re-verifies every manifest — the container's
    identity, each manifest's exact text (by recomputed digest), and its
    journal's recorded progress — *before* a single GitHub mutation is
    dispatched, so a change discovered in the last manifest still aborts the
    first one's untouched. A freshly-discovered `uncertain` record aborts
    the whole batch, and so does an already-`applied` record whose recorded
    repo no longer matches the freshly resolved one. The second pass then
    walks each manifest's actions in display order, skipping ones already
    `applied` (restoring a create's issue number into `created_issue_numbers`
    for any later `issue_ref`), and for a pending action: durably marks it
    `prepared`, dispatches its exact mutation, and durably marks the
    outcome — all under one lock per action, so a concurrent reconciliation
    or drop can never observe a half-finished one. The run stops at the
    first action it cannot durably resolve one way or the other; nothing
    later is ever attempted. A manifest that becomes fully applied is
    logged, deleted, and archived before the next manifest starts.
    """
    applied: list[tuple[str, JournalAction]] = []
    skipped: list[tuple[str, JournalAction]] = []
    cleaned: list[str] = []

    def failed(
        manifest: str | None, index: int | None, uncertain: bool, detail: str
    ) -> ApplyReport:
        return ApplyReport(
            tuple(applied),
            tuple(skipped),
            tuple(cleaned),
            ApplyFailure(manifest, index, uncertain, detail),
        )

    try:
        fresh_identity = container_identity(incus, batch.container)
    except (JournalError, IncusError) as exc:
        return failed(None, None, False, f"could not verify the container's identity: {exc}")
    if fresh_identity != batch.identity:
        return failed(
            None,
            None,
            False,
            "the container's identity changed since this batch was approved; re-approve it",
        )

    try:
        outbox = read_issue_outbox(incus, batch.container, uid=uid)
    except OutboxReadError as exc:
        return failed(None, None, False, str(exc))

    journals: dict[str, IssueJournal] = {}
    for prepared in batch.manifests:
        name = prepared.manifest.name
        text = outbox.files.get(name)
        if text is None:
            return failed(name, None, False, f"{name}: manifest is no longer in the outbox")
        digest = proposal_digest(
            name,
            text,
            {body: outbox.files.get(body, "") for body in prepared.manifest.body_files},
        )
        if digest != prepared.digest:
            return failed(name, None, False, f"{name}: proposal changed since it was approved")
        key = journal_key(batch.identity, name)
        try:
            journal = _create_or_replace_journal(
                journal_store, key, digest, len(prepared.manifest.actions)
            )
        except JournalError as exc:
            return failed(name, None, False, str(exc))
        receipts = {a.index: a for a in journal.actions}
        for resolved in prepared.actions:
            current = receipts.get(resolved.index)
            if _status(current) == "uncertain":
                assert current is not None
                return failed(
                    name,
                    resolved.index,
                    True,
                    f"{name} action {resolved.index}: a previous run's outcome is uncertain "
                    f"({current.detail or 'unknown'}) and must be reconciled before this batch "
                    "can proceed",
                )
            if current is not None and current.repo.casefold() != resolved.repo.identity:
                return failed(
                    name,
                    resolved.index,
                    False,
                    f"{name} action {resolved.index}: its recorded repo no longer matches "
                    "the resolved repo; re-approve this batch",
                )
        journals[name] = journal

    for prepared in batch.manifests:
        name = prepared.manifest.name
        key = journal_key(batch.identity, name)
        journal = journals[name]
        receipts = {a.index: a for a in journal.actions}
        created_issue_numbers: dict[str, int] = {}
        for resolved in prepared.actions:
            current = receipts.get(resolved.index)
            if current is not None:
                skipped.append((name, current))
                if isinstance(resolved.action, CreateAction) and current.issue is not None:
                    created_issue_numbers[resolved.action.ref] = current.issue
                continue

            with journal_store.lock(key):
                try:
                    journal_store.mark_prepared(key, resolved.index, repo=resolved.repo.slug)
                except JournalError as exc:
                    return failed(name, resolved.index, False, str(exc))
                try:
                    receipt = _execute_one(batch.host_repo_root, resolved, created_issue_numbers)
                except IssueGithubMutationError as exc:
                    recorded = True
                    try:
                        if exc.uncertain:
                            journal_store.mark_uncertain(
                                key, resolved.index, repo=resolved.repo.slug, detail=str(exc)
                            )
                        else:
                            journal_store.clear_prepared(key, resolved.index)
                    except JournalError:
                        recorded = False
                    return failed(
                        name,
                        resolved.index,
                        exc.uncertain or not recorded,
                        _apply_failure_detail(str(exc), uncertain=exc.uncertain),
                    )
                try:
                    updated = journal_store.mark_applied(
                        key,
                        resolved.index,
                        repo=resolved.repo.slug,
                        url=receipt.url,
                        issue=receipt.issue,
                    )
                except JournalError as exc:
                    return failed(
                        name,
                        resolved.index,
                        True,
                        f"GitHub mutation succeeded but its receipt could not be recorded "
                        f"({exc}); a re-run will treat it as uncertain",
                    )

            applied_action = next(a for a in updated.actions if a.index == resolved.index)
            applied.append((name, applied_action))
            if isinstance(resolved.action, CreateAction):
                created_issue_numbers[resolved.action.ref] = receipt.issue

        failure_detail = _cleanup_manifest(
            incus,
            batch.container,
            prepared,
            uid=uid,
            journal_store=journal_store,
            identity=batch.identity,
        )
        if failure_detail is not None:
            return failed(name, None, False, failure_detail)
        cleaned.append(name)

    return ApplyReport(tuple(applied), tuple(skipped), tuple(cleaned), None)


# --------------------------------------------------------------------------
# Explicit reconciliation
#
# The one path that turns an `uncertain` action into either `applied` (a
# human confirms, from GitHub's own UI, exactly what landed) or nothing at
# all (a human says: forget it, retry). Neither ever touches a `pending` or
# `applied` action, or a proposal that no longer matches what the human is
# looking at.
# --------------------------------------------------------------------------

_RECEIPT_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>[1-9][0-9]*)"
    r"(?:#\S*)?$"
)


def _validated_receipt(url: str, repo: RepoTarget) -> int:
    """Parse and validate a GitHub issue receipt URL; return its issue number.

    Deliberately strict: exactly `https://github.com/<owner>/<repo>/issues/<n>`,
    optionally followed by a `#fragment` (a comment permalink), matching
    `repo` case-insensitively. No query string, no `pull`, no credentials in
    the authority, no host lookalike, no trailing garbage.
    """
    match = _RECEIPT_URL_RE.fullmatch(url)
    if match is None:
        raise JournalError(f"not a GitHub issue URL: {url!r}")
    slug = f"{match['owner']}/{match['repo']}"
    if slug.casefold() != repo.identity:
        raise JournalError(f"receipt URL does not name repository {repo.slug!r}: {url!r}")
    return int(match["number"])


def _expected_issue_number(
    action: IssueAction, manifest: IssueManifest, journal: IssueJournal
) -> int | None:
    """The issue number a non-create action's receipt must agree with, if known."""
    assert not isinstance(action, CreateAction)
    target = action.target
    if isinstance(target, ExistingIssue):
        return target.number
    assert isinstance(target, CreatedIssue)
    receipts = {a.index: a for a in journal.actions}
    for other_index, other in enumerate(manifest.actions):
        if isinstance(other, CreateAction) and other.ref == target.ref:
            receipt = receipts.get(other_index)
            return receipt.issue if receipt is not None and receipt.state == "applied" else None
    return None


def reconcile_action(
    *,
    key: JournalKey,
    index: int,
    resolution: Resolution,
    repo: RepoTarget,
    manifest: IssueManifest,
    digest: str,
    journal_store: JournalStore,
) -> None:
    """Apply a human's explicit resolution to one uncertain action.

    `manifest` and `digest` pin this call to the exact proposal the human
    is looking at: `JournalKey`/`JournalAction` encode neither the action
    kind nor the caller's current proposal digest, so without them a stale
    or wrong-proposal resolution could not be rejected. Only an action
    currently `uncertain` (never `pending` or `applied`) may be resolved,
    and `repo` must match what was actually journaled for it — a defense
    against reconciling the right index against the wrong repository.

    `RetryResolution` simply forgets the uncertain record, leaving the
    action to be re-attempted as `pending`. `AppliedResolution` requires a
    `https://github.com/<repo>/issues/<n>` receipt matching `repo`; for a
    create, `issue` must be supplied and agree with that URL's number; for
    every other action kind, `issue` is forbidden and, when the action's own
    target issue is already known (an explicit number, or an earlier
    create's own applied receipt), the URL's number must agree with it too.
    """
    if not 0 <= index < len(manifest.actions):
        raise JournalError("reconciliation index is out of bounds for this manifest")
    journal = journal_store.load(key)
    if journal is None:
        raise JournalError(f"no journal exists for {key.manifest_name}")
    if journal.manifest_name != manifest.name:
        raise JournalError("journal manifest name does not match this proposal")
    if journal.digest != digest:
        raise JournalError("journal digest does not match this proposal")
    if journal.action_count != len(manifest.actions):
        raise JournalError("journal action count does not match this proposal")
    current = next((a for a in journal.actions if a.index == index), None)
    if current is None or current.state != "uncertain":
        raise JournalError("only an uncertain action can be reconciled")
    if current.repo.casefold() != repo.identity:
        raise JournalError("journal action repo does not match the resolved repo")

    if isinstance(resolution, RetryResolution):
        journal_store.resolve_retry(key, index)
        return

    action = manifest.actions[index]
    url_number = _validated_receipt(resolution.url, repo)
    if isinstance(action, CreateAction):
        if type(resolution.issue) is not int:
            raise JournalError("a create's resolution requires its GitHub issue number")
        if resolution.issue != url_number:
            raise JournalError("resolution issue number does not match the receipt URL")
    else:
        if resolution.issue is not None:
            raise JournalError("only a create's resolution may carry an issue number")
        expected = _expected_issue_number(action, manifest, journal)
        if expected is not None and expected != url_number:
            raise JournalError("receipt URL does not match this action's issue")

    journal_store.resolve_applied(key, index, url=resolution.url, issue=resolution.issue)


# --------------------------------------------------------------------------
# Manual cleanup
#
# Discarding a proposal the human never wants applied. The default refuses
# any recorded progress at all — even settled, all-`applied` progress —
# because silently deleting a manifest that already changed GitHub without
# a trace would erase the only record of it; `archive_journal=True` is the
# explicit opt-in to keep that record while abandoning the rest.
# --------------------------------------------------------------------------


def drop_manifest(
    incus: Incus,
    container: str,
    outbox: OutboxSnapshot,
    manifest_name: str,
    *,
    uid: int | None,
    journal_store: JournalStore,
    identity: ContainerIdentity,
    archive_journal: bool = False,
) -> tuple[str, ...]:
    """Delete `manifest_name` from the container's outbox without applying it.

    Refuses to touch a manifest whose text no longer matches `outbox` — the
    snapshot the caller read and is asking to drop — a fresh read must
    still agree with it. By default, any recorded journal progress at all
    refuses the drop outright. With `archive_journal=True`, uncertainty
    still refuses it, but settled progress (applied actions, pending ones
    left untouched) is allowed: the container files are deleted *first*,
    and only once that succeeds is the journal archived — so a delete
    failure leaves the active journal exactly as it was, still the replay
    barrier for whatever was already applied.

    Deletes the manifest itself unconditionally, but only the body files no
    *other* manifest in a freshly re-read outbox still references — not
    `outbox` itself, which may predate a manifest written after the caller's
    own read. A shared Markdown file is kept either way. Returns the
    outbox-relative names actually deleted.
    """
    fresh = read_issue_outbox(incus, container, uid=uid)
    if fresh.files.get(manifest_name) != outbox.files.get(manifest_name):
        raise JournalError(
            f"{manifest_name}: the outbox changed since it was read; re-read it before dropping"
        )
    text = outbox.files.get(manifest_name)
    if text is None:
        raise JournalError(f"{manifest_name}: manifest is not in the outbox")

    key = journal_key(identity, manifest_name)
    journal = journal_store.load(key)
    has_uncertainty = journal is not None and journal_has_uncertainty(journal)
    if archive_journal:
        if has_uncertainty:
            raise JournalError(
                f"{manifest_name}: cannot archive a journal containing uncertain progress"
            )
    elif journal is not None and journal.actions:
        raise JournalError(
            f"{manifest_name}: cannot drop a manifest with recorded progress; pass "
            "archive_journal=True to archive its settled progress instead"
        )

    try:
        parsed = parse_manifest(manifest_name, text, fresh.files)
        body_files: frozenset[str] = parsed.body_files
    except IssueManifestError:
        body_files = frozenset()
    referenced = _referenced_elsewhere(fresh, manifest_name)
    names = [manifest_name]
    if referenced is not None:
        names.extend(sorted(f for f in body_files if f not in referenced))

    delete_outbox_files(incus, container, _outbox_directory(), names, uid=uid)

    if archive_journal and journal is not None and journal.actions:
        journal_store.archive(key)

    return tuple(names)
