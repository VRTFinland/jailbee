"""Cumulative egress IP pool with TTL eviction.

This module is the single source of truth for the per-repo allowlist
ACL and the in-container `/etc/hosts` pinning blocks. Both `jailbee apply`
(synchronous) and the singleton `jailbee net refresh` timer (background)
call `refresh_pool` against the same SQLite-backed pool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from jailbee.config import load_config
from jailbee.db.models import PoolIP, RefreshState, RegisteredRepo
from jailbee.egress import resolve_with_status
from jailbee.loose_revert import check_and_revert_loose
from jailbee.paths import repo_config_path

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.egress import EgressEntry
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

log = logging.getLogger(__name__)

DEFAULT_TTL = timedelta(hours=24)
DEFAULT_MAX_PER_HOST = 20


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of one refresh cycle for one repo."""

    container_prefix: str
    # "error" is `refresh_all`'s catch-all for a repo whose refresh raised;
    # every other value comes from refresh_pool itself.
    status: str  # "ok" | "dns_error" | "partial" | "acl_error" | "error"
    added: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None


def register_repo(session: Session, cfg: Config) -> None:
    """Idempotently register the repo so the timer refreshes it.

    Handles three transitions: brand-new registration, repo `mv` (same
    prefix, new path), and config-edited prefix change (different
    prefix at the same path). The latter clears the stale row.
    """
    prefix = cfg.container_prefix
    repo_root = str(Path(cfg.repo_root).resolve())

    stale = session.exec(
        select(RegisteredRepo).where(
            RegisteredRepo.repo_root == repo_root,
            RegisteredRepo.container_prefix != prefix,
        )
    ).all()
    for row in stale:
        log.info(
            "egress_pool: dropping stale registration %s (prefix changed at %s)",
            row.container_prefix,
            repo_root,
        )
        session.delete(row)

    existing = session.get(RegisteredRepo, prefix)
    if existing is None:
        session.add(
            RegisteredRepo(
                container_prefix=prefix,
                repo_root=repo_root,
                registered_at=datetime.now(UTC),
                synthetic_config=cfg.is_synthetic(),
            )
        )
    else:
        if existing.repo_root != repo_root:
            existing.repo_root = repo_root
        # Both directions are real transitions: a scratch directory gains a
        # `.jailbee/config.yaml`, or a configured repo's config is deleted.
        # The flag is what `refresh_all` consults before pruning a row whose
        # config file it cannot find.
        if existing.synthetic_config != cfg.is_synthetic():
            existing.synthetic_config = cfg.is_synthetic()
    session.commit()


def merge_resolved_ips(
    session: Session,
    container_prefix: str,
    resolved: dict[str, list[str]],
    *,
    now: datetime,
) -> list[tuple[str, str]]:
    """Upsert resolved (hostname, ip) pairs into the pool.

    For each (prefix, hostname, ip):
      - existing row → bump ``last_seen`` to ``now``
      - new row → insert with ``first_seen = last_seen = now``

    Returns the list of newly-added ``(hostname, ip)`` pairs.
    """
    added: list[tuple[str, str]] = []
    for hostname, ips in resolved.items():
        for ip in ips:
            row = session.get(PoolIP, (container_prefix, hostname, ip))
            if row is None:
                session.add(
                    PoolIP(
                        container_prefix=container_prefix,
                        hostname=hostname,
                        ip=ip,
                        first_seen=now,
                        last_seen=now,
                    )
                )
                added.append((hostname, ip))
            else:
                row.last_seen = now
    return added


