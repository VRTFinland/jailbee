"""Discover registered repositories and their remaining Incus resources."""

from __future__ import annotations

import shutil
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from sqlmodel import select

from jailbee.config import SCRATCH_BASE_ALIAS, load_repo_config
from jailbee.config.errors import ConfigError
from jailbee.config.loader import _default_shared_dir
from jailbee.config.local_layer import local_config_path
from jailbee.db.models import (
    BackgroundJob,
    DismissedNotice,
    EgressOverride,
    PoolIP,
    RefreshState,
    RegisteredRepo,
    RepoUpgradeState,
    SentRecord,
)
from jailbee.egress_scope import (
    ACL_NAME_MAX,
    _capped_acl_name,
    bridge_extras_acl_name,
    extra_acl_name,
)
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.golden import find_all_archived_images
from jailbee.maintenance import humanize
from jailbee.paths import repo_config_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlmodel import Session

    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope


@dataclass(frozen=True)
class RepoEntry:
    prefix: str
    repo_root: Path | None
    state: str
    containers: int
    base: str | None


def list_repos(
    incus: Incus, session_factory: Callable[[], Session], *, scope: RemoteRepoScope | None = None
) -> list[RepoEntry]:
    with session_factory() as session:
        rows = session.exec(select(RegisteredRepo)).all()
    registered = {row.container_prefix: row for row in rows}
    prefixes = set(registered)
    prefixes.update(
        profile[:-5]
        for profile in incus.profile_list()
        if profile.endswith("-base") and profile != "default" and profile[:-5]
    )
    instances = incus.list_containers(fast=True)
    counts: dict[str, int] = {}
    names = {raw.get("name") for raw in instances}
    for raw in instances:
        for profile in raw.get("profiles") or []:
            if profile.endswith("-base") and profile != "default":
                prefix = profile[:-5]
                counts[prefix] = counts.get(prefix, 0) + 1
                break
    sizes = {
        alias.get("name"): int(image.get("size", 0) or 0)
        for image in incus.list_images()
        for alias in image.get("aliases", [])
    }
    result = []
    for prefix in sorted(prefixes):
        if scope is not None and not scope.allows(prefix):
            continue
        row = registered.get(prefix)
        root = Path(row.repo_root) if row else None
        state = "leftover"
        alias = f"{prefix}-base"
        if row is not None and root is not None:
            if not root.is_dir():
                state = "dir missing"
            else:
                state = "no config"
                if row.synthetic_config or repo_config_path(root) is not None:
                    try:
                        cfg = load_repo_config(root)
                        if cfg.container_prefix == prefix:
                            alias = cfg.golden.alias
                            state = "ok"
                    except (ConfigError, OSError, ValueError):
                        pass
        base = humanize(sizes[alias]) if alias in sizes else None
        if base is None and f"{alias}--golden" in names:
            base = "instance"
        result.append(RepoEntry(prefix, root, state, counts.get(prefix, 0), base))
    return result


@dataclass(frozen=True)
class RemovalPlan:
    prefix: str
    cfg: Config | None
    containers: list[str]
    profiles: list[str]
    acls: list[str]
    golden_images: list[str]
    golden_instances: list[str]
    golden_kept_reason: str | None
    state_counts: dict[str, int]
    purge_files: list[Path]
    kept_files: list[Path]
    global_refs: list[str]
    shared_kept_reason: str | None = None


@dataclass(frozen=True)
class StepResult:
    label: str
    ok: bool
    detail: str


