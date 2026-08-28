"""Tests for the generic per-container cache pool."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee import pool
from jailbee.config import ChromeConfig, PoolSpec, load_config, load_config_from_text
from jailbee.incus import IncusError

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg(tmp_path: Path):
    """full_config.yaml already sets `chrome.enabled: true`, so this config
    carries the chrome-profile pool and nothing else."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    return cfg.model_copy(update={"shared_dir": tmp_path / "shared"})


def _cfg_gradle(tmp_path: Path):
    """A config with the java stack, hence a pooled `gradle` cache.

    Built through `load_config_from_text`, never
    `model_copy(update={"golden": {...}})`: `model_copy` does not
    validate, so the field would end up holding a raw dict.
    """
    cfg = load_config_from_text("golden:\n  stacks:\n    java: corretto-21\n", tmp_path / "c.yaml")
    return cfg.model_copy(update={"shared_dir": tmp_path / "shared"})


def _pool(tmp_path: Path, **spec_fields) -> pool.Pool:
    """A standalone pool rooted in tmp_path, independent of any Config."""
    return pool.Pool(
        name="gradle",
        root=tmp_path / "shared" / "caches" / "gradle",
        container_path="/home/dev/.gradle",
        spec=PoolSpec(**spec_fields),
    )


def test_device_name_is_derived_from_the_cache_name(tmp_path):
    assert _pool(tmp_path).device_name == "gradle-slot"


def test_a_pool_is_hashable(tmp_path):
    """`Pool` is `frozen=True`, so it gets a generated `__hash__` — which
    raised TypeError on the `PoolSpec` field, because hashing a pydantic
    model with `list[str]` fields is not possible. `field(hash=False)`
    excludes it; `frozen=True` on `PoolSpec` would not have helped."""
    p = _pool(tmp_path, link_paths=["files"], stale_globs=["**/*.lock"])
    assert hash(p) == hash(_pool(tmp_path, link_paths=["files"], stale_globs=["**/*.lock"]))
    assert len({p, p}) == 1


def test_chrome_pool_keeps_its_legacy_device_name(tmp_path):
    """Existing containers carry `chrome-profile-slot`; renaming strands them."""
    p = pool.get(_cfg(tmp_path), "chrome-profile")
    assert p is not None
    assert p.device_name == "chrome-profile-slot"
    assert p.root == tmp_path / "shared" / "chrome-pool"


def test_seed_excludes_link_paths_from_the_copy_pass(tmp_path, mocker):
    run = mocker.patch("jailbee.pool.subprocess.run")
    p = _pool(tmp_path, link_paths=["caches/modules-2/files-2.1"], wipe_paths=["daemon"])
    src = p.slots_dir / "slot-0"
    (src / "caches" / "modules-2" / "files-2.1").mkdir(parents=True)
    dst = p.slots_dir / "slot-1"
    dst.mkdir(parents=True)

    pool._seed(p, src, dst)

    copy_argv = run.call_args_list[0].args[0]
    assert "--exclude=daemon/" in copy_argv
    assert "--exclude=caches/modules-2/files-2.1/" in copy_argv


def test_seed_hardlinks_each_link_path_in_its_own_pass(tmp_path, mocker):
    run = mocker.patch("jailbee.pool.subprocess.run")
    p = _pool(
        tmp_path,
        link_paths=["caches/modules-2/files-2.1", "wrapper/dists"],
        stale_globs=["**/*.lck"],
    )
    src = p.slots_dir / "slot-0"
    (src / "caches" / "modules-2" / "files-2.1").mkdir(parents=True)
    (src / "wrapper" / "dists").mkdir(parents=True)
    dst = p.slots_dir / "slot-1"
    dst.mkdir(parents=True)

    pool._seed(p, src, dst)

    assert run.call_count == 3  # one copy pass + one per link path
    for link_argv in (run.call_args_list[1].args[0], run.call_args_list[2].args[0]):
        # Every link pass carries the stale excludes and --delete-excluded.
        assert "--exclude=**/*.lck" in link_argv
        assert "--delete-excluded" in link_argv
    link_argv = run.call_args_list[1].args[0]
    assert f"--link-dest={src / 'caches/modules-2/files-2.1'}" in link_argv
    assert link_argv[-2] == f"{src / 'caches/modules-2/files-2.1'}/"
    assert link_argv[-1] == f"{dst / 'caches/modules-2/files-2.1'}/"