def evict_expired(
    session: Session,
    container_prefix: str,
    refreshed_hostnames: set[str],
    *,
    now: datetime,
    ttl: timedelta = DEFAULT_TTL,
    max_per_host: int = DEFAULT_MAX_PER_HOST,
) -> list[tuple[str, str]]:
    """Evict IPs past TTL or above per-host cap, ONLY for refreshed hostnames.

    Hostnames whose DNS failed this cycle (not in ``refreshed_hostnames``)
    are left alone — their ``last_seen`` is stale through no fault of the IP.
    """
    cutoff = now - ttl
    removed: list[tuple[str, str]] = []

    for hostname in refreshed_hostnames:
        stale_rows = session.exec(
            select(PoolIP).where(
                PoolIP.container_prefix == container_prefix,
                PoolIP.hostname == hostname,
                PoolIP.last_seen < cutoff,
            )
        ).all()
        for row in stale_rows:
            removed.append((row.hostname, row.ip))
            session.delete(row)

        survivors = session.exec(
            select(PoolIP)
            .where(
                PoolIP.container_prefix == container_prefix,
                PoolIP.hostname == hostname,
            )
            .order_by(PoolIP.last_seen)  # type: ignore[arg-type]
        ).all()
        if len(survivors) > max_per_host:
            over = len(survivors) - max_per_host
            for row in survivors[:over]:
                removed.append((row.hostname, row.ip))
                session.delete(row)

    return removed


def refresh_pool(
    cfg: Config,
    gcfg: GlobalConfig,
    incus: Incus,
    session: Session,
    *,
    now: datetime,
    ttl: timedelta = DEFAULT_TTL,
    max_per_host: int = DEFAULT_MAX_PER_HOST,
) -> RefreshResult:
    """One refresh cycle for one repo.

    Order:
      1. Parse effective_repo_entries (config + host-local repo overrides),
         collect hostname targets.
      2. resolve_with_status → (resolved, failed)
      3. merge resolved IPs (bumps last_seen)
      4. evict_expired (only for resolved hostnames; honours TTL + cap)
      5. record RefreshState (status, timestamp, error msg) and commit
      6. phase B (unconditional — see below): refresh each container's own
         extra ACL, then prune pool rows for containers that no longer exist
      7. IF anything resolved: compute mirror_endpoint, write ACL, update
         /etc/hosts

    Errors writing ACL or /etc/hosts do not roll back the DB transaction —
    state already reflects what we resolved. Phase B always runs, even when
    step 7 is skipped because the repo's own resolve was empty (a
    literals-only ``egress_allow`` has no hostnames to resolve at all, which
    is not the same thing as a DNS failure) — otherwise a repo with no
    hostname entries would never refresh its containers' extras or prune
    their pool rows. Phase B is independently guarded: a failure there must
    not undo or overshadow what was already written (or about to be written)
    around it.

    Phase B runs BEFORE step 7's `_update_strict_container_hosts`, not
    after: that helper reads each container's extras back out of the same
    `ct:` pool key phase B just populated, to fold them into the
    `/etc/hosts` pin. Running phase B later left a freshly-added
    `jailbee net egress add` host missing from `/etc/hosts` for one whole
    extra refresh cycle after the CLI had already written it correctly —
    the next tick would then overwrite the CLI's correct pin with a stale
    one, for up to two cycles.
    """
    from jailbee.egress import parse_egress_entry
    from jailbee.egress_scope import effective_repo_entries

    prefix = cfg.container_prefix

    raw_entries = list(effective_repo_entries(cfg, session))
    specs = [parse_egress_entry(raw) for raw in raw_entries]
    hostnames = sorted({s.target for s in specs if not s.is_literal})

    resolved, failed = resolve_with_status(hostnames)

    added = merge_resolved_ips(session, prefix, resolved, now=now)
    removed = evict_expired(
        session,
        prefix,
        set(resolved.keys()),
        now=now,
        ttl=ttl,
        max_per_host=max_per_host,
    )

    if not resolved and failed:
        status = "dns_error"
    elif failed:
        status = "partial"
    else:
        status = "ok"

    error_msg: str | None
    if failed:
        error_msg = "; ".join(f"{name}: {err}" for name, err in failed.items())
    else:
        error_msg = None

    _record_state(session, prefix, now, status, error_msg)
    session.commit()  # state durable BEFORE side effects

    # Phase B (unconditional, and BEFORE step 7 below — see the ordering
    # note in the docstring): refresh each container's own extra ACL, then
    # prune pool rows for containers that no longer exist. A failure here
    # must NOT invalidate the repo-scope work already committed above, or
    # block step 7 from running below.
    try:
        _refresh_container_extras(cfg, incus, session, now=now, ttl=ttl, max_per_host=max_per_host)
        _prune_container_pools(incus, session)
    except Exception as e:
        log.warning("refresh_pool: container-extras phase failed for %s: %s", prefix, e)

    if resolved:
        # Only when something resolved: don't push an ACL/hosts derived from
        # an empty resolve — what's already in Incus stays; the next
        # successful cycle will refresh it. A literals-only allowlist (no
        # hostnames at all) and a total DNS failure both leave `resolved`
        # empty, so both skip this block — but phase B above still ran for
        # both, since it has no dependency on the repo's own resolve.
        mirror_endpoint = _compute_mirror_endpoint(cfg, incus, gcfg)

        try:
            _write_acl(cfg, session, incus, mirror_endpoint=mirror_endpoint)
        except Exception as e:
            log.warning("refresh_pool: ACL write failed for %s: %s", prefix, e)
            _record_state(session, prefix, now, "acl_error", str(e))
            session.commit()
            return RefreshResult(
                container_prefix=prefix,
                status="acl_error",
                added=added,
                removed=removed,
                error=str(e),
            )

        _update_strict_container_hosts(
            cfg,
            session,
            incus,
            mirror_endpoint=mirror_endpoint,
        )

    return RefreshResult(
        container_prefix=prefix,
        status=status,
        added=added,
        removed=removed,
        error=error_msg,
    )


