"""Host-side orchestration for GitHub issue outbox actions."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, assert_never

from jailbee import git, issue_github, issue_projects, submodules
from jailbee.github_repo import github_slug, resolve_submodule_url
from jailbee.incus import Incus, IncusError
from jailbee.issue_github import (
    IssueGithubMutationError,
    IssueGithubReadError,
    IssueSnapshot,
    MutationReceipt,
)
from jailbee.issue_manifest import (
    MAX_MANIFESTS,
    MAX_OFFER_ACTIONS,
    ME_LOGIN,
    AssigneesAction,
    CommentAction,
    CreateAction,
    CreatedIssue,
    EditAction,
    ExistingIssue,
    IssueAction,
    IssueManifest,
    IssueManifestError,
    LabelsAction,
    ProjectAddAction,
    StateAction,
    foreign_slug,
    mutation_keys,
    parse_manifest,
)
from jailbee.outbox import publication_db, sent
from jailbee.outbox.markdown_view import render_markdown
from jailbee.outbox_io import (
    ASSIGN_NOT_APPLIED_DETAIL,
    UNASSIGN_NOT_APPLIED_DETAIL,
    ContainerIdentity,
    IssueJournal,
    JournalAction,
    JournalError,
    proposal_digest,
    safe_mutation_detail,
)

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.outbox.publication_db import PublicationStore

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoTarget:
    """A host-authorized repository an issue manifest may name."""

    path: str
    repo_root: Path
    slug: str
    foreign: bool = False

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
        sub = submodules.resolve_host_subrepo(cfg.repo_root, declared.path)
        repo_root = sub.root if sub is not None else cfg.repo_root / declared.path
        if sub is not None:
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


def target_for(
    targets: Mapping[str, RepoTarget], repo: str, host_repo_root: Path
) -> RepoTarget | None:
    """The target an action's ``repo`` names, or None when the host does not authorize it.

    ``.`` and submodule paths come from ``targets``. ``github:<owner>/<name>``
    names a slug directly: the authorized target with that slug when there is
    one, otherwise a foreign target. ``host_repo_root`` is only the working
    directory of the host's ``gh`` calls, which name the slug explicitly.
    """
    known = targets.get(repo)
    if known is not None:
        return known
    slug = foreign_slug(repo)
    if slug is None:
        return None
    for target in targets.values():
        if target.identity == slug.casefold():
            return target
    return RepoTarget(path=repo, repo_root=host_repo_root, slug=slug, foreign=True)


class IssueGateError(Exception):
    """One or more read-only checks refuse a proposed batch."""


class IssueExecutionError(IssueGateError):
    """A required read or transport failed; legacy gate catches remain compatible."""


class IssueStaleError(IssueGateError):
    """An expected field changed after the approval plan was prepared."""


@dataclass(frozen=True)
class OutboxSnapshot:
    files: Mapping[str, str]

    @property
    def manifest_names(self) -> tuple[str, ...]:
        return tuple(sorted(name for name in self.files if name.endswith(".json")))


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
    project: issue_projects.Project | None = None
    project_target: issue_projects.ProjectTarget | None = None


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
    # The instance's Incus volatile.uuid: the outbox store's key for its proposals.
    uuid: str = ""

    def _foreign_actions(self) -> list[ResolvedAction]:
        # A receipt in any state means the action was already dispatched.
        # "uncertain" still maps to status "pending" via _status, so status
        # alone would count a refused-to-reconcile action as fresh foreign
        # work and a later refusal would write a journal first.
        pending: list[ResolvedAction] = []
        for manifest in self.manifests:
            receipts = (
                {receipt.index for receipt in manifest.journal.actions}
                if manifest.journal is not None
                else set()
            )
            pending.extend(
                action
                for action in manifest.actions
                if action.repo.foreign
                and action.status == "pending"
                and action.index not in receipts
            )
        return pending

    @property
    def foreign_slugs(self) -> tuple[str, ...]:
        """Display slugs of foreign repositories that still have a pending action."""
        slugs: dict[str, str] = {}
        for action in self._foreign_actions():
            slugs.setdefault(action.repo.identity, action.repo.slug)
        return tuple(sorted(slugs.values(), key=str.casefold))

    @property
    def foreign_pending(self) -> int:
        return len(self._foreign_actions())


def store_outbox(uuid: str) -> OutboxSnapshot:
    """The instance's staged issue proposals from the host outbox store.

    Read-only on ``broker.db`` (no Incus call); manifests and their bodies are
    keyed by name as in the old file layout. Raises `StoreError`.
    """
    from jailbee.outbox import publication_db, store_db

    files: dict[str, str] = {}
    for row in store_db.read_proposals(publication_db.default_root(), uuid, "issue"):
        files[row.name] = row.manifest
        files.update(row.bodies)
    return OutboxSnapshot(MappingProxyType(files))


def store_journals(identity: ContainerIdentity, uuid: str) -> dict[str, IssueJournal]:
    """Active issue journals of the instance by manifest name (prepared read as uncertain)."""
    from jailbee.outbox import publication_db

    return {
        name: _issue_journal(identity, journal)
        for (_, name), journal in publication_db.read_journals(
            publication_db.default_root(), uuid, "issue"
        ).items()
    }


def _issue_journal(identity: ContainerIdentity, journal: publication_db.Journal) -> IssueJournal:
    return IssueJournal(
        identity=identity,
        manifest_name=journal.name,
        digest=journal.digest,
        action_count=journal.action_count,
        actions=journal.actions,
    )


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
    elif isinstance(action, AssigneesAction):
        fields["assignees"] = frozenset(login.casefold() for login in action.expected_assignees)
    elif isinstance(action, StateAction):
        fields["state"] = action.expected_state
    return fields


def resolved_assignees(
    action: AssigneesAction, login: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The (add, remove) logins of ``action`` with ``@me`` replaced by the publisher ``login``.

    An empty ``login`` (not resolved) leaves ``@me`` as written.
    """

    def resolve(logins: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(login if item == ME_LOGIN and login else item for item in logins)

    return resolve(action.add), resolve(action.remove)


def _assignee_overlap(action: AssigneesAction, login: str) -> bool:
    add, remove = resolved_assignees(action, login)
    return bool({item.casefold() for item in add} & {item.casefold() for item in remove})


def _assignees_differ(action: AssigneesAction, snapshot: IssueSnapshot, login: str) -> bool:
    """Whether a login this action does not touch changed since the expected set was recorded.

    The action may have landed partly or wholly on an earlier uncertain
    attempt, so any combination of its own add/remove logins is acceptable
    (replaying the action is idempotent there). Everything else must still
    equal the expected set; a change to an unrelated login is stale.
    """
    add, remove = resolved_assignees(action, login)
    touched = {item.casefold() for item in add} | {item.casefold() for item in remove}
    expected = {item.casefold() for item in action.expected_assignees}
    live = {item.casefold() for item in snapshot.assignees}
    return live - touched != expected - touched


def _differences(
    action: IssueAction, snapshot: IssueSnapshot, context: str, login: str = ""
) -> list[str]:
    differences = []
    for field, expected in _expected_fields(action).items():
        if field == "assignees":
            assert isinstance(action, AssigneesAction)
            if _assignees_differ(action, snapshot, login):
                differences.append(f"{context}: expected.{field} differs from the current issue")
            continue
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
    host_repo_root: Path,
    journals: Mapping[str, IssueJournal],
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
            if target_for(targets, action.repo, host_repo_root) is None:
                known = ", ".join(repr(path) for path in sorted(targets)) or "[none]"
                refusals.append(
                    f"{name} action {index}: forbidden repo path {action.repo!r}; "
                    f"known paths: {known}, or github:<owner>/<name>"
                )
        try:
            journal = journals.get(name)
            digest = proposal_digest(
                name, outbox.files[name], {body: outbox.files[body] for body in manifest.body_files}
            )
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
                        target = target_for(targets, action.repo, host_repo_root)
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
) -> PreparedBatch:
    """Resolve and validate an offer using only host-authorized, read-only inputs.

    Proposals and journals come from the host outbox store; the only Incus call
    resolves the instance's identity and uuid.
    """
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox.service import instance_identity

    try:
        identity, uuid = instance_identity(incus, container)
        outbox = store_outbox(uuid)
        journals = store_journals(identity, uuid)
    except (StoreError, JournalError, IncusError) as exc:
        raise IssueExecutionError(str(exc)) from exc
    refusals: list[str] = []
    execution_errors: list[IssueGithubReadError] = []
    try:
        targets = resolve_repo_targets(cfg)
    except ValueError as exc:
        refusals.append(str(exc))
        targets = {}
    proposals = _load_proposals(outbox, selected_names, targets, cfg.repo_root, journals, refusals)
    if refusals:
        raise IssueGateError("\n".join(refusals))

    try:
        login = issue_github.current_login(cfg.repo_root)
    except IssueGithubReadError as exc:
        execution_errors.append(exc)
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
            repo = target_for(targets, action.repo, cfg.repo_root)
            assert repo is not None, "_load_proposals refused unauthorized repos"
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
                            execution_errors.append(exc)
                            issue_errors[key] = str(exc)
                    if key in issue_errors:
                        refusals.append(f"{context}: {issue_errors[key]}")
                    snapshot = issues.get(key)
                elif issue.ref is not None:
                    snapshot = created.get(issue.ref)
                if snapshot is not None:
                    if snapshot.is_pull_request and not isinstance(action, ProjectAddAction):
                        refusals.append(f"{context}: target is a pull request, not an issue")
                    refusals.extend(_differences(action, snapshot, context, login))

            labels = None
            if isinstance(action, (CreateAction, LabelsAction)) and status == "pending":
                if repo.identity not in label_maps and repo.identity not in label_errors:
                    try:
                        label_maps[repo.identity] = issue_github.list_labels(
                            cfg.repo_root, repo.slug
                        )
                    except IssueGithubReadError as exc:
                        # A foreign repo's label list is advisory: GitHub
                        # drops labels the publisher cannot set, so a failed
                        # read must not refuse this action or the batch.
                        if not repo.foreign:
                            execution_errors.append(exc)
                        label_errors[repo.identity] = str(exc)
                if repo.identity in label_errors and not repo.foreign:
                    refusals.append(f"{context}: {label_errors[repo.identity]}")
                # A foreign label list the host user cannot read is not a
                # refusal. Skip pre-validation and send the action's own
                # labels; GitHub drops any the publisher cannot set.
                # _execute_one treats None as no labels, so None is wrong here.
                if repo.foreign and repo.identity in label_errors:
                    labels = (
                        action.labels
                        if isinstance(action, CreateAction)
                        else tuple(
                            label
                            for label in action.expected_labels
                            if label.casefold() not in {item.casefold() for item in action.remove}
                        )
                        + action.add
                    )
                else:
                    labels = _resolve_labels(
                        action, label_maps.get(repo.identity), context, refusals
                    )

            if status == "pending" and isinstance(action, AssigneesAction):
                if _assignee_overlap(action, login):
                    refusals.append(f"{context}: a login cannot appear in both add and remove")
            if status == "pending":
                for field in mutation_keys(action):
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
            project = project_target = None
            if isinstance(action, ProjectAddAction) and status == "pending":
                try:
                    project = issue_projects.resolve_project(cfg.repo_root, action.project)
                    if status == "pending":
                        project_target = (
                            issue_projects.get_target(
                                cfg.repo_root, project, repo.slug, issue.number
                            )
                            if issue.number is not None
                            else None
                        )
                        present = project_target.present if project_target else False
                        if present != action.expected_present:
                            refusals.append(f"{context}: project membership changed since staging")
                except IssueGithubReadError as exc:
                    execution_errors.append(exc)
                    refusals.append(f"{context}: {exc}")
            actions.append(
                ResolvedAction(index, action, repo, issue, status, labels, project, project_target)
            )
        prepared.append(PreparedManifest(manifest, digest, journal, tuple(actions)))
    if execution_errors:
        raise IssueExecutionError("\n".join(refusals)) from execution_errors[0]
    if refusals:
        raise IssueGateError("\n".join(refusals))
    return PreparedBatch(
        container,
        cfg.repo_root,
        identity,
        login,
        outbox,
        tuple(prepared),
        MappingProxyType(issues),
        uuid,
    )