def execute_removal(
    plan: RemovalPlan,
    incus: Incus,
    session_factory: Callable[[], Session],
    *,
    force: bool = False,
    destroy_container: Callable[..., None] | None = None,
    destroy_orphan: Callable[..., None] | None = None,
) -> list[StepResult]:
    """Remove independent resources best-effort, retaining registration on failure."""
    from jailbee.egress_proxy import ProxyStatus, drop_fragment, proxy_status
    from jailbee.init_command import BRIDGE_NETWORK, detach_acl_from_bridge
    from jailbee.network_generation import WORK_BRIDGE
    from jailbee.work_network import work_network_lock

    if destroy_container is None:
        from jailbee.lifecycle import destroy_container
    if destroy_orphan is None:
        from jailbee.orphans import destroy_orphan

    results: list[StepResult] = []

    def step(label: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as exc:
            results.append(StepResult(label, False, str(exc)))
        else:
            results.append(StepResult(label, True, "removed or already absent"))

    known = {plan.prefix}
    ownership = {name: plan.prefix for name in plan.containers}

    def inventory() -> None:
        # Capture ownership before deleting its profiles and instances.
        with session_factory() as session:
            known.update(row.container_prefix for row in session.exec(select(RegisteredRepo)).all())
        known.update(p[:-5] for p in incus.profile_list() if p.endswith("-base") and p[:-5])
        for raw in incus.list_containers(fast=True):
            for profile in raw.get("profiles") or []:
                if profile.endswith("-base"):
                    ownership[raw["name"]] = profile[:-5]
                    break

    step("ownership", inventory)
    inventory_ok = results[-1].ok
    for name in plan.containers:

        def remove_container(name: str = name) -> None:
            if incus.exists(name):
                if plan.cfg is not None:
                    assert destroy_container is not None
                    destroy_container(plan.cfg, incus, name, force=force)
                else:
                    assert destroy_orphan is not None
                    destroy_orphan(incus, name, force=force)

        step(f"container {name}", remove_container)
    for name in plan.profiles:

        def remove_profile(name: str = name) -> None:
            if incus.profile_exists(name):
                incus.profile_delete(name)

        step(f"profile {name}", remove_profile)
    for name in plan.acls:

        def remove_acl(name: str = name) -> None:
            if not incus.network_acl_exists(name):
                return
            for reference in incus.network_acl_used_by(name):
                parts = urlsplit(reference).path.split("/")
                if len(parts) != 4 or parts[1:3] != ["1.0", "networks"] or not parts[3]:
                    raise ValueError(f"ACL {name} is still referenced by {reference}")
                network = unquote(parts[3])
                if network == BRIDGE_NETWORK:
                    detach_acl_from_bridge(incus, name)
                    continue
                with work_network_lock() if network == WORK_BRIDGE else nullcontext():
                    current = incus.network_get(network, "security.acls")
                    remaining = [
                        acl.strip()
                        for acl in current.split(",")
                        if acl.strip() and acl.strip() != name
                    ]
                    desired = ",".join(remaining)
                    if desired != current:
                        incus.network_set(network, "security.acls", desired)
            incus.network_acl_delete(name)

        step(f"acl {name}", remove_acl)
    for name in plan.golden_images:

        def remove_image(name: str = name) -> None:
            if incus.image_exists(name):
                incus.image_delete(name)

        step(f"image {name}", remove_image)
    for name in plan.golden_instances:

        def remove_golden(name: str = name) -> None:
            if incus.exists(name):
                from jailbee.stopping import stop_container

                if any(
                    raw.get("name") == name and raw.get("status") == "Running"
                    for raw in incus.list_containers(fast=True)
                ):
                    stop_container(
                        incus, name, force=force, force_fallback=True, show_progress=False
                    )
                incus.delete(name, force=force)

        step(f"golden instance {name}", remove_golden)
    def remove_proxy_fragment() -> None:
        if not drop_fragment(incus, plan.prefix) and proxy_status(incus) != ProxyStatus.RUNNING:
            results.append(StepResult(
                "proxy fragment", True, "proxy unavailable; fragment may remain on disk"
            ))
            return
        results.append(StepResult("proxy fragment", True, "removed or already absent"))

    try:
        remove_proxy_fragment()
    except Exception as exc:
        results.append(StepResult("proxy fragment", False, str(exc)))

    def cleanup() -> None:
        if not inventory_ok or any(not result.ok for result in results):
            # Capped ACL names cannot be reversed: retain rows needed to rediscover them.
            raise ValueError("state and registry retained because a removal step failed")
        with session_factory() as session:
            for row in session.exec(select(PoolIP)).all():
                if _pool_owned(row.container_prefix, plan.prefix, known, ownership):
                    session.delete(row)
            for records in (
                session.exec(select(RefreshState)).all(),
                session.exec(select(RepoUpgradeState)).all(),
                session.exec(select(EgressOverride)).all(),
                session.exec(select(BackgroundJob)).all(),
                session.exec(select(SentRecord)).all(),
            ):
                for record in records:
                    if record.container_prefix == plan.prefix:
                        session.delete(record)
            for notice in session.exec(select(DismissedNotice)).all():
                if notice.scope == plan.prefix:
                    session.delete(notice)
            session.commit()
            # Never purge recoverable user data after an incomplete removal.
            if all(result.ok for result in results):
                for path in plan.purge_files:

                    def purge(path: Path = path) -> None:
                        if path.is_symlink() or path.is_file():
                            path.unlink()
                        elif path.exists():
                            shutil.rmtree(path)

                    step(f"purge {path}", purge)
            if all(result.ok for result in results):
                registered = session.get(RegisteredRepo, plan.prefix)
                if registered is not None:
                    session.delete(registered)
                session.commit()
            else:
                raise ValueError("registry retained because a removal step failed")

    step("registry", cleanup)
    return results


def _pool_owned(value: str, prefix: str, known: set[str], ownership: dict[str, str]) -> bool:
    if value == prefix:
        return True
    if not value.startswith("ct:"):
        return False
    name = value[3:]
    if name in ownership:
        return ownership[name] == prefix
    matches = [p for p in known if name.startswith(p + "-")]
    return bool(matches) and max(matches, key=len) == prefix


def _load_registered_config(row: RegisteredRepo | None) -> Config | None:
    if row is None:
        return None
    root = Path(row.repo_root)
    if not root.is_dir() or not (row.synthetic_config or repo_config_path(root)):
        return None
    try:
        cfg = load_repo_config(root)
        return cfg if cfg.container_prefix == row.container_prefix else None
    except (ConfigError, OSError, ValueError):
        return None


def plan_removal(
    prefix: str,
    incus: Incus,
    session_factory: Callable[[], Session],
    *,
    purge: bool = False,
    cfg: Config | None = None,
) -> RemovalPlan:
    """Inventory only resources owned by this repository; perform no mutations."""
    if cfg is not None and cfg.container_prefix != prefix:
        raise ValueError("config container prefix does not match target")
    with session_factory() as session:
        rows = session.exec(select(RegisteredRepo)).all()
        if cfg is None:
            cfg = _load_registered_config(
                next((r for r in rows if r.container_prefix == prefix), None)
            )
        profile_names = set(incus.profile_list())
        known = (
            {r.container_prefix for r in rows}
            | {p[:-5] for p in profile_names if p.endswith("-base") and p[:-5]}
            | {prefix}
        )
        instances = incus.list_containers(fast=True)
        ownership: dict[str, str] = {}
        for instance in instances:
            name = instance.get("name")
            if not isinstance(name, str):
                continue
            for profile in instance.get("profiles") or []:
                if profile.endswith("-base") and profile[:-5] in known:
                    ownership[name] = profile[:-5]
                    break
        alias = cfg.golden.alias if cfg else f"{prefix}-base"
        containers = sorted(
            name
            for name, owner in ownership.items()
            if owner == prefix
        )

        pool_rows = session.exec(select(PoolIP)).all()
        known_container_names = set(ownership)
        known_container_names.update(
            row.container_prefix[3:] for row in pool_rows if row.container_prefix.startswith("ct:")
        )
        known_container_names.update(
            row.container_name for row in session.exec(select(BackgroundJob)).all()
        )
        known_container_names.update(
            row.container for row in session.exec(select(SentRecord)).all()
        )
        state_counts = {
            "pool_ip": sum(
                _pool_owned(r.container_prefix, prefix, known, ownership) for r in pool_rows
            ),
            "refresh_state": sum(
                r.container_prefix == prefix for r in session.exec(select(RefreshState)).all()
            ),
            "repo_upgrade_state": sum(
                r.container_prefix == prefix for r in session.exec(select(RepoUpgradeState)).all()
            ),
            "egress_override": sum(
                r.container_prefix == prefix for r in session.exec(select(EgressOverride)).all()
            ),
            "background_op": sum(
                r.container_prefix == prefix for r in session.exec(select(BackgroundJob)).all()
            ),
            "dismissed_notice": sum(
                r.scope == prefix for r in session.exec(select(DismissedNotice)).all()
            ),
            "sent_record": sum(
                r.container_prefix == prefix for r in session.exec(select(SentRecord)).all()
            ),
        }
    profiles = [
        f"{prefix}-{s}"
        for s in (
            "base",
            "binds",
            "net-strict",
            "net-loose",
            "net-offline",
            "net-work",
            "net-work-strict",
            "net-work-loose",
        )
        if f"{prefix}-{s}" in profile_names
    ]
    candidates = [
        f"{prefix}-allowlist",
        bridge_extras_acl_name(prefix),
        f"{prefix}-work-loose",
        _capped_acl_name(prefix, "-work-container-extras"),
        *(extra_acl_name(c) for c in containers),
    ]
    existing_acls = set(incus.network_acl_list())
    owned_names = {
        n for n in known_container_names if _pool_owned(f"ct:{n}", prefix, known, ownership)
    }
    foreign_names = known_container_names - owned_names
    foreign_acls = {extra_acl_name(n) for n in foreign_names}
    candidates.extend(extra_acl_name(n) for n in sorted(owned_names))
    candidates.extend(
        acl
        for acl in sorted(existing_acls)
        if acl.endswith("-extra")
        and len(acl) <= ACL_NAME_MAX
        and _pool_owned(f"ct:{acl[:-6]}", prefix, known, ownership)
    )
    acls = [
        name
        for name in dict.fromkeys(candidates)
        if name in existing_acls and name not in foreign_acls
    ]
    alias = cfg.golden.alias if cfg else f"{prefix}-base"
    other_configs = [(row, _load_registered_config(row)) for row in rows if row.container_prefix != prefix]
    reason = None
    if alias == SCRATCH_BASE_ALIAS:
        reason = "shared scratch base"
    else:
        for other, other_cfg in other_configs:
            if other_cfg is None:
                reason = f"config unavailable for {other.container_prefix}"
                break
            if other_cfg.golden.alias == alias:
                reason = f"shared with {other.container_prefix}"
                break
    images: list[str] = []
    golden_instances: list[str] = []
    if reason is None:
        image_names = {
            a.get("name") for image in incus.list_images() for a in image.get("aliases", [])
        }
        images = [a for a in (alias, f"{alias}--new", f"{alias}--old") if a in image_names]
        images.extend(a.alias for a in find_all_archived_images(incus, [alias]))
        instance_names = {i.get("name") for i in instances}
        golden_instances = [
            n
            for n in dict.fromkeys(
                (f"{alias}--golden", f"{alias}--build", f"{alias}--old", f"{prefix}-base-build")
            )
            if n in instance_names and n not in ownership
        ]
    default_shared = _default_shared_dir(prefix)
    shared = Path(cfg.shared_dir) if cfg and cfg.shared_dir else default_shared
    shared_reason = None
    for other, other_cfg in other_configs:
        if other_cfg is None:
            shared_reason = f"config unavailable for {other.container_prefix}"
            break
        other_shared = Path(other_cfg.shared_dir) if other_cfg.shared_dir else _default_shared_dir(other.container_prefix)
        if shared.resolve() == other_shared.resolve():
            shared_reason = f"shared with {other.container_prefix}"
            break
    purge_files: list[Path] = []
    kept_files: list[Path] = []
    for path in (local_config_path(prefix), shared):
        if path.exists():
            (
                purge_files
                if purge and (path != shared or (shared == default_shared and shared_reason is None))
                else kept_files
            ).append(path)
    global_cfg, _ = load_global_config(default_global_config_path())
    refs = []
    if prefix in global_cfg.credentials.repos:
        refs.append("credentials.repos")
    if prefix in global_cfg.remote.ssh.excluded_repos:
        refs.append("remote.ssh.excluded_repos")
    for i, root in enumerate(global_cfg.remote.ssh.mount_roots):
        if prefix in root.repos:
            refs.append(f"remote.ssh.mount_roots[{i}].repos")
    return RemovalPlan(
        prefix,
        cfg,
        containers,
        profiles,
        acls,
        images,
        golden_instances,
        reason,
        state_counts,
        purge_files,
        kept_files,
        refs,
        shared_reason,
    )
