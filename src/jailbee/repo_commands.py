"""Presentation and orchestration for repository management commands."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.repo_remove import RemovalPlan

from rich.table import Table
from sqlmodel import Session

from jailbee.db import get_engine
from jailbee.incus import Incus
from jailbee.remote_ssh.repo_scope import scope_for_session
from jailbee.repo_remove import list_repos
from jailbee.tui import console


def list_command(fmt: Literal["table", "json"]) -> None:
    entries = list_repos(Incus(), lambda: Session(get_engine()), scope=scope_for_session())
    rows = [
        {
            "prefix": e.prefix,
            "path": str(e.repo_root) if e.repo_root else None,
            "state": e.state,
            "containers": e.containers,
            "base": e.base,
        }
        for e in entries
    ]
    if fmt == "json":
        print(json.dumps(rows))
        return
    table = Table()
    for name in ("PREFIX", "PATH", "STATE", "CONTAINERS", "BASE"):
        table.add_column(name)
    for row in rows:
        table.add_row(
            str(row["prefix"]),
            str(row["path"] or "-"),
            str(row["state"]),
            str(row["containers"]),
            str(row["base"] or "-"),
        )
    console.print(table)


def _print_plan(plan: RemovalPlan) -> None:
    typer.echo(f"Repository: {plan.prefix}")
    for label, values in (
        ("Containers", plan.containers),
        ("Profiles", plan.profiles),
        ("ACLs", plan.acls),
        ("Golden images", plan.golden_images),
        ("Golden instances", plan.golden_instances),
        ("Purge files", plan.purge_files),
        ("Kept files (use --purge for defaults)", plan.kept_files),
        ("Global references (kept)", plan.global_refs),
    ):
        typer.echo(f"{label}: {', '.join(map(str, values)) or '-'}")
    if plan.golden_kept_reason:
        typer.echo(f"Golden kept: {plan.golden_kept_reason}")
    if plan.shared_kept_reason:
        typer.echo(f"Shared files kept: {plan.shared_kept_reason}")
    if plan.cfg is None:
        typer.echo("Warning: repository config unavailable; container git state was not checked.")
    typer.echo(
        "State rows: " + (", ".join(f"{k}={v}" for k, v in plan.state_counts.items()) or "-")
    )


def remove_command(
    target: str | None,
    *,
    yes: bool,
    dry_run: bool,
    force: bool,
    purge: bool,
    running_agents: Callable[[Config, Incus, list[str]], list[str]],
    warn_before_destroy: Callable[..., bool],
) -> None:
    from jailbee import prompting, repo_remove
    from jailbee.config import ConfigError, load_repo_config
    from jailbee.lifecycle import destroy_container, list_containers
    from jailbee.orphans import destroy_orphan

    incus = Incus()

    def sessions() -> Session:
        return Session(get_engine())

    scope = scope_for_session()
    entries = None
    path_cfg = None
    if target and ("/" in target or target.startswith((".", "~"))):
        target_path = Path(target).expanduser()
        if not target_path.is_dir():
            raise typer.BadParameter("Repository directory does not exist")
        root = target_path.resolve()
        entries = list_repos(incus, sessions, scope=scope)
        row = next((e for e in entries if e.repo_root and e.repo_root.resolve() == root), None)
        if row is not None:
            target = row.prefix
        else:
            try:
                path_cfg = load_repo_config(root)
            except (ConfigError, OSError, ValueError) as exc:
                raise typer.BadParameter(str(exc)) from exc
            target = path_cfg.container_prefix
            row = next((e for e in entries if e.prefix == target), None)
            if row and row.repo_root and row.repo_root.resolve() != root:
                raise typer.BadParameter("Directory does not match the registered repository")
    if target is None:
        entries = list_repos(incus, sessions, scope=scope)
        target = prompting.choose_one(
            "repository",
            [prompting.Option(e.prefix, e.prefix, e.prefix) for e in entries],
            destructive=True,
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", target) or not scope.allows(target):
        raise typer.BadParameter("Invalid or unavailable repository prefix")
    if path_cfg is None:
        plan = repo_remove.plan_removal(target, incus, sessions, purge=purge)
    else:
        plan = repo_remove.plan_removal(target, incus, sessions, purge=purge, cfg=path_cfg)
    _print_plan(plan)
    if dry_run:
        return
    if not yes and not prompting.is_interactive():
        typer.echo("Confirmation requires a terminal or --yes", err=True)
        raise typer.Exit(2)
    if not force:
        if plan.cfg is not None:
            running = running_agents(plan.cfg, incus, plan.containers)
        else:
            from jailbee.accounts import groups
            from jailbee.accounts.adapters import base

            # Load every supported pooled adapter even when no repo config survives.
            base.get_adapter("claude")
            running = sorted(
                f"{name}: {container}"
                for name in base.ADAPTERS
                for container in plan.containers
                if groups.agent_running(incus, container, command=name) is True
            )
        if running:
            typer.echo(
                "Agents running: " + ", ".join(running) + "; use --force to override", err=True
            )
            raise typer.Exit(2)
    question = f"Remove repository {target}?"
    if not force and plan.cfg is not None:
        infos = [
            i
            for i in list_containers(plan.cfg, incus, with_git_status=True)
            if i.name in plan.containers
        ]
        if yes:
            # Print risks without prompting; explicit --yes has already given consent.
            from jailbee.destroy_guard import assess, status_is_unknown
            from jailbee.tui import warn

            for info in infos:
                if status_is_unknown(info):
                    warn(f"Git status unknown: {info.display_name}")
                summary = assess(plan.cfg, info)
                if summary:
                    warn(summary.line)
        elif not warn_before_destroy(plan.cfg, infos, question=question):
            raise typer.Exit(1)
    elif not yes and not prompting.ask_confirm(
        question, default=False, destructive=True, alternative="--yes"
    ):
        raise typer.Exit(1)
    results = repo_remove.execute_removal(
        plan,
        incus,
        sessions,
        force=force,
        destroy_container=destroy_container,
        destroy_orphan=destroy_orphan,
    )
    for result in results:
        typer.echo(f"{'OK' if result.ok else 'FAILED'} {result.label}: {result.detail}")
    if any(not result.ok for result in results):
        raise typer.Exit(1)