def revalidate_batch(batch: PreparedBatch) -> None:
    """Refetch only pending existing mutations and compare their declared fields."""
    fresh: dict[tuple[str, int], IssueSnapshot] = {}
    errors: dict[tuple[str, int], str] = {}
    execution_errors: list[IssueGithubReadError] = []
    refusals = []
    for manifest in batch.manifests:
        for resolved in manifest.actions:
            if isinstance(resolved.action, ProjectAddAction) and resolved.status == "pending":
                if resolved.issue.number is not None:
                    assert resolved.project is not None
                    try:
                        target = issue_projects.get_target(
                            batch.host_repo_root,
                            resolved.project,
                            resolved.repo.slug,
                            resolved.issue.number,
                        )
                        if target.present != resolved.action.expected_present:
                            refusals.append(
                                f"{manifest.manifest.name} action {resolved.index}: "
                                "project membership changed since staging"
                            )
                    except IssueGithubReadError as exc:
                        execution_errors.append(exc)
                        refusals.append(str(exc))
                continue
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
                    execution_errors.append(exc)
                    errors[key] = str(exc)
            if key in errors:
                refusals.append(f"{context}: {errors[key]}")
                continue
            snapshot = fresh[key]
            if snapshot.is_pull_request:
                refusals.append(f"{context}: target is a pull request, not an issue")
            refusals.extend(_differences(resolved.action, snapshot, context, batch.login))
    if execution_errors:
        raise IssueExecutionError("\n".join(refusals)) from execution_errors[0]
    if refusals:
        raise IssueStaleError("\n".join(refusals))


