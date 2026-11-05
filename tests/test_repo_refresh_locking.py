"""Repository removal excludes timer and direct egress refresh side effects."""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, create_engine, select

from jailbee import egress_pool
from jailbee.db import _ensure_schema, state_dir
from jailbee.db.models import PoolIP, RefreshState, RegisteredRepo
from jailbee.incus import Incus
from jailbee.repo_locks import locked
from jailbee.repo_remove import RemovalPlan, execute_removal


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'state.sqlite'}", connect_args={"check_same_thread": False}
    )
    _ensure_schema(engine)
    yield engine
    engine.dispose()


def register(engine, root, prefix="foo"):
    with Session(engine) as session:
        session.add(RegisteredRepo(
            container_prefix=prefix, repo_root=str(root), registered_at=datetime.now(UTC),
            synthetic_config=True,
        ))
        session.commit()


def empty_plan():
    return RemovalPlan("foo", None, [], [], [], [], [], None, {}, [], [], [])


def test_refresh_all_rechecks_snapshot_after_removal(engine, tmp_path, mocker):
    register(engine, tmp_path)
    refresh = mocker.patch.object(egress_pool, "_refresh_pool_locked")
    load = mocker.patch.object(egress_pool, "load_repo_config")
    incus = MagicMock(spec=Incus)

    @contextmanager
    def remove_before_lock(prefix, **kwargs):
        # refresh_all has already loaded its work list, and the caller still
        # has a strong reference to the old ORM row in its identity map.
        with Session(engine) as removal:
            row = removal.get(RegisteredRepo, prefix)
            removal.delete(row)
            removal.commit()
        with locked(prefix, **kwargs) as acquired:
            yield acquired

    mocker.patch.object(egress_pool, "locked", side_effect=remove_before_lock)
    with Session(engine) as stale:
        old = stale.get(RegisteredRepo, "foo")
        assert old is not None
        results = egress_pool.refresh_all(stale, MagicMock(), incus, now=datetime.now(UTC))
    assert results == {}
    refresh.assert_not_called()
    load.assert_not_called()
    assert incus.mock_calls == []


def test_refresh_all_reloads_registration_metadata(engine, tmp_path, mocker):
    register(engine, tmp_path / "old")
    new_root = tmp_path / "new"
    new_root.mkdir()
    cfg = MagicMock(container_prefix="foo")
    load = mocker.patch.object(egress_pool, "load_repo_config", return_value=cfg)
    mocker.patch.object(egress_pool, "_refresh_pool_locked", return_value=egress_pool.RefreshResult(
        container_prefix="foo", status="ok",
    ))
    mocker.patch.object(egress_pool, "check_and_revert_loose")
    mocker.patch.object(egress_pool, "check_and_revert_mounts")
    mocker.patch.object(egress_pool, "check_and_revert_egress")
    mocker.patch.object(egress_pool, "_compute_mirror_endpoint", return_value=None)

    @contextmanager
    def move_before_lock(prefix, **kwargs):
        with Session(engine) as updater:
            row = updater.get(RegisteredRepo, prefix)
            row.repo_root = str(new_root)
            updater.commit()
        with locked(prefix, **kwargs) as acquired:
            yield acquired

    mocker.patch.object(egress_pool, "locked", side_effect=move_before_lock)
    with Session(engine) as stale:
        old = stale.get(RegisteredRepo, "foo")
        assert old is not None
        assert "foo" in egress_pool.refresh_all(
            stale, MagicMock(), MagicMock(spec=Incus), now=datetime.now(UTC)
        )
    load.assert_called_once_with(new_root)


def test_refresh_all_skips_busy_repo_but_refreshes_other(engine, tmp_path, mocker):
    for prefix in ("foo", "bar"):
        register(engine, tmp_path, prefix)
    refreshed = []

    def refresh(repo, *args, **kwargs):
        refreshed.append(repo.container_prefix)
        return {}

    mocker.patch.object(egress_pool, "_refresh_registered_repo", side_effect=refresh)
    with locked("foo"), Session(engine) as session:
        egress_pool.refresh_all(session, MagicMock(), MagicMock(), now=datetime.now(UTC))
    assert refreshed == ["bar"]


def test_direct_refresh_skips_removed_row_in_stale_session(engine, tmp_path, make_cfg, mocker):
    register(engine, tmp_path)
    refresh = mocker.patch.object(egress_pool, "_refresh_pool_locked")
    with Session(engine) as stale:
        old = stale.get(RegisteredRepo, "foo")
        with Session(engine) as removal:
            removal.delete(removal.get(RegisteredRepo, "foo"))
            removal.commit()
        assert old is not None
        result = egress_pool.refresh_pool(
            make_cfg(tmp_path, container_prefix="foo"), MagicMock(), MagicMock(), stale,
            now=datetime.now(UTC),
        )
    assert result.status == "skipped"
    refresh.assert_not_called()
    with Session(engine) as check:
        assert check.exec(select(PoolIP)).all() == []
        assert check.exec(select(RefreshState)).all() == []