def _record_state(
    session: Session,
    prefix: str,
    now: datetime,
    status: str,
    error_msg: str | None,
) -> None:
    existing = session.get(RefreshState, prefix)
    if existing is None:
        session.add(
            RefreshState(
                container_prefix=prefix,
                last_refresh_at=now,
                last_refresh_status=status,
                last_error_msg=error_msg,
            )
        )
    else:
        existing.last_refresh_at = now
        existing.last_refresh_status = status
        existing.last_error_msg = error_msg


def _compute_mirror_endpoint(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
) -> tuple[str, int] | None:
    """Resolve the registry-mirror endpoint, or None when unwanted/unreachable.

    Best-effort by design. This runs on the 60s timer and inside `jailbee new`,
    where a mirror that is merely stopped must not abort the cycle: the ACL
    omits the mirror rule until a later refresh finds it running.
    """
    from jailbee.docker_daemon import mirror_wanted

    if not mirror_wanted(cfg, gcfg):
        return None
    from jailbee.docker_daemon import compute_mirror_endpoint

    try:
        return compute_mirror_endpoint(incus, gcfg)
    except ValueError as e:
        log.warning("refresh_pool: mirror unavailable, ACL omits its rule: %s", e)
        return None


def _write_acl(
    cfg: Config,
    session: Session,
    incus: Incus,
    *,
    mirror_endpoint: tuple[str, int] | None,
) -> None:
    """Build EgressEntry list from current pool + literal egress entries,
    render the ACL YAML, and push via the nft-quirk-aware helper.
    """
    from jailbee.egress_scope import effective_repo_entries
    from jailbee.network import acl_name, allowlist_acl_yaml

    prefix = cfg.container_prefix
    raw_entries = list(effective_repo_entries(cfg, session))
    entries = _entries_from_pool(session, prefix, raw_entries)

    yaml_text = allowlist_acl_yaml(cfg, entries, mirror_endpoint=mirror_endpoint)
    _apply_acl_with_nft_quirk(incus, acl_name(cfg), yaml_text)


def _apply_acl_with_nft_quirk(incus: Incus, name: str, yaml_text: str) -> None:
    """Push ACL YAML, tolerating the Incus ≤6.18 nft-flush-chain quirk.

    Mirrors apply._apply_acl_with_nft_quirk so the refresh path is consistent.
    """
    from jailbee.incus import IncusError
    from jailbee.init_command import _is_nft_flush_chain_missing
    from jailbee.tui import warn

    try:
        incus.network_acl_set_yaml(name, yaml_text)
    except IncusError as e:
        if _is_nft_flush_chain_missing(str(e)):
            warn(
                f"ACL {name} updated in Incus, but nftables sync "
                f"failed (chain not yet present). Cosmetic — next "
                f"container start will refresh the chain."
            )
            return
        raise


CONTAINER_POOL_PREFIX = "ct:"
"""Pool-key namespace for per-container extras.

Disjoint from real ``container_prefix`` values, which are bare repo prefixes,
so one pool table serves both scopes with no chance of collision.
"""


