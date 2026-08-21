"""Tests for refresh_pool / refresh_all (ACL + hosts side-effects mocked)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture
from sqlmodel import Session, select

from jailbee.db.models import PoolIP, RefreshState, RegisteredRepo


@pytest.fixture
def cfg(tmp_path: Path, mocker: MockerFixture) -> Any:
    """Mock Config with the bits refresh_pool reads."""
    c = mocker.Mock()
    c.container_prefix = "X"
    c.repo_root = tmp_path
    c.effective_egress_allow.return_value = ["github.com:443"]
    return c


@pytest.fixture
def gcfg(mocker: MockerFixture) -> Any:
    return mocker.Mock()


@pytest.fixture
def incus(mocker: MockerFixture) -> Any:
    """Mock Incus with side-effect targets we'll patch later."""
    i = mocker.Mock()
    i.network_acl_set_yaml.return_value = None
    i.list_containers.return_value = []
    return i


def test_refresh_pool_records_ok_status(
    db_session: Session,
    cfg: Any,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool

    mocker.patch(
        "jailbee.egress_pool.resolve_with_status",
        return_value=({"github.com": ["1.1.1.1"]}, {}),
    )
    mocker.patch.object(egress_pool, "_write_acl", autospec=True)
    mocker.patch.object(egress_pool, "_update_strict_container_hosts", autospec=True)
    mocker.patch.object(egress_pool, "_compute_mirror_endpoint", return_value=None)

    result = egress_pool.refresh_pool(
        cfg,
        gcfg,
        incus,
        db_session,
        now=frozen_now,
    )
    assert result.status == "ok"
    assert result.added == [("github.com", "1.1.1.1")]
    assert result.removed == []
    assert result.error is None

    state = db_session.get(RefreshState, "X")
    assert state is not None
    assert state.last_refresh_status == "ok"
    assert state.last_refresh_at == frozen_now

    pool = db_session.exec(select(PoolIP)).all()
    assert len(pool) == 1
    assert pool[0].ip == "1.1.1.1"


def test_refresh_pool_dns_total_failure_preserves_pool(
    db_session: Session,
    cfg: Any,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool

    # Pre-populate pool with an old IP — must survive a DNS-failure cycle.
    old = frozen_now - timedelta(hours=25)
    db_session.add(
        PoolIP(
            container_prefix="X",
            hostname="github.com",
            ip="ancient.ip",
            first_seen=old,
            last_seen=old,
        )
    )
    db_session.commit()

    mocker.patch(
        "jailbee.egress_pool.resolve_with_status",
        return_value=({}, {"github.com": "getaddrinfo: -3"}),
    )
    mocker.patch.object(egress_pool, "_write_acl", autospec=True)
    mocker.patch.object(egress_pool, "_update_strict_container_hosts", autospec=True)
    mocker.patch.object(egress_pool, "_compute_mirror_endpoint", return_value=None)

    result = egress_pool.refresh_pool(
        cfg,
        gcfg,
        incus,
        db_session,
        now=frozen_now,
    )
    assert result.status == "dns_error"
    assert "github.com" in (result.error or "")

    state = db_session.get(RefreshState, "X")
    assert state is not None
    assert state.last_refresh_status == "dns_error"

    pool = db_session.exec(select(PoolIP)).all()
    assert {p.ip for p in pool} == {"ancient.ip"}


def test_refresh_pool_partial_dns_failure(
    db_session: Session,
    cfg: Any,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool

    cfg.effective_egress_allow.return_value = ["github.com:443", "api.bad:443"]
    mocker.patch(
        "jailbee.egress_pool.resolve_with_status",
        return_value=({"github.com": ["1.1.1.1"]}, {"api.bad": "down"}),
    )
    mocker.patch.object(egress_pool, "_write_acl", autospec=True)
    mocker.patch.object(egress_pool, "_update_strict_container_hosts", autospec=True)
    mocker.patch.object(egress_pool, "_compute_mirror_endpoint", return_value=None)

    result = egress_pool.refresh_pool(
        cfg,
        gcfg,
        incus,
        db_session,
        now=frozen_now,
    )
    assert result.status == "partial"
    assert "api.bad" in (result.error or "")
    state = db_session.get(RefreshState, "X")
    assert state is not None
    assert state.last_refresh_status == "partial"


def test_write_acl_uses_pool_union(
    db_session: Session,
    cfg: Any,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    mocker: MockerFixture,
) -> None:
    """ACL is rendered from PoolIP rows, not from a fresh DNS resolution."""
    from jailbee import egress_pool

    mocker.patch(
        "jailbee.egress_pool.resolve_with_status",
        return_value=({"github.com": ["1.1.1.1"]}, {}),
    )

    # Pre-populate a second IP that DNS no longer returns — it should still
    # appear in the ACL because TTL hasn't elapsed.
    db_session.add(
        PoolIP(
            container_prefix="X",
            hostname="github.com",
            ip="1.1.1.2",
            first_seen=frozen_now - timedelta(hours=10),
            last_seen=frozen_now - timedelta(hours=10),
        )
    )
    db_session.commit()

    captured_entries: list[list[Any]] = []

    def capture_yaml(
        cfg_arg: Any,
        entries: list[Any],
        mirror_endpoint: Any = None,
    ) -> str:
        captured_entries.append(list(entries))
        return "EGRESS_YAML"

    mocker.patch(
        "jailbee.network.allowlist_acl_yaml",
        side_effect=capture_yaml,
    )
    mocker.patch.object(egress_pool, "_compute_mirror_endpoint", return_value=None)
    mocker.patch.object(egress_pool, "_update_strict_container_hosts")

    egress_pool.refresh_pool(cfg, gcfg, incus, db_session, now=frozen_now)

    assert len(captured_entries) == 1
    dests = captured_entries[0][0].destinations
    assert "1.1.1.1" in dests
    assert "1.1.1.2" in dests
    incus.network_acl_set_yaml.assert_called_once()


def test_write_acl_failure_records_acl_error(
    db_session: Session,
    cfg: Any,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool
    from jailbee.incus import IncusError

    mocker.patch(
        "jailbee.egress_pool.resolve_with_status",
        return_value=({"github.com": ["1.1.1.1"]}, {}),
    )
    mocker.patch.object(egress_pool, "_compute_mirror_endpoint", return_value=None)
    mocker.patch.object(egress_pool, "_update_strict_container_hosts")
    mocker.patch(
        "jailbee.network.allowlist_acl_yaml",
        return_value="EGRESS_YAML",
    )
    incus.network_acl_set_yaml.side_effect = IncusError("some unrelated incus failure")

    result = egress_pool.refresh_pool(
        cfg,
        gcfg,
        incus,
        db_session,
        now=frozen_now,
    )
    assert result.status == "acl_error"
    assert "some unrelated incus failure" in (result.error or "")

    pool = db_session.exec(select(PoolIP)).all()
    assert len(pool) == 1


def test_hosts_updated_for_running_strict_containers_only(
    db_session: Session,
    cfg: Any,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool

    mocker.patch(
        "jailbee.egress_pool.resolve_with_status",
        return_value=({"github.com": ["1.1.1.1"]}, {}),
    )
    mocker.patch.object(egress_pool, "_compute_mirror_endpoint", return_value=None)
    mocker.patch.object(egress_pool, "_write_acl")

    fake_strict_running = mocker.Mock(state="Running", network="strict")
    fake_strict_running.name = "X-foo"
    fake_loose_running = mocker.Mock(state="Running", network="loose")
    fake_loose_running.name = "X-bar"
    fake_strict_stopped = mocker.Mock(state="Stopped", network="strict")
    fake_strict_stopped.name = "X-baz"

    mocker.patch(
        "jailbee.egress_pool._list_containers",
        return_value=[fake_strict_running, fake_loose_running, fake_strict_stopped],
    )
    apply_hosts_mock = mocker.patch("jailbee.hosts.apply_hosts")

    egress_pool.refresh_pool(cfg, gcfg, incus, db_session, now=frozen_now)

    # Only the running strict container should be touched.
    assert apply_hosts_mock.call_count == 1
    name = apply_hosts_mock.call_args.args[2]
    assert name == "X-foo"


def test_hosts_update_failure_is_nonfatal(
    db_session: Session,
    cfg: Any,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool
    from jailbee.incus import IncusError

    mocker.patch(
        "jailbee.egress_pool.resolve_with_status",
        return_value=({"github.com": ["1.1.1.1"]}, {}),
    )
    mocker.patch.object(egress_pool, "_compute_mirror_endpoint", return_value=None)
    mocker.patch.object(egress_pool, "_write_acl")

    c1 = mocker.Mock(state="Running", network="strict")
    c1.name = "X-a"
    c2 = mocker.Mock(state="Running", network="strict")
    c2.name = "X-b"
    mocker.patch(
        "jailbee.egress_pool._list_containers",
        return_value=[c1, c2],
    )

    apply_hosts_mock = mocker.patch(
        "jailbee.hosts.apply_hosts",
        side_effect=[IncusError("c1 timeout"), None],
    )

    result = egress_pool.refresh_pool(
        cfg,
        gcfg,
        incus,
        db_session,
        now=frozen_now,
    )
    assert result.status == "ok"
    assert apply_hosts_mock.call_count == 2


def test_refresh_all_iterates_registered_repos(
    db_session: Session,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool

    repo_a = tmp_path / "a"
    (repo_a / ".gie").mkdir(parents=True)
    (repo_a / ".gie" / "config.yaml").write_text("# placeholder")
    repo_b = tmp_path / "b"
    (repo_b / ".gie").mkdir(parents=True)
    (repo_b / ".gie" / "config.yaml").write_text("# placeholder")

    db_session.add(
        RegisteredRepo(
            container_prefix="A",
            repo_root=str(repo_a),
            registered_at=frozen_now,
        )
    )
    db_session.add(
        RegisteredRepo(
            container_prefix="B",
            repo_root=str(repo_b),
            registered_at=frozen_now,
        )
    )
    db_session.commit()

    def fake_load(path: Path) -> Any:
        assert path.name == "config.yaml", (
            f"load_config must be called with the config file path, got {path}"
        )
        assert path.parent.name == ".gie"
        repo_root = path.parent.parent
        m = mocker.Mock()
        m.container_prefix = repo_root.name.upper()
        m.repo_root = repo_root
        m.effective_egress_allow.return_value = []
        return m

    mocker.patch("jailbee.egress_pool.load_config", side_effect=fake_load)
    mock_refresh = mocker.patch.object(
        egress_pool,
        "refresh_pool",
        return_value=egress_pool.RefreshResult(
            container_prefix="dummy",
            status="ok",
        ),
    )

    results = egress_pool.refresh_all(db_session, gcfg, incus, now=frozen_now)
    assert set(results.keys()) == {"A", "B"}
    assert mock_refresh.call_count == 2


def test_refresh_all_does_not_unregister_a_repo_migrated_to_jailbee_dir(
    db_session: Session,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """A repo whose config now lives at .jailbee/config.yaml must survive
    a refresh cycle rather than being pruned as if its config vanished."""
    from jailbee import egress_pool

    repo = tmp_path / "migrated"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".jailbee" / "config.yaml").write_text("# placeholder")

    db_session.add(
        RegisteredRepo(
            container_prefix="M",
            repo_root=str(repo),
            registered_at=frozen_now,
        )
    )
    db_session.commit()

    def fake_load(path: Path) -> Any:
        assert path.name == "config.yaml"
        assert path.parent.name == ".jailbee"
        m = mocker.Mock()
        m.container_prefix = "M"
        m.repo_root = path.parent.parent
        m.effective_egress_allow.return_value = []
        return m

    mocker.patch("jailbee.egress_pool.load_config", side_effect=fake_load)
    mocker.patch.object(
        egress_pool,
        "refresh_pool",
        return_value=egress_pool.RefreshResult(
            container_prefix="M",
            status="ok",
        ),
    )
    mocker.patch.object(egress_pool, "check_and_revert_loose")

    results = egress_pool.refresh_all(db_session, gcfg, incus, now=frozen_now)

    assert "M" in results
    rows = db_session.exec(select(RegisteredRepo)).all()
    assert {r.container_prefix for r in rows} == {"M"}


def test_refresh_all_self_prunes_missing_config(
    db_session: Session,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool

    db_session.add(
        RegisteredRepo(
            container_prefix="GHOST",
            repo_root=str(tmp_path / "vanished"),
            registered_at=frozen_now,
        )
    )
    db_session.commit()

    egress_pool.refresh_all(db_session, gcfg, incus, now=frozen_now)

    rows = db_session.exec(select(RegisteredRepo)).all()
    assert rows == []


def test_refresh_all_skips_on_prefix_mismatch(
    db_session: Session,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee import egress_pool

    repo = tmp_path / "moved"
    (repo / ".gie").mkdir(parents=True)
    (repo / ".gie" / "config.yaml").write_text("# placeholder")

    db_session.add(
        RegisteredRepo(
            container_prefix="OLD",
            repo_root=str(repo),
            registered_at=frozen_now,
        )
    )
    db_session.commit()

    def fake_load(path: Path) -> Any:
        assert path.name == "config.yaml"
        m = mocker.Mock()
        m.container_prefix = "NEW"  # config says NEW, registry has OLD
        m.repo_root = path.parent.parent
        return m

    mocker.patch("jailbee.egress_pool.load_config", side_effect=fake_load)
    mock_refresh = mocker.patch.object(egress_pool, "refresh_pool")

    egress_pool.refresh_all(db_session, gcfg, incus, now=frozen_now)
    mock_refresh.assert_not_called()

    rows = db_session.exec(select(RegisteredRepo)).all()
    assert {r.container_prefix for r in rows} == {"OLD"}


def test_refresh_all_invokes_loose_revert_per_repo(
    db_session: Session,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """Each registered repo gets one `check_and_revert_loose` call."""
    from jailbee import egress_pool

    repo_a = tmp_path / "a"
    (repo_a / ".gie").mkdir(parents=True)
    (repo_a / ".gie" / "config.yaml").write_text("# placeholder")

    db_session.add(
        RegisteredRepo(
            container_prefix="A",
            repo_root=str(repo_a),
            registered_at=frozen_now,
        )
    )
    db_session.commit()

    def fake_load(path: Path) -> Any:
        m = mocker.Mock()
        m.container_prefix = "A"
        m.repo_root = path.parent.parent
        m.effective_egress_allow.return_value = []
        return m

    mocker.patch("jailbee.egress_pool.load_config", side_effect=fake_load)
    mocker.patch.object(
        egress_pool,
        "refresh_pool",
        return_value=egress_pool.RefreshResult(
            container_prefix="A",
            status="ok",
        ),
    )
    revert = mocker.patch.object(egress_pool, "check_and_revert_loose")

    egress_pool.refresh_all(db_session, gcfg, incus, now=frozen_now)

    assert revert.call_count == 1
    args, kwargs = revert.call_args
    # No gcfg: the timer acts on the labels, not on the auto-revert policy.
    assert args[1] is incus
    assert kwargs["now"] == frozen_now


def test_refresh_all_continues_when_loose_revert_raises(
    db_session: Session,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """A failure inside loose_revert must not abort the rest of the cycle."""
    from jailbee import egress_pool

    repo_a = tmp_path / "a"
    (repo_a / ".gie").mkdir(parents=True)
    (repo_a / ".gie" / "config.yaml").write_text("# placeholder")

    db_session.add(
        RegisteredRepo(
            container_prefix="A",
            repo_root=str(repo_a),
            registered_at=frozen_now,
        )
    )
    db_session.commit()

    def fake_load(path: Path) -> Any:
        m = mocker.Mock()
        m.container_prefix = "A"
        m.repo_root = path.parent.parent
        m.effective_egress_allow.return_value = []
        return m

    mocker.patch("jailbee.egress_pool.load_config", side_effect=fake_load)
    mocker.patch.object(
        egress_pool,
        "refresh_pool",
        return_value=egress_pool.RefreshResult(
            container_prefix="A",
            status="ok",
        ),
    )
    mocker.patch.object(
        egress_pool,
        "check_and_revert_loose",
        side_effect=RuntimeError("boom"),
    )

    # Should not raise — the loop swallows the error and logs it.
    results = egress_pool.refresh_all(db_session, gcfg, incus, now=frozen_now)
    assert "A" in results


def test_compute_mirror_endpoint_returns_none_when_the_mirror_is_down(
    cfg: Any, gcfg: Any, incus: Any, mocker: MockerFixture
) -> None:
    """`jailbee new` calls refresh_pool (cli.py:923), so a ValueError here
    surfaces as a traceback from a command that only wanted a container."""
    from jailbee import egress_pool

    mocker.patch("jailbee.docker_daemon.mirror_wanted", return_value=True)
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        side_effect=ValueError("jailbee-registry-mirror container not found."),
    )

    assert egress_pool._compute_mirror_endpoint(cfg, incus, gcfg) is None


def test_compute_mirror_endpoint_skips_a_repo_that_does_not_want_the_mirror(
    tmp_path: Path, incus: Any, mocker: MockerFixture
) -> None:
    """The spec's headline benefit for non-Docker repos, in the module that
    writes the ACL. Uses real Config/GlobalConfig objects — the module-level
    `cfg`/`gcfg` fixtures are Mocks, so the gate cannot be exercised through
    them."""
    from jailbee import egress_pool
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path / "repo")  # no docker, no extra_registries, no ecr
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path / "registry"),
    )
    compute = mocker.patch("jailbee.docker_daemon.compute_mirror_endpoint")

    assert egress_pool._compute_mirror_endpoint(cfg, incus, gcfg) is None
    compute.assert_not_called()


def test_refresh_all_continues_when_one_repo_refresh_raises(
    db_session: Session,
    gcfg: Any,
    incus: Any,
    frozen_now: datetime,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """One repo's failure must not skip every later repo — nor the trailing
    session.commit() — and must not vanish from the results either: `jailbee
    net refresh` (the systemd unit's ExecStart) derives its exit code from
    these keys, so a dropped one exits 0 on a broken cycle."""
    from jailbee import egress_pool

    for name in ("a", "b"):
        repo = tmp_path / name
        (repo / ".jailbee").mkdir(parents=True)
        (repo / ".jailbee" / "config.yaml").write_text("# placeholder")
        db_session.add(
            RegisteredRepo(
                container_prefix=name.upper(),
                repo_root=str(repo),
                registered_at=frozen_now,
            )
        )
    db_session.commit()

    def fake_load(path: Path) -> Any:
        m = mocker.Mock()
        m.container_prefix = path.parent.parent.name.upper()
        m.repo_root = path.parent.parent
        m.effective_egress_allow.return_value = []
        return m

    mocker.patch("jailbee.egress_pool.load_config", side_effect=fake_load)

    def refresh(cfg_arg: Any, *a: Any, **kw: Any) -> Any:
        if cfg_arg.container_prefix == "A":
            raise ValueError("boom")
        return egress_pool.RefreshResult(container_prefix="B", status="ok")

    mocker.patch.object(egress_pool, "refresh_pool", side_effect=refresh)

    # Spy installed after the setup commits above so only refresh_all's own
    # trailing commit is counted.
    commit = mocker.spy(db_session, "commit")

    results = egress_pool.refresh_all(db_session, gcfg, incus, now=frozen_now)

    assert set(results.keys()) == {"A", "B"}
    assert results["A"].status == "error"
    assert results["A"].error is not None and "boom" in results["A"].error
    assert results["B"].status == "ok"
    # Neither repo's config is missing, so the loop body commits nothing —
    # this call count is the trailing commit and nothing else.
    assert commit.call_count == 1
