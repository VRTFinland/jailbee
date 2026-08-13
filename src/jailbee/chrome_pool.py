"""Per-container Chrome profile pool.

A pool of Chrome profile directories on the host. Each running container
that opens Chrome gets its own slot from the pool, avoiding Chrome's
SingletonLock collisions when multiple containers run Chrome concurrently.

This module is the only place that mutates `<shared_dir>/chrome-pool/`
(default: `$XDG_DATA_HOME/jailbee/shared/<repo>/chrome-pool/`).
"""

from __future__ import annotations

import fcntl
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from jailbee.config import CONTAINER_USERNAME, Config
from jailbee.incus import Incus, IncusError
from jailbee.tui import info

# Chrome cache directories — wiped on release, excluded from rsync seed.
# Defined as relative paths under a slot dir; same list used in both
# operations so they cannot drift.
_CACHE_RELATIVE = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/Service Worker/CacheStorage",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "ShaderCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    # Top-level regenerable data sets — large, redownloaded on demand,
    # not user state. Without these the freed slot stays at ~80 MB.
    "Safe Browsing",
    "optimization_guide_model_store",
    "BrowserMetrics",
)

# Constant device name for the per-container slot mount.
_DEVICE_NAME = "chrome-profile-slot"


def _container_chrome_path(cfg: Config) -> str:
    del cfg  # username is now hardcoded; signature kept for call-site compatibility
    return f"/home/{CONTAINER_USERNAME}/.config/google-chrome"


@dataclass
class SlotInfo:
    """Information about one slot for `jailbee chrome pool ls`."""

    name: str
    path: Path
    container: str | None
    login_data_mtime: float | None
    size_bytes: int


def _pool_root(cfg: Config) -> Path:
    assert cfg.shared_dir is not None  # set by load_config
    return cfg.shared_dir / "chrome-pool"


def _slots_dir(cfg: Config) -> Path:
    return _pool_root(cfg) / "slots"


def _by_container_dir(cfg: Config) -> Path:
    return _pool_root(cfg) / "by-container"


def _lock_path(cfg: Config) -> Path:
    return _pool_root(cfg) / ".lock"


def _ensure_pool_dirs(cfg: Config) -> None:
    """Create pool layout if missing. Idempotent."""
    _slots_dir(cfg).mkdir(parents=True, exist_ok=True)
    _by_container_dir(cfg).mkdir(parents=True, exist_ok=True)
    lock = _lock_path(cfg)
    if not lock.exists():
        lock.touch()


@contextmanager
def _lock(cfg: Config) -> Iterator[IO[bytes]]:
    """Acquire an exclusive flock on the pool root.

    The lock auto-releases on process exit (kernel-managed), so a kill
    -9 mid-rsync leaves no stale lock.
    """
    fp = open(_lock_path(cfg), "wb")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield fp
    finally:
        fp.close()


def _slot_mtime(slot: Path) -> float:
    """Return the mtime of `Default/Login Data`, or 0.0 if missing.

    `Default/Login Data` is Chrome's password SQLite. rsync -a preserves
    mtimes through copies, so this points at the most recent real user
    activity in any slot, not the time of the last seed-copy.
    """
    login_data = slot / "Default" / "Login Data"
    if not login_data.exists():
        return 0.0
    return login_data.stat().st_mtime


def _all_slots(cfg: Config) -> list[Path]:
    """Return all slot directories sorted by name."""
    return sorted(p for p in _slots_dir(cfg).glob("slot-*") if p.is_dir())


def _free_slots(cfg: Config) -> list[Path]:
    """Return slots with no incoming `by-container/` symlink."""
    by_c = _by_container_dir(cfg)
    allocated = {sym.resolve() for sym in by_c.iterdir() if sym.is_symlink()}
    return [s for s in _all_slots(cfg) if s.resolve() not in allocated]


def _create_new_slot(cfg: Config) -> Path:
    """Create slot-N where N is the smallest non-negative int not in use."""
    used = {int(s.name.removeprefix("slot-")) for s in _all_slots(cfg)}
    n = 0
    while n in used:
        n += 1
    new_slot = _slots_dir(cfg) / f"slot-{n}"
    new_slot.mkdir()
    return new_slot


def _reconcile(cfg: Config, incus: Incus) -> None:
    """Drop `by-container/<name>` symlinks for containers that no longer exist.

    Propagates `IncusError` from `list_containers` — never delete symlinks
    when Incus state is unknown.
    """
    by_c = _by_container_dir(cfg)
    if not by_c.exists():
        return
    existing = {c["name"] for c in incus.list_containers()}
    for symlink in by_c.iterdir():
        if symlink.name not in existing:
            symlink.unlink()


_RSYNC_EXCLUDES: tuple[str, ...] = (
    *(f"--exclude={p}/" for p in _CACHE_RELATIVE),
    "--exclude=Singleton*",
)


def _rsync_seed(source: Path, target: Path) -> None:
    """Copy source/ → target/ excluding cache dirs and Chrome singleton locks."""
    subprocess.run(
        ["rsync", "-a", "--delete", *_RSYNC_EXCLUDES, f"{source}/", f"{target}/"],
        check=True,
    )