def test_seed_skips_a_link_path_absent_from_the_source(tmp_path, mocker):
    run = mocker.patch("jailbee.pool.subprocess.run")
    p = _pool(tmp_path, link_paths=["wrapper/dists"])
    src = p.slots_dir / "slot-0"
    src.mkdir(parents=True)
    dst = p.slots_dir / "slot-1"
    dst.mkdir(parents=True)

    pool._seed(p, src, dst)

    assert run.call_count == 1  # copy pass only


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_seed_really_hardlinks_link_paths_and_really_copies_the_rest(tmp_path):
    """The one test that runs rsync for real: argv assertions cannot show
    that two slots share an inode, and that is the whole point of the
    link pass. Local filesystem only — no Incus, no network."""
    p = _pool(tmp_path, link_paths=["files"], stale_globs=["**/*.lock"])
    src = p.slots_dir / "slot-0"
    (src / "files").mkdir(parents=True)
    (src / "files" / "artifact.jar").write_bytes(b"x" * 64)
    (src / "files" / "deep").mkdir()
    # A stale match INSIDE the link path: the hardlink pass must exclude it
    # too. Hardlinking a lock file gives both slots one inode, and fcntl
    # locks are per-inode — exactly the cross-container contention pooling
    # exists to remove.
    (src / "files" / "deep" / "inner.lock").write_bytes(b"")
    (src / "mutable.bin").write_bytes(b"y" * 64)
    (src / "stale.lock").write_bytes(b"")
    dst = p.slots_dir / "slot-1"
    (dst / "files").mkdir(parents=True)
    # A lock left in a reused free slot whose release never ran: seeding
    # must clear it (`--delete-excluded`), not leave it behind.
    (dst / "files" / "leftover.lock").write_bytes(b"")

    pool._seed(p, src, dst)

    linked = (src / "files" / "artifact.jar").stat()
    copied = (dst / "files" / "artifact.jar").stat()
    assert linked.st_ino == copied.st_ino  # one inode, two names

    assert (src / "mutable.bin").stat().st_ino != (dst / "mutable.bin").stat().st_ino
    assert not (dst / "stale.lock").exists()  # stale globs never seed
    assert not (dst / "files" / "deep" / "inner.lock").exists()  # ...inside a link path either
    assert not (dst / "files" / "leftover.lock").exists()  # nor survive in a reused slot


def test_warmth_falls_back_to_slot_mtime_without_a_warmth_file(tmp_path):
    p = _pool(tmp_path)
    slot = p.slots_dir / "slot-0"
    slot.mkdir(parents=True)
    os.utime(slot, (1000.0, 1000.0))
    assert pool._slot_mtime(p, slot) == 1000.0


def test_wipe_removes_wipe_paths_and_stale_globs_recursively(tmp_path):
    p = _pool(tmp_path, wipe_paths=["daemon"], stale_globs=["**/*.lock"])
    slot = p.slots_dir / "slot-0"
    (slot / "daemon" / "8.14").mkdir(parents=True)
    (slot / "caches" / "modules-2").mkdir(parents=True)
    (slot / "caches" / "modules-2" / "gc.lock").write_text("x")
    (slot / "caches" / "keep.bin").write_text("x")

    pool._wipe(p, slot)

    assert not (slot / "daemon").exists()
    assert not (slot / "caches" / "modules-2" / "gc.lock").exists()
    assert (slot / "caches" / "keep.bin").exists()


