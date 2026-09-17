"""Host-side orchestration for GitHub issue outbox actions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from jailbee import git, issue_github, submodules
from jailbee.config import CONTAINER_USERNAME
from jailbee.github_repo import github_slug, resolve_submodule_url
from jailbee.incus import Incus, IncusError
from jailbee.issue_github import IssueGithubReadError, IssueSnapshot
from jailbee.issue_manifest import (
    MAX_MANIFEST_BYTES,
    MAX_MANIFESTS,
    MAX_OFFER_ACTIONS,
    CommentAction,
    CreateAction,
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
    JournalStore,
    container_identity,
    journal_key,
    proposal_digest,
    read_text_outbox,
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


def read_issue_outbox(incus: Incus, container: str, *, uid: int | None) -> OutboxSnapshot:
    """Read proposal text without accepting container-owned progress."""
    files = read_text_outbox(
        incus,
        container,
        f"/home/{CONTAINER_USERNAME}/.jailbee/issue-outbox",
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