def _wipe_caches(slot: Path) -> None:
    """Remove cache dirs and stale Chrome singleton locks from `slot`.

    Idempotent. The Singleton* files are how Chrome detects "another
    instance running on this profile" — they remain when Chrome exits
    uncleanly (container destroy, kill -9). Leaving them in a freed slot
    breaks the next allocator on the reuse path (target == source) where
    rsync's exclude doesn't run. Always wipe at release.
    """
    for rel in _CACHE_RELATIVE:
        shutil.rmtree(slot / rel, ignore_errors=True)
    for orphan in (*slot.glob("Singleton*"), *slot.glob("BrowserMetrics-*.pma")):
        try:
            orphan.unlink()
        except OSError:
            pass


def _ensure_mount(cfg: Config, incus: Incus, container: str, slot: Path) -> None:
    """Add the slot disk device to the container.

    Note: `incus config device add` errors if the device already exists.
    Callers that re-allocate for the same container must skip this call;
    `allocate` enforces that via the idempotent-symlink check.
    """
    incus.config_device_add(
        container,
        _DEVICE_NAME,
        "disk",
        {
            "source": str(slot),
            "path": _container_chrome_path(cfg),
        },
    )


_DEVICE_MISSING_MARKERS = ("not found", "doesn't exist", "no such")


def _try_remove_device(cfg: Config, incus: Incus, container: str) -> None:
    """Remove the slot device, silently absorbing 'already gone' errors."""
    del cfg  # signature parity with _ensure_mount
    try:
        incus.config_device_remove(container, _DEVICE_NAME)
    except IncusError as e:
        msg = str(e).lower()
        if any(marker in msg for marker in _DEVICE_MISSING_MARKERS):
            return
        raise


def allocate(cfg: Config, incus: Incus, container: str) -> Path:
    """Allocate a slot for `container`, mount it, return host slot path.

    Idempotent: re-calling for a container that already has a slot
    returns the existing slot without re-allocating, copying, or
    re-mounting.
    """
    _ensure_pool_dirs(cfg)
    with _lock(cfg):
        _reconcile(cfg, incus)

        symlink = _by_container_dir(cfg) / container
        if symlink.is_symlink():
            target = symlink.resolve()
            if target.exists():
                return target
            # Dangling — clean up, fall through to re-allocate
            symlink.unlink()

        all_slots = _all_slots(cfg)
        free = _free_slots(cfg)
        source = max(all_slots, key=_slot_mtime, default=None)

        if free:
            target = min(free, key=_slot_mtime)
        else:
            target = _create_new_slot(cfg)

        if source is not None and target != source:
            _rsync_seed(source, target)

        symlink.symlink_to(Path("..") / "slots" / target.name)
        try:
            _ensure_mount(cfg, incus, container, target)
        except Exception:
            symlink.unlink()
            raise

        if source is None:
            info(f"Allocated fresh {target.name} for {container}")
        elif target == source:
            info(f"Reusing {target.name} for {container}")
        else:
            info(f"Allocated {target.name} for {container} (seeded from {source.name})")
        return target


def release(cfg: Config, incus: Incus, container: str) -> None:
    """Release `container`'s slot: unmount, wipe caches, drop symlink.

    No-op if `container` has no slot allocated, or the pool root doesn't
    exist yet.
    """
    pool = _pool_root(cfg)
    if not pool.exists():
        return
    with _lock(cfg):
        symlink = _by_container_dir(cfg) / container
        if not symlink.is_symlink():
            return
        slot = symlink.resolve()
        _try_remove_device(cfg, incus, container)
        if slot.exists():
            _wipe_caches(slot)
        symlink.unlink()


def _slot_size_bytes(slot: Path) -> int:
    total = 0
    for p in slot.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _container_for_slot(cfg: Config, slot: Path) -> str | None:
    for sym in _by_container_dir(cfg).iterdir():
        if sym.is_symlink() and sym.resolve() == slot.resolve():
            return sym.name
    return None


def list_slots(cfg: Config, incus: Incus) -> list[SlotInfo]:
    """Return every slot's state. Implicitly reconciles first."""
    _ensure_pool_dirs(cfg)
    with _lock(cfg):
        _reconcile(cfg, incus)
        infos: list[SlotInfo] = []
        for slot in _all_slots(cfg):
            mtime = _slot_mtime(slot)
            infos.append(
                SlotInfo(
                    name=slot.name,
                    path=slot,
                    container=_container_for_slot(cfg, slot),
                    login_data_mtime=mtime if mtime > 0.0 else None,
                    size_bytes=_slot_size_bytes(slot),
                )
            )
        return infos


def prune(cfg: Config, incus: Incus) -> int:
    """Delete every unallocated slot. Returns count deleted."""
    _ensure_pool_dirs(cfg)
    with _lock(cfg):
        _reconcile(cfg, incus)
        free = _free_slots(cfg)
        for slot in free:
            shutil.rmtree(slot)
        return len(free)