def _prose(label: str, text: str | None, *, markdown: bool = False) -> list[str]:
    if markdown and text:
        return [f"  {label}:", *render_markdown(text, indent="    ")]
    return [f"  {label}:", *(f"    {line}" for line in (text or "[empty]").split("\n"))]


def _assignee_changes(action: AssigneesAction, login: str) -> str:
    """``assignees +alice \u2212bob``, ``@me`` resolved when the publisher ``login`` is known."""
    add, remove = resolved_assignees(action, login)
    changes = [f"+{item}" for item in add] + [f"\u2212{item}" for item in remove]
    return "assignees " + " ".join(changes)


def _action_lines(
    name: str,
    index: int,
    action: IssueAction,
    issue: ResolvedIssue,
    receipt: JournalAction | None,
    labels: tuple[str, ...] | None,
    container: str,
    login: str = "",
    project: issue_projects.Project | None = None,
) -> list[str]:
    kind = action.kind
    target = f"#{issue.number}" if issue.number is not None else f"ref {issue.ref}"
    status = _status(receipt)
    display = "applied; skip" if status == "applied" else status
    lines = [f"{name} action {index}: {kind} {target} [{display}]"]
    if receipt and receipt.url:
        lines.append(f"  receipt: {receipt.url}")
    if receipt and receipt.detail:
        lines.append(f"  detail: {receipt.detail}")
    if status == "uncertain":
        lines.append(
            f"  resolve: jailbee issue resolve {container} {name} {index} "
            "(--applied --url <url> [--issue <n>] | --retry)"
        )
    if not isinstance(action, CreateAction) and issue.ref is not None:
        lines.append(f"  depends on create ref {issue.ref}")
    if isinstance(action, CreateAction):
        if issue.number is not None:
            lines.append(f"  create ref: {action.ref}")
        lines.extend(_prose("title", action.title))
        lines.extend(_prose("body", action.body, markdown=True))
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
        lines.extend(_prose("body", action.body, markdown=True))
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
    elif isinstance(action, AssigneesAction):
        lines.append(f"  {_assignee_changes(action, login)}")
    elif isinstance(action, ProjectAddAction):
        title = f' "{project.title}"' if project else ""
        lines.append(f"  project {action.project}{title} · {target} · add")
        lines.append(f"  membership before: {'present' if action.expected_present else 'absent'}")
    elif isinstance(action, StateAction):
        lines.append(f"  state: {action.expected_state} -> {action.state}")
        if action.reason is not None:
            lines.append(f"  reason: {action.reason}")
    else:
        assert_never(action)
    return lines


