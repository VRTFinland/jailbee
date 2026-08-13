"""Tests for merge_resolved_ips, evict_expired, and resolve_with_status."""

from __future__ import annotations

from datetime import datetime, timedelta

from pytest_mock import MockerFixture
from sqlmodel import Session, select

from jailbee.db.models import PoolIP


def test_merge_inserts_new_ip(db_session: Session, frozen_now: datetime) -> None:
    from jailbee.egress_pool import merge_resolved_ips

    added = merge_resolved_ips(
        db_session,
        "X",
        {"github.com": ["1.1.1.1"]},
        now=frozen_now,
    )
    assert added == [("github.com", "1.1.1.1")]

    row = db_session.get(PoolIP, ("X", "github.com", "1.1.1.1"))
    assert row is not None
    assert row.first_seen == frozen_now
    assert row.last_seen == frozen_now


def test_merge_bumps_last_seen_on_existing(
    db_session: Session,
    frozen_now: datetime,
) -> None:
    from jailbee.egress_pool import merge_resolved_ips

    earlier = frozen_now - timedelta(hours=2)
    db_session.add(
        PoolIP(
            container_prefix="X",
            hostname="github.com",
            ip="1.1.1.1",
            first_seen=earlier,
            last_seen=earlier,
        )
    )
    db_session.commit()

    added = merge_resolved_ips(
        db_session,
        "X",
        {"github.com": ["1.1.1.1"]},
        now=frozen_now,
    )
    assert added == []  # not "added" — already there

    row = db_session.get(PoolIP, ("X", "github.com", "1.1.1.1"))
    assert row is not None
    assert row.first_seen == earlier
    assert row.last_seen == frozen_now


def test_merge_handles_multiple_hosts_and_ips(
    db_session: Session,
    frozen_now: datetime,
) -> None:
    from jailbee.egress_pool import merge_resolved_ips

    added = merge_resolved_ips(
        db_session,
        "X",
        {
            "github.com": ["1.1.1.1", "1.1.1.2"],
            "api.github.com": ["1.1.1.3"],
        },
        now=frozen_now,
    )
    assert set(added) == {
        ("github.com", "1.1.1.1"),
        ("github.com", "1.1.1.2"),
        ("api.github.com", "1.1.1.3"),
    }


def test_evict_expired_drops_only_for_refreshed_hosts(
    db_session: Session,
    frozen_now: datetime,
) -> None:
    from jailbee.egress_pool import evict_expired

    old = frozen_now - timedelta(hours=25)
    # IP for a refreshed host, past TTL
    db_session.add(
        PoolIP(
            container_prefix="X",
            hostname="github.com",
            ip="old.refreshed",
            first_seen=old,
            last_seen=old,
        )
    )
    # IP for a NOT-refreshed host (DNS failed for it), also past TTL.
    db_session.add(
        PoolIP(
            container_prefix="X",
            hostname="api.anthropic.com",
            ip="old.skipped",
            first_seen=old,
            last_seen=old,
        )
    )
    db_session.commit()

    removed = evict_expired(
        db_session,
        "X",
        refreshed_hostnames={"github.com"},
        now=frozen_now,
        ttl=timedelta(hours=24),
    )
    assert removed == [("github.com", "old.refreshed")]

    rows = db_session.exec(select(PoolIP)).all()
    survivors = {(r.hostname, r.ip) for r in rows}
    assert survivors == {("api.anthropic.com", "old.skipped")}


def test_evict_keeps_ips_within_ttl(
    db_session: Session,
    frozen_now: datetime,
) -> None:
    from jailbee.egress_pool import evict_expired

    fresh = frozen_now - timedelta(hours=23, minutes=59)
    db_session.add(
        PoolIP(
            container_prefix="X",
            hostname="github.com",
            ip="1.1.1.1",
            first_seen=fresh,
            last_seen=fresh,
        )
    )
    db_session.commit()

    removed = evict_expired(
        db_session,
        "X",
        {"github.com"},
        now=frozen_now,
        ttl=timedelta(hours=24),
    )
    assert removed == []


def test_cap_evicts_oldest_first(
    db_session: Session,
    frozen_now: datetime,
) -> None:
    from jailbee.egress_pool import evict_expired

    # Insert 22 IPs for one host, decreasing freshness (higher i = older)
    for i in range(22):
        ts = frozen_now - timedelta(minutes=i)
        db_session.add(
            PoolIP(
                container_prefix="X",
                hostname="github.com",
                ip=f"1.1.1.{i}",
                first_seen=ts,
                last_seen=ts,
            )
        )
    db_session.commit()

    removed = evict_expired(
        db_session,
        "X",
        {"github.com"},
        now=frozen_now,
        ttl=timedelta(hours=24),
        max_per_host=20,
    )
    # Two oldest should be evicted (i=20 and i=21)
    assert set(removed) == {("github.com", "1.1.1.20"), ("github.com", "1.1.1.21")}

    rows = db_session.exec(select(PoolIP)).all()
    assert len(rows) == 20


def test_resolve_with_status_all_ok(mocker: MockerFixture) -> None:
    from jailbee import egress_pool

    mocker.patch(
        "jailbee.egress_pool.resolve_hostnames",
        return_value={"github.com": ["1.1.1.1"], "api.github.com": ["1.1.1.2"]},
    )
    resolved, failed = egress_pool.resolve_with_status(
        ["github.com", "api.github.com"],
    )
    assert resolved == {"github.com": ["1.1.1.1"], "api.github.com": ["1.1.1.2"]}
    assert failed == {}


def test_resolve_with_status_total_failure(mocker: MockerFixture) -> None:
    from jailbee import egress_pool
    from jailbee.egress import NetworkResolveError

    def boom(names: list[str]) -> dict[str, list[str]]:
        raise NetworkResolveError(names[0], Exception("getaddrinfo: -3"))

    mocker.patch(
        "jailbee.egress_pool.resolve_hostnames",
        side_effect=boom,
    )
    resolved, failed = egress_pool.resolve_with_status(
        ["github.com", "api.github.com"],
    )
    assert resolved == {}
    assert set(failed.keys()) == {"github.com", "api.github.com"}


def test_resolve_with_status_partial_failure(mocker: MockerFixture) -> None:
    """When the batched resolve fails, try one at a time and split."""
    from jailbee import egress_pool
    from jailbee.egress import NetworkResolveError

    def selective(names: list[str]) -> dict[str, list[str]]:
        if names == ["github.com", "api.github.com"]:
            raise NetworkResolveError("api.github.com", Exception("temp fail"))
        if names == ["github.com"]:
            return {"github.com": ["1.1.1.1"]}
        raise NetworkResolveError(names[0], Exception("temp fail"))

    mocker.patch(
        "jailbee.egress_pool.resolve_hostnames",
        side_effect=selective,
    )
    resolved, failed = egress_pool.resolve_with_status(
        ["github.com", "api.github.com"],
    )
    assert resolved == {"github.com": ["1.1.1.1"]}
    assert set(failed.keys()) == {"api.github.com"}
