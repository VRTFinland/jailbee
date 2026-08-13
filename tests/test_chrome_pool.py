"""Tests for the per-container Chrome profile pool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee import chrome_pool
from jailbee.config import load_config
from jailbee.incus import IncusError

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg(tmp_path: Path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    return cfg.model_copy(update={"shared_dir": tmp_path / "shared"})


def test_ensure_pool_dirs_creates_layout(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    assert (tmp_path / "shared" / "chrome-pool" / "slots").is_dir()
    assert (tmp_path / "shared" / "chrome-pool" / "by-container").is_dir()
    assert (tmp_path / "shared" / "chrome-pool" / ".lock").is_file()


def test_ensure_pool_dirs_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    chrome_pool._ensure_pool_dirs(cfg)  # must not raise
    assert (tmp_path / "shared" / "chrome-pool" / "slots").is_dir()


def _make_slot(slots_dir: Path, name: str, login_data_mtime: float | None = None):
    slot = slots_dir / name
    (slot / "Default").mkdir(parents=True, exist_ok=True)
    if login_data_mtime is not None:
        import os

        login_data = slot / "Default" / "Login Data"
        login_data.write_bytes(b"")
        os.utime(login_data, (login_data_mtime, login_data_mtime))
    return slot


def test_slot_mtime_returns_login_data_mtime(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    slot = _make_slot(chrome_pool._slots_dir(cfg), "slot-0", login_data_mtime=1234.0)
    assert chrome_pool._slot_mtime(slot) == 1234.0


def test_slot_mtime_returns_zero_when_login_data_missing(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    slot = _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    assert chrome_pool._slot_mtime(slot) == 0.0


def test_all_slots_sorted(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-2")
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    _make_slot(chrome_pool._slots_dir(cfg), "slot-1")
    names = [s.name for s in chrome_pool._all_slots(cfg)]
    assert names == ["slot-0", "slot-1", "slot-2"]


def test_free_slots_excludes_allocated(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    s0 = _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    _make_slot(chrome_pool._slots_dir(cfg), "slot-1")
    (chrome_pool._by_container_dir(cfg) / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    free = chrome_pool._free_slots(cfg)
    assert s0 not in free
    assert len(free) == 1
    assert free[0].name == "slot-1"


def test_create_new_slot_picks_smallest_unused(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    _make_slot(chrome_pool._slots_dir(cfg), "slot-2")
    new = chrome_pool._create_new_slot(cfg)
    assert new.name == "slot-1"
    assert new.is_dir()


def test_create_new_slot_starts_at_zero_when_pool_empty(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    new = chrome_pool._create_new_slot(cfg)
    assert new.name == "slot-0"


def test_reconcile_removes_symlinks_for_dead_containers(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    (chrome_pool._by_container_dir(cfg) / "feat-dead").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = []  # no containers exist
    chrome_pool._reconcile(cfg, incus)
    assert not (chrome_pool._by_container_dir(cfg) / "feat-dead").exists()


def test_reconcile_keeps_symlinks_for_alive_containers(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    (chrome_pool._by_container_dir(cfg) / "feat-alive").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-alive"}]
    chrome_pool._reconcile(cfg, incus)
    assert (chrome_pool._by_container_dir(cfg) / "feat-alive").is_symlink()


def test_reconcile_propagates_incus_list_failure(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    (chrome_pool._by_container_dir(cfg) / "feat-x").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.side_effect = IncusError("daemon down")
    with pytest.raises(IncusError):
        chrome_pool._reconcile(cfg, incus)
    # Symlink must NOT be removed when listing fails
    assert (chrome_pool._by_container_dir(cfg) / "feat-x").is_symlink()


def test_rsync_seed_invokes_rsync_with_excludes(tmp_path, mocker):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    run = mocker.patch("jailbee.chrome_pool.subprocess.run")
    chrome_pool._rsync_seed(src, dst)
    run.assert_called_once()
    argv = run.call_args.args[0]
    assert argv[0] == "rsync"
    assert "-a" in argv
    assert "--delete" in argv
    assert any(a == "--exclude=Default/Cache/" for a in argv)
    assert any(a == "--exclude=Singleton*" for a in argv)
    assert argv[-2] == f"{src}/"
    assert argv[-1] == f"{dst}/"


def test_wipe_caches_removes_only_cache_dirs(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    slot = _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    # Create the standard caches and a non-cache file
    (slot / "Default" / "Cache").mkdir(parents=True)
    (slot / "Default" / "Cache" / "f.bin").write_bytes(b"x")
    (slot / "Default" / "Code Cache").mkdir(parents=True)
    (slot / "ShaderCache").mkdir()
    (slot / "Default" / "Login Data").write_bytes(b"keep")
    (slot / "Default" / "Cookies").write_bytes(b"keep")

    chrome_pool._wipe_caches(slot)

    assert not (slot / "Default" / "Cache").exists()
    assert not (slot / "Default" / "Code Cache").exists()
    assert not (slot / "ShaderCache").exists()
    assert (slot / "Default" / "Login Data").read_bytes() == b"keep"
    assert (slot / "Default" / "Cookies").read_bytes() == b"keep"


def test_wipe_caches_silent_if_caches_absent(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    slot = _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    chrome_pool._wipe_caches(slot)  # must not raise


def test_wipe_caches_removes_stale_singleton_locks(tmp_path):
    """Singleton* files block Chrome on the reuse path (target == source)
    where rsync's exclude doesn't run. Release must clean them.
    """
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    slot = _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    (slot / "SingletonLock").write_bytes(b"stale-lock")
    (slot / "SingletonCookie").write_bytes(b"stale-cookie")
    (slot / "SingletonSocket").write_bytes(b"stale-socket")
    (slot / "Default" / "Login Data").write_bytes(b"keep")

    chrome_pool._wipe_caches(slot)

    assert not (slot / "SingletonLock").exists()
    assert not (slot / "SingletonCookie").exists()
    assert not (slot / "SingletonSocket").exists()
    assert (slot / "Default" / "Login Data").read_bytes() == b"keep"


def test_ensure_mount_calls_config_device_add(tmp_path):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    chrome_pool._ensure_mount(cfg, incus, "feat-x", tmp_path / "slot-0")
    incus.config_device_add.assert_called_once_with(
        "feat-x",
        "chrome-profile-slot",
        "disk",
        {
            "source": str(tmp_path / "slot-0"),
            "path": "/home/dev/.config/google-chrome",
        },
    )


def test_try_remove_device_swallows_missing(tmp_path):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    incus.config_device_remove.side_effect = IncusError("Device not found")
    # Must not raise
    chrome_pool._try_remove_device(cfg, incus, "feat-x")


def test_try_remove_device_propagates_other_errors(tmp_path):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    incus.config_device_remove.side_effect = IncusError("Permission denied")
    with pytest.raises(IncusError):
        chrome_pool._try_remove_device(cfg, incus, "feat-x")


def _fake_rsync(mocker):
    """Mock rsync subprocess.run with a side-effect that does cp -r.

    Tests that rsync is called with right args use the mock directly;
    tests that depend on target contents post-copy rely on this fake
    to actually copy the source tree.
    """
    import shutil as _sh

    def _run(argv, *, check=True):
        del check
        # argv[-2] = source/, argv[-1] = target/
        src = argv[-2].rstrip("/")
        dst = argv[-1].rstrip("/")
        if Path(dst).exists():
            _sh.rmtree(dst)
        _sh.copytree(src, dst)
        return mocker.Mock(returncode=0)

    return mocker.patch("jailbee.chrome_pool.subprocess.run", side_effect=_run)


def test_allocate_creates_first_slot(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)
    slot = chrome_pool.allocate(cfg, incus, "feat-foo")
    assert slot.name == "slot-0"
    assert (chrome_pool._by_container_dir(cfg) / "feat-foo").is_symlink()
    incus.config_device_add.assert_called_once()


def test_allocate_seeds_from_existing_slot(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "feat-foo"},
        {"name": "feat-bar"},
    ]
    run = _fake_rsync(mocker)

    # First container creates slot-0 with login data
    slot0 = chrome_pool.allocate(cfg, incus, "feat-foo")
    (slot0 / "Default").mkdir(parents=True, exist_ok=True)
    (slot0 / "Default" / "Login Data").write_bytes(b"first-pwds")

    # Second container should seed from slot-0
    slot1 = chrome_pool.allocate(cfg, incus, "feat-bar")
    assert slot1.name == "slot-1"
    assert (slot1 / "Default" / "Login Data").read_bytes() == b"first-pwds"
    assert run.call_count >= 1


def test_allocate_reuses_oldest_free(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    # Two free slots, slot-0 is older
    s0 = _make_slot(chrome_pool._slots_dir(cfg), "slot-0", login_data_mtime=100.0)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-1", login_data_mtime=200.0)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    slot = chrome_pool.allocate(cfg, incus, "feat-foo")
    # Source = slot-1 (newest), target = slot-0 (oldest free)
    assert slot == s0


def test_allocate_uses_newest_as_source_even_if_active(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    s0 = _make_slot(chrome_pool._slots_dir(cfg), "slot-0", login_data_mtime=100.0)
    s1 = _make_slot(chrome_pool._slots_dir(cfg), "slot-1", login_data_mtime=999.0)
    # slot-1 is allocated to feat-active (it's the newest, and active)
    (chrome_pool._by_container_dir(cfg) / "feat-active").symlink_to(Path("..") / "slots" / "slot-1")
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "feat-active"},
        {"name": "feat-new"},
    ]
    run = _fake_rsync(mocker)

    target = chrome_pool.allocate(cfg, incus, "feat-new")
    # Target = slot-0 (oldest free), source should be slot-1 (newest, active)
    assert target == s0
    rsync_args = run.call_args.args[0]
    assert rsync_args[-2] == f"{s1}/"  # source is slot-1
    assert rsync_args[-1] == f"{s0}/"


def test_allocate_skips_rsync_when_only_slot_is_free(tmp_path, mocker):
    """If the only slot is also the newest, target == source → no copy."""
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0", login_data_mtime=100.0)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    run = _fake_rsync(mocker)

    chrome_pool.allocate(cfg, incus, "feat-foo")
    run.assert_not_called()


def test_allocate_idempotent_for_same_container(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    s1 = chrome_pool.allocate(cfg, incus, "feat-foo")
    s2 = chrome_pool.allocate(cfg, incus, "feat-foo")
    assert s1 == s2
    # device_add called only on first allocation; second is reuse
    assert incus.config_device_add.call_count == 1


def test_allocate_recovers_from_dangling_symlink(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    # Create symlink pointing to non-existent slot
    (chrome_pool._by_container_dir(cfg) / "feat-foo").symlink_to(Path("..") / "slots" / "slot-99")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    slot = chrome_pool.allocate(cfg, incus, "feat-foo")
    # Should have created a fresh slot
    assert slot.exists()
    assert (chrome_pool._by_container_dir(cfg) / "feat-foo").resolve() == slot.resolve()


def test_release_unmounts_and_wipes_caches(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    slot = _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    (slot / "Default" / "Cache").mkdir(parents=True)
    (slot / "Default" / "Cache" / "f").write_bytes(b"x")
    (chrome_pool._by_container_dir(cfg) / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()

    chrome_pool.release(cfg, incus, "feat-foo")

    incus.config_device_remove.assert_called_once_with("feat-foo", "chrome-profile-slot")
    assert not (slot / "Default" / "Cache").exists()
    assert not (chrome_pool._by_container_dir(cfg) / "feat-foo").exists()


def test_release_preserves_login_data(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    slot = _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    (slot / "Default" / "Login Data").write_bytes(b"keep-me")
    (chrome_pool._by_container_dir(cfg) / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    chrome_pool.release(cfg, incus, "feat-foo")
    assert (slot / "Default" / "Login Data").read_bytes() == b"keep-me"


def test_release_no_op_when_no_slot_allocated(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    incus = MagicMock()
    chrome_pool.release(cfg, incus, "feat-never")  # must not raise
    incus.config_device_remove.assert_not_called()


def test_release_swallows_device_remove_error_when_container_gone(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    (chrome_pool._by_container_dir(cfg) / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.config_device_remove.side_effect = IncusError("Device not found")
    chrome_pool.release(cfg, incus, "feat-foo")  # must not raise
    assert not (chrome_pool._by_container_dir(cfg) / "feat-foo").exists()


def test_release_no_op_when_pool_root_missing(tmp_path):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    chrome_pool.release(cfg, incus, "feat-foo")  # must not raise
    incus.config_device_remove.assert_not_called()


def test_list_slots_returns_state_for_each_slot(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0", login_data_mtime=100.0)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-1", login_data_mtime=200.0)
    (chrome_pool._by_container_dir(cfg) / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]

    slots = chrome_pool.list_slots(cfg, incus)
    by_name = {s.name: s for s in slots}
    assert by_name["slot-0"].container == "feat-foo"
    assert by_name["slot-1"].container is None
    assert by_name["slot-0"].login_data_mtime == 100.0
    assert by_name["slot-1"].login_data_mtime == 200.0
    assert by_name["slot-0"].size_bytes >= 0


def test_list_slots_implicitly_reconciles(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    (chrome_pool._by_container_dir(cfg) / "feat-dead").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = []  # feat-dead is gone

    slots = chrome_pool.list_slots(cfg, incus)
    assert slots[0].container is None  # reconciled


def test_prune_removes_only_free_slots(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    _make_slot(chrome_pool._slots_dir(cfg), "slot-0")
    _make_slot(chrome_pool._slots_dir(cfg), "slot-1")
    (chrome_pool._by_container_dir(cfg) / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]

    deleted = chrome_pool.prune(cfg, incus)

    assert deleted == 1
    assert (chrome_pool._slots_dir(cfg) / "slot-0").exists()
    assert not (chrome_pool._slots_dir(cfg) / "slot-1").exists()


def test_prune_returns_zero_when_pool_empty(tmp_path):
    cfg = _cfg(tmp_path)
    chrome_pool._ensure_pool_dirs(cfg)
    incus = MagicMock()
    incus.list_containers.return_value = []
    assert chrome_pool.prune(cfg, incus) == 0


def test_allocate_logs_fresh_when_pool_empty(tmp_path, mocker, capsys):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    chrome_pool.allocate(cfg, incus, "feat-foo")

    out = capsys.readouterr().out
    assert "fresh" in out.lower()
    assert "slot-0" in out
    assert "feat-foo" in out


def test_allocate_logs_seeded_when_source_exists(tmp_path, mocker, capsys):
    cfg = _cfg(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "a"}, {"name": "b"}]
    _fake_rsync(mocker)

    chrome_pool.allocate(cfg, incus, "a")
    capsys.readouterr()  # discard first
    chrome_pool.allocate(cfg, incus, "b")

    out = capsys.readouterr().out
    assert "seeded" in out.lower()
    assert "slot-0" in out  # source
    assert "slot-1" in out  # target


def test_concurrent_allocate_serializes_via_flock(tmp_path, mocker):
    """Two allocations from different threads must end up with distinct
    slots — the flock prevents both from picking the same target.
    """
    import threading

    cfg = _cfg(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "feat-a"},
        {"name": "feat-b"},
    ]
    _fake_rsync(mocker)

    results: dict[str, Path] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = chrome_pool.allocate(cfg, incus, name)

    t1 = threading.Thread(target=worker, args=("feat-a",))
    t2 = threading.Thread(target=worker, args=("feat-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["feat-a"] != results["feat-b"]
    sa = (chrome_pool._by_container_dir(cfg) / "feat-a").resolve()
    sb = (chrome_pool._by_container_dir(cfg) / "feat-b").resolve()
    assert sa != sb