FOREIGN_WARNING: Final = "  ⚠ foreign repository: not this repo or one of its submodules"


class ForeignRepoRefused(Exception):  # noqa: N818 - name specified by the foreign-repo plan.
    """Pending actions target foreign repositories and nothing consented."""


def foreign_consent(
    batch: PreparedBatch, *, allowed: bool, ask: Callable[[str], bool] | None
) -> bool:
    """Whether publication may reach the batch's foreign repositories.

    True when no pending action is foreign or ``allowed`` (``--foreign``).
    Otherwise ``ask`` gets the question, and its answer decides. ``ask=None``
    (``-y``, or no terminal) raises ForeignRepoRefused.
    """
    slugs = batch.foreign_slugs
    if not slugs or allowed:
        return True
    names = ", ".join(slugs)
    if ask is None:
        raise ForeignRepoRefused(
            f"Refusing to publish to foreign repositories ({names}) without --foreign."
        )
    count = batch.foreign_pending
    noun = "action" if count == 1 else "actions"
    return ask(f"Publish {count} {noun} to foreign repositories {names}?")


def foreign_question(batch: PreparedBatch) -> str | None:
    """The single publish question when foreign repositories are involved, else None.

    An interactive run asks this instead of a plain publish question followed
    by foreign_consent's: one "yes" under the plan consents to both.
    """
    slugs = batch.foreign_slugs
    if not slugs:
        return None
    total = sum(a.status == "pending" for m in batch.manifests for a in m.actions)
    noun = "action" if total == 1 else "actions"
    names = ", ".join(slugs)
    count = batch.foreign_pending
    if count == total:
        return f"Publish {total} pending {noun} to foreign repositories {names}?"
    return f"Publish {total} pending {noun}, {count} of them to foreign repositories {names}?"


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
                if resolved.repo.foreign:
                    lines.append(FOREIGN_WARNING)
                previous = resolved.repo
            lines.extend(
                _action_lines(
                    prepared.manifest.name,
                    resolved.index,
                    resolved.action,
                    resolved.issue,
                    receipts.get(resolved.index),
                    resolved.labels,
                    batch.container,
                    batch.login,
                    resolved.project,
                )
            )
    return lines