def test_pools_for_returns_only_pooled_caches_with_expanded_paths(tmp_path):
    cfg = _cfg(tmp_path)
    names = {p.name for p in pool.pools_for(cfg)}
    assert "ssh" not in names  # a plain shared cache is not a pool
    for p in pool.pools_for(cfg):
        assert p.container_path.startswith("/")  # `~` expanded


def test_allocate_startup_skips_on_demand_pools(tmp_path, mocker):
    cfg = _cfg(tmp_path)  # chrome-profile is the only pool, and it is on-demand
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "c1"}]
    mocker.patch("jailbee.pool.subprocess.run")

    pool.allocate_startup(cfg, incus, "c1")

    added = {call.args[1] for call in incus.config_device_add.call_args_list}
    assert "chrome-profile-slot" not in added


def test_allocate_startup_attaches_every_on_start_pool(tmp_path, mocker):
    """Positive path: gradle+m2 (on-start) get slots; chrome-profile (on-demand)
    is skipped. A body reduced to `return`, or a filter bug dropping every
    pool, would pass `test_allocate_startup_skips_on_demand_pools` (it only
    asserts the negative) but fails the assertions below.
    """
    cfg = _cfg_gradle(tmp_path).model_copy(update={"chrome": ChromeConfig(enabled=True)})
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "c1"}]
    mocker.patch("jailbee.pool.subprocess.run")

    pool.allocate_startup(cfg, incus, "c1")

    added = {call.args[1] for call in incus.config_device_add.call_args_list}
    assert "gradle-slot" in added
    assert "m2-slot" in added
    assert "chrome-profile-slot" not in added


def test_allocate_skips_seed_when_pool_spec_disables_it(tmp_path, mocker, capsys):
    """`seed=False` must skip the rsync copy (dropping `and pool.spec.seed`
    from the seed condition would still call rsync here, since target !=
    source), but the symlink and mount are still created — and the log
    message must not falsely claim a seed that never happened.
    """
    cfg = _cfg(tmp_path)  # built before patching subprocess.run: load_config shells
    # out to `git` itself, and patching `jailbee.pool.subprocess.run` patches the
    # `run` attribute on the shared `subprocess` module process-wide.
    p = _pool(tmp_path, seed=False)
    older = p.slots_dir / "slot-0"
    newer = p.slots_dir / "slot-1"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    os.utime(older, (100.0, 100.0))
    os.utime(newer, (200.0, 200.0))
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "c1"}]
    run = mocker.patch("jailbee.pool.subprocess.run")

    target = pool.allocate(cfg, incus, p, "c1")

    assert target == older  # oldest free slot picked as target; newer is source
    run.assert_not_called()  # seed disabled: no rsync at all
    assert (p.by_container_dir / "c1").is_symlink()
    incus.config_device_add.assert_called_once()

    out = capsys.readouterr().out
    assert "seeded" not in out.lower()  # nothing was actually seeded
    assert "seed disabled" in out.lower()


def test_release_all_releases_every_pool(tmp_path, mocker):
    """Two pools at once: gradle (on-start) and chrome-profile (on-demand)."""
    cfg = _cfg_gradle(tmp_path).model_copy(update={"chrome": ChromeConfig(enabled=True)})
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "c1"}]
    mocker.patch("jailbee.pool.subprocess.run")
    for p in pool.pools_for(cfg):
        pool.ensure_pool_dirs(cfg, p)
        pool.allocate(cfg, incus, p, "c1")
    assert len(pool.pools_for(cfg)) == 3  # gradle, m2, chrome-profile

    pool.release_all(cfg, incus, "c1")

    for p in pool.pools_for(cfg):
        assert not (p.by_container_dir / "c1").exists()