def test_removal_holds_lock_through_final_commit(engine, tmp_path, make_cfg, mocker):
    register(engine, tmp_path)
    cfg = make_cfg(tmp_path, container_prefix="foo")
    core = mocker.patch.object(egress_pool, "_refresh_pool_locked")
    incus = MagicMock(spec=Incus)
    incus.profile_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []

    def during_removal(*args):
        with Session(engine) as session:
            # Registry is intentionally still present while removal owns it.
            assert session.get(RegisteredRepo, "foo") is not None
            result = egress_pool.refresh_pool(cfg, MagicMock(), incus, session, now=datetime.now(UTC))
            assert result.status == "skipped"
        with locked("foo", blocking=False) as acquired:
            assert not acquired
        return False

    mocker.patch("jailbee.egress_proxy.drop_fragment", side_effect=during_removal)
    final_commits = []

    class RemovalSession(Session):
        def commit(self):
            if any(isinstance(row, RegisteredRepo) for row in self.deleted):
                with locked("foo", blocking=False) as acquired:
                    assert not acquired
                final_commits.append(True)
            super().commit()

    assert all(result.ok for result in execute_removal(empty_plan(), incus, lambda: RemovalSession(engine)))
    assert final_commits == [True]
    core.assert_not_called()
    with Session(engine) as session:
        assert session.get(RegisteredRepo, "foo") is None
    with locked("foo", blocking=False) as acquired:
        assert acquired


def test_direct_refresh_serializes_before_removal(engine, tmp_path, make_cfg, mocker):
    register(engine, tmp_path)
    acquired, release, removal_started = Event(), Event(), Event()

    def refresh_core(*args, **kwargs):
        acquired.set()
        assert release.wait(5)
        return egress_pool.RefreshResult(container_prefix="foo", status="ok")

    mocker.patch.object(egress_pool, "_refresh_pool_locked", side_effect=refresh_core)
    incus = MagicMock(spec=Incus)
    incus.profile_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    mocker.patch("jailbee.egress_proxy.drop_fragment", return_value=False)
    cfg = make_cfg(tmp_path, container_prefix="foo")

    def refresh():
        with Session(engine) as session:
            return egress_pool.refresh_pool(cfg, MagicMock(), incus, session, now=datetime.now(UTC))

    @contextmanager
    def observed_lock(prefix, *, blocking=True):
        if blocking:
            removal_started.set()
        with locked(prefix, blocking=blocking) as got:
            yield got

    mocker.patch("jailbee.repo_locks.locked", side_effect=observed_lock)

    def remove():
        return execute_removal(empty_plan(), incus, lambda: Session(engine))

    with ThreadPoolExecutor(max_workers=2) as workers:
        refreshing = workers.submit(refresh)
        try:
            assert acquired.wait(5)
            removing = workers.submit(remove)
            assert removal_started.wait(5)
            # A second direct caller also observes the active operation.
            with Session(engine) as session:
                assert egress_pool.refresh_pool(
                    cfg, MagicMock(), incus, session, now=datetime.now(UTC)
                ).status == "skipped"
            incus.profile_list.assert_not_called()
            assert not removing.done()
        finally:
            release.set()
        assert refreshing.result(timeout=5).status == "ok"
        assert all(result.ok for result in removing.result(timeout=5))
    with Session(engine) as session:
        assert session.get(RegisteredRepo, "foo") is None


@pytest.mark.parametrize("acl", ["foo-allowlist", "foo-" + "x" * 53 + "-extra"])
def test_removal_includes_acl_created_after_plan(engine, mocker, acl):
    incus = MagicMock(spec=Incus)
    incus.profile_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = [acl, "bar-allowlist"]
    incus.network_acl_used_by.return_value = []
    mocker.patch("jailbee.egress_proxy.drop_fragment", return_value=False)
    results = execute_removal(empty_plan(), incus, lambda: Session(engine))
    assert all(result.ok for result in results)
    incus.network_acl_delete.assert_called_once_with(acl)


def test_removal_failure_releases_lock_and_keeps_registration(engine, tmp_path, mocker):
    register(engine, tmp_path)
    incus = MagicMock(spec=Incus)
    incus.profile_list.side_effect = RuntimeError("inventory failed")
    mocker.patch("jailbee.egress_proxy.drop_fragment", return_value=False)
    assert any(not result.ok for result in execute_removal(
        empty_plan(), incus, lambda: Session(engine)
    ))
    with locked("foo", blocking=False) as acquired:
        assert acquired
    with Session(engine) as session:
        assert session.get(RegisteredRepo, "foo") is not None


def _hold_repo_lock(acquired, release):
    with locked("foo"):
        acquired.set()
        assert release.wait(10)


def test_repo_lock_cross_process_and_stable_inode():
    ctx = multiprocessing.get_context("spawn")
    acquired, release = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_hold_repo_lock, args=(acquired, release))
    worker.start()
    try:
        assert acquired.wait(10)
        path = state_dir() / "repo-locks/foo.lock"
        inode = path.stat().st_ino
        with locked("foo", blocking=False) as got:
            assert not got
        with locked("bar", blocking=False) as got:
            assert got
        release.set()
        worker.join(10)
        assert worker.exitcode == 0
        with locked("foo"):
            assert path.stat().st_ino == inode
    finally:
        release.set()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join(10)