def show_lines(manifest: IssueManifest, journal: IssueJournal | None, container: str) -> list[str]:
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
        lines.extend(_action_lines(manifest.name, index, action, issue, receipt, None, container))
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
    kind: Literal["validation", "execution"] = "execution"


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
        "GitHub Projects requires host authorization: gh auth refresh -s project",
        "project membership changed since staging",
        ASSIGN_NOT_APPLIED_DETAIL,
        UNASSIGN_NOT_APPLIED_DETAIL,
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


def _resolved_number(issue: ResolvedIssue, created: Mapping[str, int]) -> int:
    """The issue number `issue` targets: its own, or a ref created this run."""
    if issue.number is not None:
        return issue.number
    assert issue.ref is not None, "a resolved issue always carries a number or a create ref"
    return created[issue.ref]


def _execute_one(
    host_repo_root: Path, resolved: ResolvedAction, created: dict[str, int], login: str = ""
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
    if isinstance(action, AssigneesAction):
        add, remove = resolved_assignees(action, login)
        last: MutationReceipt | None = None
        if add:
            added = issue_github.add_assignees(host_repo_root, repo, number, logins=add)
            last = added
            present = {item.casefold() for item in added.assignees}
            missing = tuple(item for item in add if item.casefold() not in present)
            if missing:
                # GitHub answered 2xx but ignored logins it cannot assign. The removal is
                # not sent; any add that did land leaves the issue half-changed.
                raise IssueGithubMutationError(
                    ASSIGN_NOT_APPLIED_DETAIL,
                    uncertain=len(missing) < len(add),
                )
        if remove:
            try:
                removed = issue_github.remove_assignees(host_repo_root, repo, number, logins=remove)
            except IssueGithubMutationError as exc:
                if last is None:
                    raise
                # The additions already landed, so a definite rejection of the
                # removal still leaves the issue half-changed.
                raise IssueGithubMutationError(
                    "GitHub mutation outcome is uncertain", uncertain=True
                ) from exc
            last = removed
            present = {item.casefold() for item in removed.assignees}
            still = tuple(item for item in remove if item.casefold() in present)
            if still:
                raise IssueGithubMutationError(
                    UNASSIGN_NOT_APPLIED_DETAIL,
                    uncertain=bool(add) or len(still) < len(remove),
                )
        assert last is not None, "a parsed assignees action changes at least one login"
        return last
    if isinstance(action, ProjectAddAction):
        try:
            project = resolved.project or issue_projects.resolve_project(
                host_repo_root, action.project
            )
            target = issue_projects.get_target(host_repo_root, project, repo, number)
        except IssueGithubReadError as exc:
            raise IssueGithubMutationError(str(exc), uncertain=False) from exc
        if target.present != action.expected_present:
            raise IssueGithubMutationError(
                "project membership changed since staging", uncertain=False
            )
        return issue_projects.add_item(host_repo_root, project, target, number)
    if isinstance(action, StateAction):
        return issue_github.set_state(
            host_repo_root, repo, number, state=action.state, reason=action.reason
        )
    assert_never(action)


def pending_action_count(batch: PreparedBatch) -> int:
    """Count pending actions in the prepared batch."""
    return sum(a.status == "pending" for m in batch.manifests for a in m.actions)


def _sent_action(
    manifest: str,
    digest: str,
    resolved: ResolvedAction,
    *,
    target: int | None,
    url: str | None,
    login: str = "",
) -> sent.SentAction:
    """The Sent-log entry of one dispatched issue action, from the prepared snapshot."""
    action = resolved.action
    title: str | None = None
    if isinstance(action, CreateAction):
        kind, title = "create", action.title
        content = action.title + (f"\n\n{action.body}" if action.body else "")
        if resolved.labels:
            content += f"\n\nlabels: {', '.join(resolved.labels)}"
    elif isinstance(action, EditAction):
        kind, title = "edit", action.title
        content = "\n\n".join(p for p in (action.title, action.body) if p)
    elif isinstance(action, CommentAction):
        kind, content = "comment", action.body
    elif isinstance(action, LabelsAction):
        kind = "labels"
        changes = [f"+{label}" for label in action.add]
        changes += [f"\u2212{label}" for label in action.remove]
        content = "labels " + " ".join(changes)
    elif isinstance(action, AssigneesAction):
        kind = "assignees"
        content = _assignee_changes(action, login)
    elif isinstance(action, ProjectAddAction):
        kind = action.kind
        title = resolved.project.title if resolved.project else None
        content = (
            f'project {action.project} "{title or ""}" · add; '
            f"membership {action.expected_present} -> True"
        )
    elif isinstance(action, StateAction):
        kind = "state"
        content = f"state {action.state}" + (f" ({action.reason})" if action.reason else "")
    else:
        assert_never(action)
    return sent.SentAction(
        outbox="issue",
        manifest=manifest,
        index=resolved.index,
        kind=kind,
        repo=resolved.repo.slug,
        target=target,
        url=url,
        title=title,
        content=content,
        digest=digest,
    )


def apply_batch(
    batch: PreparedBatch,
    *,
    incus: Incus,
    publications: PublicationStore | None = None,
    before_action: Callable[[], None] | None = None,
    container_prefix: str,
    publisher: sent.Publisher,
    clock: Callable[[], float] = time.time,
) -> ApplyReport:
    """Apply every pending action of `batch`, in the exact order it was shown.

    Call `before_action` immediately before each attempted action, if provided.

    Proposals, journals and sent records live in the host outbox store. The
    publication claim of every selected manifest is taken first (single flight:
    another live run publishing one of them refuses the whole batch) and held
    to the end. Two passes. The first re-verifies every manifest — the
    instance's identity and uuid, each manifest's exact stored text (by
    recomputed digest), and its journal's recorded progress — *before* a single
    GitHub mutation is dispatched, so a change discovered in the last manifest
    still aborts the first one's untouched. A recorded `uncertain` action (a
    leftover `prepared` reads as one) aborts the whole batch, and so does an
    already-`applied` record whose repo no longer matches the resolved one.

    The second pass walks each manifest's actions in display order, skipping
    ones already `applied` (restoring a create's issue number for any later
    `issue_ref`). For a pending action it commits `prepared`, dispatches the
    exact mutation, then commits the outcome together with its sent record —
    and, when that outcome completes the manifest, the proposal's removal and
    the journal's archiving — in one transaction. A crash between the two
    commits leaves `prepared`, read back as `uncertain`. The run stops at the
    first action it cannot durably resolve one way or the other.
    """
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db
    from jailbee.outbox.service import instance_identity

    store = publications if publications is not None else publication_db.open_publications()
    applied: list[tuple[str, JournalAction]] = []
    skipped: list[tuple[str, JournalAction]] = []
    cleaned: list[str] = []

    def failed(
        manifest: str | None,
        index: int | None,
        uncertain: bool,
        detail: str,
        *,
        kind: Literal["validation", "execution"] = "execution",
    ) -> ApplyReport:
        return ApplyReport(
            tuple(applied),
            tuple(skipped),
            tuple(cleaned),
            ApplyFailure(manifest, index, uncertain, detail, kind),
        )

    with ExitStack() as held:
        claims: dict[str, publication_db.Claim] = {}
        try:
            for name in sorted({prepared.manifest.name for prepared in batch.manifests}):
                claims[name] = held.enter_context(store.claim(batch.uuid, "issue", name))
        except publication_db.ClaimBusyError as exc:
            return failed(None, None, False, str(exc), kind="validation")
        except (JournalError, StoreError) as exc:
            return failed(None, None, False, str(exc))

        try:
            fresh_identity, fresh_uuid = instance_identity(incus, batch.container)
        except (JournalError, IncusError) as exc:
            return failed(None, None, False, f"could not verify the container's identity: {exc}")
        if fresh_identity != batch.identity or fresh_uuid != batch.uuid:
            return failed(
                None,
                None,
                False,
                "the container's identity changed since this batch was approved; re-approve it",
                kind="validation",
            )

        try:
            outbox = store_outbox(batch.uuid)
        except StoreError as exc:
            return failed(None, None, False, f"outbox store is unreadable: {exc}")

        journals: dict[str, IssueJournal] = {}
        for prepared in batch.manifests:
            name = prepared.manifest.name
            text = outbox.files.get(name)
            if text is None:
                return failed(
                    name,
                    None,
                    False,
                    f"{name}: manifest is no longer in the outbox",
                    kind="validation",
                )
            bodies = {body: outbox.files.get(body, "") for body in prepared.manifest.body_files}
            digest = proposal_digest(name, text, bodies)
            if digest != prepared.digest:
                return failed(
                    name,
                    None,
                    False,
                    f"{name}: proposal changed since it was approved",
                    kind="validation",
                )
            try:
                stored = store.begin(
                    claims[name],
                    digest=digest,
                    action_count=len(prepared.manifest.actions),
                    container_full=batch.container,
                    now=clock(),
                )
            except (JournalError, StoreError) as exc:
                return failed(name, None, False, str(exc))
            journal = _issue_journal(batch.identity, stored)
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
                        kind="validation",
                    )
                if current is not None and current.repo.casefold() != resolved.repo.identity:
                    return failed(
                        name,
                        resolved.index,
                        False,
                        f"{name} action {resolved.index}: its recorded repo no longer matches "
                        "the resolved repo; re-approve this batch",
                        kind="validation",
                    )
            journals[name] = journal

        for prepared in batch.manifests:
            name = prepared.manifest.name
            claim = claims[name]
            digest = prepared.digest
            receipts = {a.index: a for a in journals[name].actions}
            created_issue_numbers: dict[str, int] = {}
            finalized = changed = False
            for resolved in prepared.actions:
                current = receipts.get(resolved.index)
                if current is not None:
                    skipped.append((name, current))
                    if isinstance(resolved.action, CreateAction) and current.issue is not None:
                        created_issue_numbers[resolved.action.ref] = current.issue
                    continue

                try:
                    store.mark_prepared(
                        claim, digest, resolved.index, repo=resolved.repo.slug, now=clock()
                    )
                except (JournalError, StoreError) as exc:
                    return failed(name, resolved.index, False, str(exc))
                if before_action is not None:
                    try:
                        before_action()
                    except BaseException as exc:
                        try:
                            store.clear_prepared(claim, digest, resolved.index)
                        except (JournalError, StoreError) as cleanup_error:
                            exc.add_note(
                                f"Could not clear prepared journal marker: {cleanup_error}"
                            )
                        raise

                try:
                    receipt = _execute_one(
                        batch.host_repo_root, resolved, created_issue_numbers, batch.login
                    )
                except IssueGithubMutationError as exc:
                    recorded = True
                    try:
                        if exc.uncertain:
                            target = resolved.issue.number or created_issue_numbers.get(
                                resolved.issue.ref or ""
                            )
                            store.settle(
                                claim,
                                digest,
                                resolved.index,
                                outcome="uncertain",
                                detail=str(exc),
                                sent=_sent_entry(
                                    batch, name, resolved, target, None, container_prefix, publisher
                                ),
                                now=clock(),
                            )
                        else:
                            store.clear_prepared(claim, digest, resolved.index)
                    except (JournalError, StoreError):
                        recorded = False
                    return failed(
                        name,
                        resolved.index,
                        exc.uncertain or not recorded,
                        _apply_failure_detail(str(exc), uncertain=exc.uncertain),
                    )
                try:
                    settled = store.settle(
                        claim,
                        digest,
                        resolved.index,
                        outcome="applied",
                        url=receipt.url,
                        issue=receipt.issue,
                        sent=_sent_entry(
                            batch,
                            name,
                            resolved,
                            receipt.issue,
                            receipt.url,
                            container_prefix,
                            publisher,
                        ),
                        now=clock(),
                    )
                except (JournalError, StoreError) as exc:
                    return failed(
                        name,
                        resolved.index,
                        True,
                        f"GitHub mutation succeeded but its receipt could not be recorded "
                        f"({exc}); a re-run will treat it as uncertain",
                    )
                finalized, changed = settled.finalized, settled.changed
                applied_action = next(
                    a for a in settled.journal.actions if a.index == resolved.index
                )
                applied.append((name, applied_action))
                if isinstance(resolved.action, CreateAction):
                    created_issue_numbers[resolved.action.ref] = receipt.issue

            if not finalized and not changed:
                # Every action was applied by an earlier run: only the removal is left.
                try:
                    settled = store.finalize(claim, digest, now=clock())
                except (JournalError, StoreError) as exc:
                    return failed(
                        name, None, False, f"{name}: applied, but it could not be removed ({exc})"
                    )
                finalized, changed = settled.finalized, settled.changed
            if not finalized:
                return failed(
                    name,
                    None,
                    False,
                    f"{name}: proposal changed after publication; cleanup refused, "
                    "journal retained",
                )
            cleaned.append(name)

        return ApplyReport(tuple(applied), tuple(skipped), tuple(cleaned), None)