def test_ensure_pools_creates_layout_for_every_pool(tmp_path):
    cfg = _cfg_gradle(tmp_path).model_copy(update={"chrome": ChromeConfig(enabled=True)})

    pool.ensure_pools(cfg)

    for p in pool.pools_for(cfg):
        assert p.slots_dir.is_dir()
        assert p.by_container_dir.is_dir()


def _pollute(p: pool.Pool) -> None:
    """Make `ensure_pool_dirs` raise for this pool: loose content *and*
    an existing slot-0, so it refuses to guess which is the real cache."""
    (p.slots_dir / "slot-0").mkdir(parents=True)
    (p.root / "caches").mkdir(parents=True)


def test_ensure_pools_strict_propagates_the_first_pool_error(tmp_path):
    """`init` must stop: a fresh repo with a polluted pool root is a
    situation to look at, not to warn past."""
    cfg = _cfg_gradle(tmp_path)
    pools = pool.pools_for(cfg)
    _pollute(pools[0])

    with pytest.raises(pool.PoolError):
        pool.ensure_pools(cfg)


def test_ensure_pools_non_strict_warns_and_keeps_going(tmp_path, capsys):
    """`apply` must not be wedged by one polluted pool root: it still has
    profiles, the ACL and the port forwards to write."""
    cfg = _cfg_gradle(tmp_path)
    pools = pool.pools_for(cfg)
    assert len(pools) > 1  # otherwise "keeps going" proves nothing
    _pollute(pools[0])

    pool.ensure_pools(cfg, strict=False)

    # Rich wraps at the console width, so compare on collapsed whitespace.
    out = " ".join(capsys.readouterr().out.split())
    assert f"pool {pools[0].name}" in out
    assert "loose cache content" in out
    for later in pools[1:]:
        assert later.slots_dir.is_dir()
        assert later.by_container_dir.is_dir()


def test_ensure_pool_dirs_migrates_a_pre_pool_cache_into_slot_0(tmp_path):
    cfg = _cfg(tmp_path)
    p = _pool(tmp_path)
    (p.root / "caches" / "modules-2").mkdir(parents=True)
    (p.root / "caches" / "modules-2" / "artifact.jar").write_text("x")

    pool.ensure_pool_dirs(cfg, p)

    assert (p.slots_dir / "slot-0" / "caches" / "modules-2" / "artifact.jar").is_file()
    assert not (p.root / "caches").exists()
    assert p.by_container_dir.is_dir()


def test_ensure_pool_dirs_refuses_to_guess_when_slot_0_exists(tmp_path):
    cfg = _cfg(tmp_path)
    p = _pool(tmp_path)
    (p.slots_dir / "slot-0").mkdir(parents=True)
    (p.root / "caches").mkdir(parents=True)

    with pytest.raises(pool.PoolError, match="loose cache content"):
        pool.ensure_pool_dirs(cfg, p)


def test_ensure_pool_dirs_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    p = _pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    pool.ensure_pool_dirs(cfg, p)
    assert p.slots_dir.is_dir()
    assert not (p.slots_dir / "slot-0").exists()


def test_unique_bytes_counts_a_shared_inode_once(tmp_path):
    p = _pool(tmp_path)
    a = p.slots_dir / "slot-0"
    b = p.slots_dir / "slot-1"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "artifact.jar").write_bytes(b"x" * 100)
    os.link(a / "artifact.jar", b / "artifact.jar")

    assert pool.unique_bytes(p) == 100


# --- Ported from tests/test_chrome_pool.py -------------------------------
#
# Each test below is rewritten to operate on a `Pool` (built with `_pool`,
# or `pool.get(cfg, "chrome-profile")` where the assertion is specifically
# about Chrome's own values) instead of a bare `Config`. `login_data_mtime`
# becomes `warmth_mtime`, writing `pool.spec.warmth_file` when the spec has
# one and touching the slot directory otherwise.