def container_pool_key(name: str) -> str:
    return f"{CONTAINER_POOL_PREFIX}{name}"


def _entries_from_pool(session: Session, key: str, raw_entries: list[str]) -> list[EgressEntry]:
    """Build `EgressEntry` list for `raw_entries` from pool key `key`.

    Shared by the repo scope (`_write_acl`, `_update_strict_container_hosts`)
    and the container scope (`_refresh_container_extras`) so both agree on
    one literal-vs-hostname rule.
    """
    from jailbee.egress import EgressEntry, parse_egress_entry

    pool_by_host: dict[str, list[str]] = {}
    for row in session.exec(select(PoolIP).where(PoolIP.container_prefix == key)).all():
        pool_by_host.setdefault(row.hostname, []).append(row.ip)

    entries: list[EgressEntry] = []
    for raw in raw_entries:
        spec = parse_egress_entry(raw)
        if spec.is_literal:
            destinations = [spec.target]
        else:
            destinations = sorted(pool_by_host.get(spec.target, []))
            if not destinations:
                # No pool entries for this hostname yet — skip; the next
                # refresh that resolves it will include it.
                continue
        entries.append(EgressEntry(destinations=destinations, port=spec.port, description=raw))
    return entries


def _refresh_container_extras(
    cfg: Config,
    incus: Incus,
    session: Session,
    *,
    now: datetime,
    ttl: timedelta,
    max_per_host: int,
) -> None:
    """Resolve and push each container's own extra ACL.

    Extras go through the same pool as the repo allowlist so a GSLB-rotating
    host does not desync between what jailbee resolved and what the container
    resolves — the reason the pool exists at all.

    A failure for one container is logged and skipped: it must not cost the
    other containers, nor the repo ACL that was already written.

    Skips a container whose current mode is not ``strict``:
    `apply_container_acl(mode="loose")` deletes the extra ACL on every mode
    switch, and this loop would otherwise recreate it every tick right
    after — permanent, harmless-but-pointless churn the orphan sweep in
    `apply.py` never reclaims because the ACL keeps getting rebuilt.
    """
    from jailbee import egress_scope
    from jailbee.egress import parse_egress_entry
    from jailbee.network import extra_acl_yaml

    for container in _list_containers(cfg, incus):
        if container.network != "strict":
            continue
        try:
            extras = egress_scope.container_extras(incus, container.name)
            if not extras:
                continue
            key = container_pool_key(container.name)
            specs = [parse_egress_entry(raw) for raw in extras]
            hostnames = sorted({s.target for s in specs if not s.is_literal})
            resolved, failed = resolve_with_status(hostnames)
            if failed:
                log.warning(
                    "refresh_pool: %s extras — unresolved: %s",
                    container.name,
                    "; ".join(f"{n}: {e}" for n, e in failed.items()),
                )
            merge_resolved_ips(session, key, resolved, now=now)
            evict_expired(session, key, set(resolved), now=now, ttl=ttl, max_per_host=max_per_host)
            session.commit()

            entries = _entries_from_pool(session, key, extras)
            if not entries:
                # Nothing resolved yet: leave the existing ACL alone rather
                # than pushing an empty one, mirroring the repo path.
                continue
            acl = egress_scope.extra_acl_name(container.name)
            if not incus.network_acl_exists(acl):
                incus.network_acl_create(acl)
            _apply_acl_with_nft_quirk(incus, acl, extra_acl_yaml(acl, entries))
        except Exception as e:
            log.warning("refresh_pool: container extras failed for %s: %s", container.name, e)


def _prune_container_pools(incus: Incus, session: Session) -> list[str]:
    """Drop `ct:` pool rows whose container no longer exists."""
    live = {container_pool_key(c["name"]) for c in incus.list_containers()}
    rows = session.exec(
        select(PoolIP).where(PoolIP.container_prefix.startswith(CONTAINER_POOL_PREFIX))
    ).all()
    dropped: list[str] = []
    for row in rows:
        if row.container_prefix in live:
            continue
        if row.container_prefix not in dropped:
            dropped.append(row.container_prefix)
        session.delete(row)
    session.commit()
    return sorted(dropped)


