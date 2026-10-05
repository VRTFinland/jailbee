"""Shared discovery, target ownership and UI callback boundaries for outboxes."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from jailbee import config as config_api
from jailbee.config import ConfigError
from jailbee.incus import IncusError
from jailbee.lifecycle import list_containers
from jailbee.outbox.delete import DeletePlan, DeleteSelection, plan_delete
from jailbee.outbox.inspect import detail_json, overview_json, safe_text
from jailbee.outbox.io import READ_TIMEOUT
from jailbee.outbox.markdown_view import print_lines
from jailbee.outbox.models import (
    ContainerView,
    OutboxChanged,
    OutboxError,
    OutboxExecutionError,
    ProposalId,
    ProposalView,
)
from jailbee.outbox.publish import PublishOptions, publish_selected
from jailbee.outbox.service import execute_delete, load_container
from jailbee.remote_ssh import repo_scope

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.lifecycle import ContainerInfo
    from jailbee.outbox_io import JournalStore
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope


def _repo_roots(cfg: Config, scope: RemoteRepoScope) -> dict[str, Path]:
    # Keep missing registered roots: unlike the dashboard, inspection must report
    # their containers as unavailable rather than silently omitting the repository.
    import sqlite3
    from pathlib import Path

    from jailbee.db import state_dir

    roots: dict[str, Path] = {}
    database = state_dir() / "state.sqlite"
    if database.is_file():
        # mode=ro also closes the exists/connect race without creating a new DB.
        # Never bootstrap or migrate runtime state for an inspection command.
        try:
            connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT container_prefix, repo_root FROM registered_repo"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise OutboxExecutionError(f"repository registry is unreadable: {exc}") from exc
        roots = {prefix: Path(root) for prefix, root in rows if scope.allows(prefix)}
    if scope.allows(cfg.container_prefix):
        roots[cfg.container_prefix] = cfg.repo_root
    return roots


def _own_config(prefix: str | None, roots: dict[str, Path]) -> Config:
    if prefix is None or prefix not in roots:
        raise OutboxError("target repository is not registered; config is unavailable")
    root = roots[prefix]
    if not root.is_dir():
        raise OutboxError(f"repository root is unavailable: {root}")
    try:
        cfg = config_api.load_repo_config(root)
    except (ConfigError, OSError) as exc:
        raise OutboxError(f"{prefix}: config is unavailable: {exc}") from exc
    if cfg.container_prefix != prefix or cfg.repo_root.resolve() != root.resolve():
        raise OutboxError(f"{prefix}: repository config identity changed; refresh required")
    return cfg


def _inventory(
    cfg: Config, incus: Incus, *, all_repos: bool
) -> tuple[list[ContainerInfo], dict[str, Path]]:
    scope = repo_scope.scope_for_session()
    try:
        containers = list_containers(
            cfg, incus, all_repos=all_repos, scope=scope, timeout=READ_TIMEOUT
        )
    except IncusError as exc:
        raise OutboxExecutionError(str(exc)) from exc
    return containers, _repo_roots(cfg, scope)


def _running(item: ContainerInfo) -> None:
    if item.state != "Running":
        raise OutboxError(f"{item.name}: container is {item.state.lower()}; outbox is unavailable")


def resolve_target(cfg: Config, incus: Incus, name: str) -> tuple[Config, str]:
    """Resolve public full/short names and fresh target-owned config before I/O.

    Browser/Qt callbacks must call this boundary again, not retain a Config in
    an immutable view. A missing config file can be a valid synthesized config;
    a missing root, failed load or changed prefix cannot fall back to the caller.
    """
    containers, roots = _inventory(cfg, incus, all_repos=True)
    item = _resolve_visible(cfg, name, containers)
    target = _own_config(item.repo, roots)
    _running(item)
    return target, item.name


def _resolve_visible(cfg: Config, name: str, containers: Sequence[ContainerInfo]) -> ContainerInfo:
    # Only the scoped inventory can supply candidates; exact full names win.
    for candidate in (name, f"{cfg.container_prefix}-{name}"):
        item = next((c for c in containers if c.name == candidate), None)
        if item is not None:
            return item
    raise OutboxError(f"no such container in the visible repository scope: {name}")


def discover(
    cfg: Config, incus: Incus, name: str | None, *, all_repos: bool, journal_store: JournalStore
) -> tuple[ContainerView, ...]:
    """Inspect all visible candidates, regardless of advisory probe counts."""
    if name is not None and all_repos:
        raise OutboxError("pass a container or --all-repos, not both")
    containers, roots = _inventory(cfg, incus, all_repos=all_repos or name is not None)
    if name is not None:
        containers = [_resolve_visible(cfg, name, containers)]
    result = []
    for item in containers:
        try:
            target = _own_config(item.repo, roots)
            _running(item)
        except OutboxError as exc:
            result.append(ContainerView(None, item.name, False, str(exc), (), ()))
            continue
        result.append(load_container(target, incus, item.name, journal_store=journal_store))
    return tuple(result)


def _selected(
    cfg: Config,
    incus: Incus,
    name: str,
    proposal: ProposalId,
    *,
    journal_store: JournalStore,
    revision: str | None = None,
) -> tuple[Config, ContainerView, ProposalView]:
    target, full = resolve_target(cfg, incus, name)
    container = load_container(target, incus, full, journal_store=journal_store, raise_errors=True)
    if not container.available:
        raise OutboxError(container.error or "container is unavailable")
    view = next((p for p in container.proposals if p.id == proposal), None)
    if view is None:
        raise OutboxError(f"{proposal}: proposal is not in the inspected outbox")
    if revision is not None and revision != view.revision:
        raise OutboxChanged("proposal changed; refresh required")
    return target, container, view


def _recheck_target(cfg: Config, incus: Incus, full: str, target: Config) -> None:
    fresh, resolved = resolve_target(cfg, incus, full)
    if resolved != full or fresh != target:
        raise OutboxChanged("target config changed; refresh required")


def drop_selected(
    cfg: Config,
    incus: Incus,
    name: str,
    proposal: ProposalId,
    *,
    selection: DeleteSelection,
    journal_store: JournalStore,
    confirm: Callable[[DeletePlan], bool],
    expected_revision: str | None = None,
) -> int:
    target, container, _ = _selected(
        cfg, incus, name, proposal, journal_store=journal_store, revision=expected_revision
    )
    plan = plan_delete(container, proposal, selection)
    if not confirm(plan):
        return 0
    _recheck_target(cfg, incus, container.name, target)
    execute_delete(target, incus, container.name, plan, journal_store=journal_store)
    return 0


def apply_selected(
    cfg: Config,
    incus: Incus,
    name: str,
    proposal: ProposalId,
    *,
    options: PublishOptions,
    journal_store: JournalStore,
    confirm: Callable[[int], bool],
    expected_revision: str | None = None,
) -> int:
    if proposal.kind == "issue" and (options.force or options.foreign):
        raise OutboxError("force and foreign are only valid for PR publication")
    target, container, view = _selected(
        cfg, incus, name, proposal, journal_store=journal_store, revision=expected_revision
    )
    if view.error:
        raise OutboxError(f"{proposal}: {view.error}")

    def checked_confirm(total: int) -> bool:
        accepted = confirm(total)
        if accepted:
            _recheck_target(cfg, incus, container.name, target)
        return accepted

    _recheck_target(cfg, incus, container.name, target)
    return publish_selected(
        target,
        incus,
        container.name,
        proposal,
        journal_store=journal_store,
        options=options,
        confirm=checked_confirm,
        expected_revision=view.revision,
        raise_errors=True,
    )


def show_overview(
    cfg: Config,
    incus: Incus,
    name: str | None,
    *,
    all_repos: bool,
    output: str,
    journal_store: JournalStore,
) -> int:
    containers = discover(cfg, incus, name, all_repos=all_repos, journal_store=journal_store)
    if output == "json":
        print_lines((json.dumps(overview_json(containers), ensure_ascii=True),))
    else:
        from rich.table import Table
        from rich.text import Text

        from jailbee.tui import console

        table = Table("Container", "Proposal", "State", "Actions / Note")
        for container in containers:
            if not container.available or not container.proposals:
                table.add_row(
                    Text(safe_text(container.name)),
                    "",
                    "unavailable" if not container.available else "empty",
                    Text(safe_text(container.error or "No proposals")),
                )
            for proposal in container.proposals:
                table.add_row(
                    Text(safe_text(container.name)),
                    Text(str(proposal.id)),
                    proposal.state,
                    Text(safe_text(proposal.error or str(len(proposal.actions)))),
                )
            for snapshot in container.stores:
                for warning in snapshot.warnings:
                    table.add_row(
                        Text(safe_text(container.name)),
                        snapshot.kind,
                        "warning",
                        Text(safe_text(warning)),
                    )
                for rejected in snapshot.rejected:
                    table.add_row(
                        Text(safe_text(container.name)),
                        snapshot.kind,
                        "rejected",
                        Text(safe_text(rejected)),
                    )
        console.print(table)
    return 2 if any(not c.available for c in containers) else 0


def show_selected(
    cfg: Config,
    incus: Incus,
    name: str,
    proposal: ProposalId,
    *,
    output: str,
    journal_store: JournalStore,
) -> int:
    _, container, view = _selected(cfg, incus, name, proposal, journal_store=journal_store)
    if output == "json":
        print_lines((json.dumps(detail_json(container, view), ensure_ascii=True),))
    else:
        print_lines((f"{container.name}: {proposal} ({view.state})", f"Revision: {view.revision}"))
        if view.error:
            print_lines((view.error,))
        if view.edit_block:
            print_lines((view.edit_block,))
        for action in view.actions:
            print_lines(
                (
                    f"Action {action.index}: {action.kind} {action.repo} "
                    f"{action.target} [{action.state}]",
                    action.text,
                )
            )
            if action.receipt:
                print_lines((f"Receipt: {action.receipt}",))
            for comment in action.comments:
                print_lines((f"Comment {comment.index}: {comment.label}", comment.text))
        print_lines(("Raw manifest:", view.raw_text))
    return 0