def _make_slot(p: pool.Pool, name: str, warmth_mtime: float | None = None) -> Path:
    slot = p.slots_dir / name
    (slot / "Default").mkdir(parents=True, exist_ok=True)
    if warmth_mtime is not None:
        if p.spec.warmth_file is not None:
            warmth = slot / p.spec.warmth_file
            warmth.parent.mkdir(parents=True, exist_ok=True)
            warmth.write_bytes(b"")
            os.utime(warmth, (warmth_mtime, warmth_mtime))
        else:
            os.utime(slot, (warmth_mtime, warmth_mtime))
    return slot


def _chrome_pool(tmp_path: Path) -> pool.Pool:
    p = pool.get(_cfg(tmp_path), "chrome-profile")
    assert p is not None
    return p


def test_ensure_pool_dirs_creates_layout(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    assert (tmp_path / "shared" / "chrome-pool" / "slots").is_dir()
    assert (tmp_path / "shared" / "chrome-pool" / "by-container").is_dir()
    assert (tmp_path / "shared" / "chrome-pool" / ".lock").is_file()


def test_ensure_pool_dirs_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    pool.ensure_pool_dirs(cfg, p)  # must not raise
    assert (tmp_path / "shared" / "chrome-pool" / "slots").is_dir()


def test_slot_mtime_returns_warmth_file_mtime(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    slot = _make_slot(p, "slot-0", warmth_mtime=1234.0)
    assert pool._slot_mtime(p, slot) == 1234.0


def test_slot_mtime_returns_zero_when_warmth_file_missing(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    slot = _make_slot(p, "slot-0")
    assert pool._slot_mtime(p, slot) == 0.0


def test_all_slots_sorted(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    _make_slot(p, "slot-2")
    _make_slot(p, "slot-0")
    _make_slot(p, "slot-1")
    names = [s.name for s in pool._all_slots(p)]
    assert names == ["slot-0", "slot-1", "slot-2"]


def test_free_slots_excludes_allocated(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    s0 = _make_slot(p, "slot-0")
    _make_slot(p, "slot-1")
    (p.by_container_dir / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    free = pool._free_slots(p)
    assert s0 not in free
    assert len(free) == 1
    assert free[0].name == "slot-1"


def test_create_new_slot_picks_smallest_unused(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    _make_slot(p, "slot-0")
    _make_slot(p, "slot-2")
    new = pool._create_new_slot(p)
    assert new.name == "slot-1"
    assert new.is_dir()


def test_create_new_slot_starts_at_zero_when_pool_empty(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    new = pool._create_new_slot(p)
    assert new.name == "slot-0"


def test_reconcile_removes_symlinks_for_dead_containers(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    _make_slot(p, "slot-0")
    (p.by_container_dir / "feat-dead").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = []  # no containers exist
    pool._reconcile(p, incus)
    assert not (p.by_container_dir / "feat-dead").exists()


def test_reconcile_keeps_symlinks_for_alive_containers(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    _make_slot(p, "slot-0")
    (p.by_container_dir / "feat-alive").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-alive"}]
    pool._reconcile(p, incus)
    assert (p.by_container_dir / "feat-alive").is_symlink()


def test_reconcile_propagates_incus_list_failure(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    _make_slot(p, "slot-0")
    (p.by_container_dir / "feat-x").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.side_effect = IncusError("daemon down")
    with pytest.raises(IncusError):
        pool._reconcile(p, incus)
    # Symlink must NOT be removed when listing fails
    assert (p.by_container_dir / "feat-x").is_symlink()


def test_rsync_seed_invokes_rsync_with_excludes(tmp_path, mocker):
    p = _chrome_pool(tmp_path)
    src = p.slots_dir / "slot-0"
    dst = p.slots_dir / "slot-1"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    run = mocker.patch("jailbee.pool.subprocess.run")
    pool._seed(p, src, dst)
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
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    slot = _make_slot(p, "slot-0")
    # Create the standard caches and a non-cache file
    (slot / "Default" / "Cache").mkdir(parents=True)
    (slot / "Default" / "Cache" / "f.bin").write_bytes(b"x")
    (slot / "Default" / "Code Cache").mkdir(parents=True)
    (slot / "ShaderCache").mkdir()
    (slot / "Default" / "Login Data").write_bytes(b"keep")
    (slot / "Default" / "Cookies").write_bytes(b"keep")

    pool._wipe(p, slot)

    assert not (slot / "Default" / "Cache").exists()
    assert not (slot / "Default" / "Code Cache").exists()
    assert not (slot / "ShaderCache").exists()
    assert (slot / "Default" / "Login Data").read_bytes() == b"keep"
    assert (slot / "Default" / "Cookies").read_bytes() == b"keep"


def test_wipe_caches_silent_if_caches_absent(tmp_path):
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    slot = _make_slot(p, "slot-0")
    pool._wipe(p, slot)  # must not raise


def test_wipe_caches_removes_stale_singleton_locks(tmp_path):
    """Singleton* files block Chrome on the reuse path (target == source)
    where rsync's exclude doesn't run. Release must clean them.
    """
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(_cfg(tmp_path), p)
    slot = _make_slot(p, "slot-0")
    (slot / "SingletonLock").write_bytes(b"stale-lock")
    (slot / "SingletonCookie").write_bytes(b"stale-cookie")
    (slot / "SingletonSocket").write_bytes(b"stale-socket")
    (slot / "Default" / "Login Data").write_bytes(b"keep")

    pool._wipe(p, slot)

    assert not (slot / "SingletonLock").exists()
    assert not (slot / "SingletonCookie").exists()
    assert not (slot / "SingletonSocket").exists()
    assert (slot / "Default" / "Login Data").read_bytes() == b"keep"


def test_ensure_mount_calls_config_device_add(tmp_path):
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    pool._ensure_mount(incus, p, "feat-x", tmp_path / "slot-0")
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
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    incus.config_device_remove.side_effect = IncusError("Device not found")
    # Must not raise
    pool._try_remove_device(incus, p, "feat-x")


def test_try_remove_device_propagates_other_errors(tmp_path):
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    incus.config_device_remove.side_effect = IncusError("Permission denied")
    with pytest.raises(IncusError):
        pool._try_remove_device(incus, p, "feat-x")


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

    return mocker.patch("jailbee.pool.subprocess.run", side_effect=_run)


def test_allocate_creates_first_slot(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)
    slot = pool.allocate(cfg, incus, p, "feat-foo")
    assert slot.name == "slot-0"
    assert (p.by_container_dir / "feat-foo").is_symlink()
    incus.config_device_add.assert_called_once()


def test_allocate_seeds_from_existing_slot(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "feat-foo"},
        {"name": "feat-bar"},
    ]
    run = _fake_rsync(mocker)

    # First container creates slot-0 with login data
    slot0 = pool.allocate(cfg, incus, p, "feat-foo")
    (slot0 / "Default").mkdir(parents=True, exist_ok=True)
    (slot0 / "Default" / "Login Data").write_bytes(b"first-pwds")

    # Second container should seed from slot-0
    slot1 = pool.allocate(cfg, incus, p, "feat-bar")
    assert slot1.name == "slot-1"
    assert (slot1 / "Default" / "Login Data").read_bytes() == b"first-pwds"
    assert run.call_count >= 1


def test_allocate_reuses_oldest_free(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    # Two free slots, slot-0 is older
    s0 = _make_slot(p, "slot-0", warmth_mtime=100.0)
    _make_slot(p, "slot-1", warmth_mtime=200.0)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    slot = pool.allocate(cfg, incus, p, "feat-foo")
    # Source = slot-1 (newest), target = slot-0 (oldest free)
    assert slot == s0


def test_allocate_uses_newest_as_source_even_if_active(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    s0 = _make_slot(p, "slot-0", warmth_mtime=100.0)
    s1 = _make_slot(p, "slot-1", warmth_mtime=999.0)
    # slot-1 is allocated to feat-active (it's the newest, and active)
    (p.by_container_dir / "feat-active").symlink_to(Path("..") / "slots" / "slot-1")
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "feat-active"},
        {"name": "feat-new"},
    ]
    run = _fake_rsync(mocker)

    target = pool.allocate(cfg, incus, p, "feat-new")
    # Target = slot-0 (oldest free), source should be slot-1 (newest, active)
    assert target == s0
    rsync_args = run.call_args.args[0]
    assert rsync_args[-2] == f"{s1}/"  # source is slot-1
    assert rsync_args[-1] == f"{s0}/"


def test_allocate_skips_rsync_when_only_slot_is_free(tmp_path, mocker):
    """If the only slot is also the newest, target == source → no copy."""
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    _make_slot(p, "slot-0", warmth_mtime=100.0)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    run = _fake_rsync(mocker)

    pool.allocate(cfg, incus, p, "feat-foo")
    run.assert_not_called()


def test_allocate_idempotent_for_same_container(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    s1 = pool.allocate(cfg, incus, p, "feat-foo")
    s2 = pool.allocate(cfg, incus, p, "feat-foo")
    assert s1 == s2
    # device_add called only on first allocation; second is reuse
    assert incus.config_device_add.call_count == 1


def test_allocate_recovers_from_dangling_symlink(tmp_path, mocker):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    # Create symlink pointing to non-existent slot
    (p.by_container_dir / "feat-foo").symlink_to(Path("..") / "slots" / "slot-99")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    slot = pool.allocate(cfg, incus, p, "feat-foo")
    # Should have created a fresh slot
    assert slot.exists()
    assert (p.by_container_dir / "feat-foo").resolve() == slot.resolve()


def test_allocate_recovers_when_the_stale_device_is_still_attached(tmp_path, mocker):
    """A slot deleted out of band leaves the symlink dangling AND
    `<name>-slot` still attached. `incus config device add` errors on an
    existing device, so without dropping it first the recovery branch
    propagates out of `allocate_startup` into `boot_container` — which has
    no guard — and `jb start`/`shell`/`restart` fails with an Incus error
    the user cannot clear without hand-removing the device.
    """
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    (p.by_container_dir / "feat-foo").symlink_to(Path("..") / "slots" / "slot-99")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    attached = {p.device_name}

    def _add(_container, device, _dtype, _props):
        if device in attached:
            raise IncusError(f"Device {device} already exists")
        attached.add(device)

    def _remove(_container, device):
        if device not in attached:
            raise IncusError(f"Device {device} not found")
        attached.discard(device)

    incus.config_device_add.side_effect = _add
    incus.config_device_remove.side_effect = _remove
    _fake_rsync(mocker)

    slot = pool.allocate(cfg, incus, p, "feat-foo")

    assert slot.exists()
    assert (p.by_container_dir / "feat-foo").resolve() == slot.resolve()
    incus.config_device_remove.assert_called_once_with("feat-foo", p.device_name)


def test_allocate_does_not_drop_the_device_on_a_first_allocation(tmp_path, mocker):
    """The stale-device removal is scoped to the re-allocate path: a normal
    first allocation must not touch devices at all."""
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    pool.allocate(cfg, incus, p, "feat-foo")

    incus.config_device_remove.assert_not_called()


def test_release_unmounts_and_wipes_caches(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    slot = _make_slot(p, "slot-0")
    (slot / "Default" / "Cache").mkdir(parents=True)
    (slot / "Default" / "Cache" / "f").write_bytes(b"x")
    (p.by_container_dir / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()

    pool.release(cfg, incus, p, "feat-foo")

    incus.config_device_remove.assert_called_once_with("feat-foo", "chrome-profile-slot")
    assert not (slot / "Default" / "Cache").exists()
    assert not (p.by_container_dir / "feat-foo").exists()


def test_release_preserves_login_data(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    slot = _make_slot(p, "slot-0")
    (slot / "Default" / "Login Data").write_bytes(b"keep-me")
    (p.by_container_dir / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    pool.release(cfg, incus, p, "feat-foo")
    assert (slot / "Default" / "Login Data").read_bytes() == b"keep-me"


def test_release_no_op_when_no_slot_allocated(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    incus = MagicMock()
    pool.release(cfg, incus, p, "feat-never")  # must not raise
    incus.config_device_remove.assert_not_called()


def test_release_swallows_device_remove_error_when_container_gone(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    _make_slot(p, "slot-0")
    (p.by_container_dir / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.config_device_remove.side_effect = IncusError("Device not found")
    pool.release(cfg, incus, p, "feat-foo")  # must not raise
    assert not (p.by_container_dir / "feat-foo").exists()


def test_release_no_op_when_pool_root_missing(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    pool.release(cfg, incus, p, "feat-foo")  # must not raise
    incus.config_device_remove.assert_not_called()


def test_list_slots_returns_state_for_each_slot(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    _make_slot(p, "slot-0", warmth_mtime=100.0)
    _make_slot(p, "slot-1", warmth_mtime=200.0)
    (p.by_container_dir / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]

    slots = pool.list_slots(cfg, incus, p)
    by_name = {s.name: s for s in slots}
    assert by_name["slot-0"].container == "feat-foo"
    assert by_name["slot-1"].container is None
    assert by_name["slot-0"].warmth_mtime == 100.0
    assert by_name["slot-1"].warmth_mtime == 200.0
    assert by_name["slot-0"].size_bytes >= 0
    assert by_name["slot-0"].pool == "chrome-profile"


def test_list_slots_implicitly_reconciles(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    _make_slot(p, "slot-0")
    (p.by_container_dir / "feat-dead").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = []  # feat-dead is gone

    slots = pool.list_slots(cfg, incus, p)
    assert slots[0].container is None  # reconciled


def test_prune_removes_only_free_slots(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    _make_slot(p, "slot-0")
    _make_slot(p, "slot-1")
    (p.by_container_dir / "feat-foo").symlink_to(Path("..") / "slots" / "slot-0")
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]

    deleted = pool.prune(cfg, incus, p)

    assert deleted == 1
    assert (p.slots_dir / "slot-0").exists()
    assert not (p.slots_dir / "slot-1").exists()


def test_prune_returns_zero_when_pool_empty(tmp_path):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    pool.ensure_pool_dirs(cfg, p)
    incus = MagicMock()
    incus.list_containers.return_value = []
    assert pool.prune(cfg, incus, p) == 0


def test_allocate_logs_fresh_when_pool_empty(tmp_path, mocker, capsys):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-foo"}]
    _fake_rsync(mocker)

    pool.allocate(cfg, incus, p, "feat-foo")

    out = capsys.readouterr().out
    assert "fresh" in out.lower()
    assert "slot-0" in out
    assert "feat-foo" in out


def test_allocate_logs_seeded_when_source_exists(tmp_path, mocker, capsys):
    cfg = _cfg(tmp_path)
    p = _chrome_pool(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "a"}, {"name": "b"}]
    _fake_rsync(mocker)

    pool.allocate(cfg, incus, p, "a")
    capsys.readouterr()  # discard first
    pool.allocate(cfg, incus, p, "b")

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
    p = _chrome_pool(tmp_path)
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
        results[name] = pool.allocate(cfg, incus, p, name)

    t1 = threading.Thread(target=worker, args=("feat-a",))
    t2 = threading.Thread(target=worker, args=("feat-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["feat-a"] != results["feat-b"]
    sa = (p.by_container_dir / "feat-a").resolve()
    sb = (p.by_container_dir / "feat-b").resolve()
    assert sa != sb