def _sent_entry(
    batch: PreparedBatch,
    name: str,
    resolved: ResolvedAction,
    target: int | None,
    url: str | None,
    container_prefix: str,
    publisher: sent.Publisher,
) -> publication_db.SentEntry | None:
    """The sent record of one dispatched action, from the prepared snapshot.

    None when it cannot be built: the outcome is recorded without it rather
    than lost after GitHub already acted.
    """
    prepared = next(m for m in batch.manifests if m.manifest.name == name)
    try:
        action = _sent_action(
            name, prepared.digest, resolved, target=target, url=url, login=batch.login
        )
    except Exception as exc:
        _log.warning(
            "could not build the sent record of %s action %d: %s", name, resolved.index, exc
        )
        return None
    return sent.entry(
        action,
        instance=batch.container,
        uuid=batch.uuid,
        container_prefix=container_prefix,
        publisher=publisher,
    )


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


def _validated_receipt(url: str, repo: RepoTarget, *, project: bool = False) -> int:
    """Parse and validate a GitHub issue receipt URL; return its issue number.

    Deliberately strict: exactly `https://github.com/<owner>/<repo>/issues/<n>`,
    optionally followed by a `#fragment` (a comment permalink), matching
    `repo` case-insensitively. No query string, no `pull`, no credentials in
    the authority, no host lookalike, no trailing garbage.
    """
    pattern = (
        re.compile(_RECEIPT_URL_RE.pattern.replace("/issues/", "/(?:issues|pull)/"))
        if project
        else _RECEIPT_URL_RE
    )
    match = pattern.fullmatch(url)
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
    uuid: str,
    index: int,
    resolution: Resolution,
    repo: RepoTarget,
    manifest: IssueManifest,
    digest: str,
    publications: PublicationStore | None = None,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Apply a human's explicit resolution to one uncertain action.

    `manifest` and `digest` pin this call to the exact proposal the human is
    looking at; the stored journal must match both. Only an action currently
    `uncertain` (a leftover `prepared` reads as one; never `pending` or
    `applied`) may be resolved, and `repo` must match what was journaled for it.
    The proposal's publication claim is held throughout, so an in-flight
    publication is never resolved under it.

    `RetryResolution` forgets the uncertain record, leaving the action to be
    re-attempted as `pending`. `AppliedResolution` requires a
    `https://github.com/<repo>/issues/<n>` receipt matching `repo`; for a
    create, `issue` must be supplied and agree with that URL's number; for
    every other action kind, `issue` is forbidden and, when the action's own
    target issue is already known, the URL's number must agree with it too. The
    journal entry, the sent record and — when this completes the proposal — the
    proposal's removal are written in one transaction. Returns whether the
    proposal was removed.
    """
    from jailbee.outbox import publication_db

    if not 0 <= index < len(manifest.actions):
        raise JournalError("reconciliation index is out of bounds for this manifest")
    store = publications if publications is not None else publication_db.open_publications()
    with store.claim(uuid, "issue", manifest.name) as claim:
        stored = store.load(uuid, "issue", manifest.name)
        if stored is None:
            raise JournalError(f"no journal exists for {manifest.name}")
        if stored.digest != digest:
            raise JournalError("journal digest does not match this proposal")
        if stored.action_count != len(manifest.actions):
            raise JournalError("journal action count does not match this proposal")
        current = next((a for a in stored.actions if a.index == index), None)
        if current is None or current.state != "uncertain":
            raise JournalError("only an uncertain action can be reconciled")
        if current.repo.casefold() != repo.identity:
            raise JournalError("journal action repo does not match the resolved repo")

        if isinstance(resolution, RetryResolution):
            store.resolve(claim, digest, index, applied=None, sent_outbox="issue", now=clock())
            return False

        action = manifest.actions[index]
        url_number = _validated_receipt(
            resolution.url, repo, project=isinstance(action, ProjectAddAction)
        )
        if isinstance(action, CreateAction):
            if type(resolution.issue) is not int:
                raise JournalError("a create's resolution requires its GitHub issue number")
            if resolution.issue != url_number:
                raise JournalError("resolution issue number does not match the receipt URL")
        else:
            if resolution.issue is not None:
                raise JournalError("only a create's resolution may carry an issue number")
            journal = IssueJournal(
                ContainerIdentity(stored.container_full, ""),
                stored.name,
                stored.digest,
                stored.action_count,
                stored.actions,
            )
            expected = _expected_issue_number(action, manifest, journal)
            if expected is not None and expected != url_number:
                raise JournalError("receipt URL does not match this action's issue")

        settled = store.resolve(
            claim,
            digest,
            index,
            applied=(resolution.url, resolution.issue),
            sent_outbox="issue",
            sent_target=url_number,
            now=clock(),
        )
        return settled.finalized