def _update_strict_container_hosts(
    cfg: Config,
    session: Session,
    incus: Incus,
    *,
    mirror_endpoint: tuple[str, int] | None,
) -> None:
    """Re-pin /etc/hosts on every running strict container of this repo.

    Per-container ``IncusError`` is caught and logged; failure on one
    container does not abort the cycle.
    """
    from jailbee import egress_scope
    from jailbee.egress_scope import effective_repo_entries
    from jailbee.hosts import apply_hosts
    from jailbee.incus import IncusError

    prefix = cfg.container_prefix
    raw_entries = list(effective_repo_entries(cfg, session))
    entries = _entries_from_pool(session, prefix, raw_entries)

    for container in _list_containers(cfg, incus):
        if container.state != "Running" or container.network != "strict":
            continue
        try:
            ct_entries = _entries_from_pool(
                session,
                container_pool_key(container.name),
                egress_scope.container_extras(incus, container.name),
            )
            apply_hosts(
                cfg,
                incus,
                container.name,
                entries=[*entries, *ct_entries],
                mirror_endpoint=mirror_endpoint,
            )
        except IncusError as e:
            log.warning(
                "refresh_pool: /etc/hosts update failed for %s: %s",
                container.name,
                e,
            )


def _list_containers(cfg: Config, incus: Incus) -> list[Any]:
    """Return container info objects for this repo.

    Delegates to apply._list_containers so the filter logic stays in one place.
    """
    from jailbee.apply import _list_containers as impl

    return list(impl(cfg, incus))


def refresh_all(
    session: Session,
    gcfg: GlobalConfig,
    incus: Incus,
    *,
    now: datetime,
) -> dict[str, RefreshResult]:
    """Iterate every registered repo and refresh its pool.

    Self-prunes registrations where neither ``.jailbee/config.yaml`` nor the
    deprecated ``.gie/config.yaml`` exists any more.
    Skips (without pruning) cycles where the repo's ``container_prefix``
    no longer matches the registered value — user must run ``jailbee apply``
    to migrate.
    """
    out: dict[str, RefreshResult] = {}
    repos = list(session.exec(select(RegisteredRepo)).all())

    for repo in repos:
        repo_root = Path(repo.repo_root)
        config_yaml = repo_config_path(repo_root)

        if config_yaml is None:
            log.info(
                "refresh_all: unregistering %s — config not found at %s",
                repo.container_prefix,
                repo_root,
            )
            session.delete(repo)
            session.commit()
            continue

        try:
            cfg = load_config(config_yaml)
        except Exception as e:
            log.warning(
                "refresh_all: skipping %s — config load failed: %s",
                repo.container_prefix,
                e,
            )
            continue

        if cfg.container_prefix != repo.container_prefix:
            log.warning(
                "refresh_all: %s — prefix changed to %s; run `jailbee apply` to migrate",
                repo.container_prefix,
                cfg.container_prefix,
            )
            continue

        try:
            out[repo.container_prefix] = refresh_pool(cfg, gcfg, incus, session, now=now)
            repo.last_refresh_at = now
        except Exception as e:
            log.warning(
                "refresh_all: refresh_pool failed for %s: %s",
                repo.container_prefix,
                e,
            )
            # Record it rather than dropping the key. `jailbee net refresh` —
            # verbatim the systemd unit's ExecStart — iterates this dict and
            # treats any status outside ("ok", "partial") as FAIL, so a dropped
            # key would exit 0 and let the timer unit look healthy. The
            # missing-mirror case is already handled inside
            # `_compute_mirror_endpoint`, so anything reaching here is a bug.
            out[repo.container_prefix] = RefreshResult(
                container_prefix=repo.container_prefix,
                status="error",
                error=str(e),
            )
            # No `continue`: check_and_revert_loose below is independent of the
            # pool refresh and must still run for this repo.

        # TTL-driven revert of `jailbee net loose` containers. One call per
        # registered repo; it acts on whatever `user.jailbee.loose_until` labels
        # exist, independent of the repo's `loose_auto_revert` policy (that
        # policy only decides whether jailbee writes a label in the first place).
        try:
            check_and_revert_loose(cfg, incus, now=now)
        except Exception as e:
            log.warning(
                "refresh_all: loose_revert failed for %s: %s",
                repo.container_prefix,
                e,
            )

    session.commit()
    return out
