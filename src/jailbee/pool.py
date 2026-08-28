"""Generic per-container cache pools.

A pool gives each container its own private copy of a cache directory
that its tool locks — Chrome's profile, Gradle's `~/.gradle` — so two
containers never contend on one lock file. Slots live on the host under
`<shared_dir>/<host_subpath>/slots/`, are handed out through
`by-container/<name>` symlinks, and are attached to the container as a
disk device named `<cache name>-slot`.

Which caches are pooled, and how each one is seeded and cleaned, is
configuration: see `PoolSpec` and `POOL_PRESETS` in `config.py`. This
module is the only place that mutates a pool root.
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

from jailbee.config import CONTAINER_USERNAME, Config, PoolSpec
from jailbee.incus import Incus, IncusError
from jailbee.tui import info


class PoolError(Exception):
    """A pool root cannot be brought into a usable state."""


@dataclass(frozen=True)
class Pool:
    name: str
    root: Path
    container_path: str
    spec: PoolSpec

    @property
    def device_name(self) -> str:
        return f"{self.name}-slot"

    @property
    def slots_dir(self) -> Path:
        return self.root / "slots"

    @property
    def by_container_dir(self) -> Path:
        return self.root / "by-container"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"


@dataclass
class SlotInfo:
    """One slot's state, for `jailbee pool ls`."""

    pool: str
    name: str
    path: Path
    container: str | None
    warmth_mtime: float | None
    size_bytes: int


def pools_for(cfg: Config) -> list[Pool]:
    """Every pooled entry of `effective_shared_caches()`, as `Pool`s."""
    assert cfg.shared_dir is not None  # set by load_config
    home = f"/home/{CONTAINER_USERNAME}"
    pools: list[Pool] = []
    for cache in cfg.effective_shared_caches():
        if cache.pool is None:
            continue
        path = (
            cache.container_path.replace("~", home, 1)
            if cache.container_path.startswith("~")
            else cache.container_path
        )
        pools.append(
            Pool(
                name=cache.name,
                root=cfg.shared_dir / cache.host_subpath,
                container_path=path,
                spec=cache.pool,
            )
        )
    return pools


def get(cfg: Config, name: str) -> Pool | None:
    """The pool for cache `name`, or None when it isn't pooled."""
    for p in pools_for(cfg):
        if p.name == name:
            return p
    return None


_RESERVED = frozenset({"slots", "by-container", ".lock"})


@contextmanager
def _lock(pool: Pool) -> Iterator[IO[bytes]]:
    """Exclusive flock on the pool root, auto-released on process exit.

    Never call `ensure_pool_dirs` from inside this: it takes the same
    lock on a second file descriptor, which deadlocks.
    """
    fp = open(pool.lock_path, "wb")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield fp
    finally:
        fp.close()


def ensure_pool_dirs(cfg: Config, pool: Pool) -> None:
    """Create the pool layout, migrating a pre-pool cache into slot-0.

    Idempotent. Installs that predate pooling have the cache itself
    directly under the pool root; moving it into `slots/slot-0` keeps it
    warm and makes it the first seed source.
    """
    del cfg  # signature parity with the other entry points
    pool.root.mkdir(parents=True, exist_ok=True)
    if not pool.lock_path.exists():
        pool.lock_path.touch()
    with _lock(pool):
        pool.slots_dir.mkdir(exist_ok=True)
        pool.by_container_dir.mkdir(exist_ok=True)
        legacy = [p for p in pool.root.iterdir() if p.name not in _RESERVED]
        if not legacy:
            return
        slot0 = pool.slots_dir / "slot-0"
        if slot0.exists():
            raise PoolError(
                f"{pool.root} holds both pool slots and loose cache content "
                f"({', '.join(sorted(p.name for p in legacy))}). Move or delete "
                f"the loose entries by hand, then re-run."
            )
        slot0.mkdir()
        for entry in legacy:
            shutil.move(str(entry), str(slot0 / entry.name))
        info(f"Migrated the existing {pool.name} cache into {slot0}")


def ensure_pools(cfg: Config) -> None:
    """`ensure_pool_dirs` for every pool. Called by init and apply."""
    for p in pools_for(cfg):
        ensure_pool_dirs(cfg, p)


def _seed(pool: Pool, source: Path, target: Path) -> None:
    """Copy source/ -> target/, hardlinking the `link_paths` subtrees.

    Pass 1 copies everything except the wipe paths, the stale globs and
    the link paths (`--delete` leaves excluded content in the target
    alone). Pass 2 syncs each link path with `--link-dest`, so identical
    files become second names for one inode instead of copies.
    """
    excludes = [f"--exclude={p}/" for p in pool.spec.wipe_paths]
    excludes += [f"--exclude={g}" for g in pool.spec.stale_globs]
    excludes += [f"--exclude={p}/" for p in pool.spec.link_paths]
    subprocess.run(
        ["rsync", "-a", "--delete", *excludes, f"{source}/", f"{target}/"],
        check=True,
    )
    for rel in pool.spec.link_paths:
        src = source / rel
        if not src.is_dir():
            continue
        dst = target / rel
        dst.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["rsync", "-a", "--delete", f"--link-dest={src}", f"{src}/", f"{dst}/"],
            check=True,
        )


def _wipe(pool: Pool, slot: Path) -> None:
    """Remove `wipe_paths` and `stale_globs` from `slot`. Idempotent.

    Always runs at release, including on the reuse path (target ==
    source) where rsync's excludes never execute — a lock file left by
    an unclean exit would otherwise break the next allocator.
    """
    for rel in pool.spec.wipe_paths:
        shutil.rmtree(slot / rel, ignore_errors=True)
    for pattern in pool.spec.stale_globs:
        for orphan in slot.glob(pattern):
            try:
                orphan.unlink()
            except OSError:
                pass


def _slot_mtime(pool: Pool, slot: Path) -> float:
    """Warmth ranking: `warmth_file`'s mtime, else the slot dir's."""
    if pool.spec.warmth_file is None:
        try:
            return slot.stat().st_mtime
        except OSError:
            return 0.0
    warmth = slot / pool.spec.warmth_file
    if not warmth.exists():
        return 0.0
    return warmth.stat().st_mtime


def _all_slots(pool: Pool) -> list[Path]:
    """Return all slot directories sorted by name."""
    return sorted(p for p in pool.slots_dir.glob("slot-*") if p.is_dir())


def _free_slots(pool: Pool) -> list[Path]:
    """Return slots with no incoming `by-container/` symlink."""
    by_c = pool.by_container_dir
    allocated = {sym.resolve() for sym in by_c.iterdir() if sym.is_symlink()}
    return [s for s in _all_slots(pool) if s.resolve() not in allocated]


def _create_new_slot(pool: Pool) -> Path:
    """Create slot-N where N is the smallest non-negative int not in use."""
    used = {int(s.name.removeprefix("slot-")) for s in _all_slots(pool)}
    n = 0
    while n in used:
        n += 1
    new_slot = pool.slots_dir / f"slot-{n}"
    new_slot.mkdir()
    return new_slot


def _reconcile(pool: Pool, incus: Incus) -> None:
    """Drop `by-container/<name>` symlinks for containers that no longer exist.

    Propagates `IncusError` from `list_containers` — never delete symlinks
    when Incus state is unknown.
    """
    by_c = pool.by_container_dir
    if not by_c.exists():
        return
    existing = {c["name"] for c in incus.list_containers()}
    for symlink in by_c.iterdir():
        if symlink.name not in existing:
            symlink.unlink()


def _ensure_mount(incus: Incus, pool: Pool, container: str, slot: Path) -> None:
    """Add the slot disk device to the container.

    Note: `incus config device add` errors if the device already exists.
    Callers that re-allocate for the same container must skip this call;
    `allocate` enforces that via the idempotent-symlink check.
    """
    incus.config_device_add(
        container,
        pool.device_name,
        "disk",
        {
            "source": str(slot),
            "path": pool.container_path,
        },
    )


_DEVICE_MISSING_MARKERS = ("not found", "doesn't exist", "no such")


def _try_remove_device(incus: Incus, pool: Pool, container: str) -> None:
    """Remove the slot device, silently absorbing 'already gone' errors."""
    try:
        incus.config_device_remove(container, pool.device_name)
    except IncusError as e:
        msg = str(e).lower()
        if any(marker in msg for marker in _DEVICE_MISSING_MARKERS):
            return
        raise


def allocate(cfg: Config, incus: Incus, pool: Pool, container: str) -> Path:
    """Allocate a slot for `container`, mount it, return host slot path.

    Idempotent: re-calling for a container that already has a slot
    returns the existing slot without re-allocating, copying, or
    re-mounting.
    """
    ensure_pool_dirs(cfg, pool)
    with _lock(pool):
        _reconcile(pool, incus)

        symlink = pool.by_container_dir / container
        if symlink.is_symlink():
            target = symlink.resolve()
            if target.exists():
                return target
            # Dangling — clean up, fall through to re-allocate
            symlink.unlink()

        all_slots = _all_slots(pool)
        free = _free_slots(pool)
        source = max(all_slots, key=lambda s: _slot_mtime(pool, s), default=None)

        if free:
            target = min(free, key=lambda s: _slot_mtime(pool, s))
        else:
            target = _create_new_slot(pool)

        seeded = source is not None and target != source and pool.spec.seed
        if seeded:
            assert source is not None  # narrows for mypy; implied by `seeded`
            _seed(pool, source, target)

        symlink.symlink_to(Path("..") / "slots" / target.name)
        try:
            _ensure_mount(incus, pool, container, target)
        except Exception:
            symlink.unlink()
            raise

        if source is None:
            info(f"Allocated fresh {target.name} for {container} ({pool.name})")
        elif target == source:
            info(f"Reusing {target.name} for {container} ({pool.name})")
        elif seeded:
            info(
                f"Allocated {target.name} for {container} ({pool.name}) "
                f"(seeded from {source.name})"
            )
        else:
            info(f"Allocated {target.name} for {container} ({pool.name}) (seed disabled)")
        return target


def release(cfg: Config, incus: Incus, pool: Pool, container: str) -> None:
    """Release `container`'s slot: unmount, wipe caches, drop symlink.

    No-op if `container` has no slot allocated, or the pool root doesn't
    exist yet.
    """
    del cfg  # signature parity with the other entry points
    if not pool.root.exists():
        return
    with _lock(pool):
        symlink = pool.by_container_dir / container
        if not symlink.is_symlink():
            return
        slot = symlink.resolve()
        _try_remove_device(incus, pool, container)
        if slot.exists():
            _wipe(pool, slot)
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


def _container_for_slot(pool: Pool, slot: Path) -> str | None:
    for sym in pool.by_container_dir.iterdir():
        if sym.is_symlink() and sym.resolve() == slot.resolve():
            return sym.name
    return None


def list_slots(cfg: Config, incus: Incus, pool: Pool) -> list[SlotInfo]:
    """Return every slot's state. Implicitly reconciles first."""
    ensure_pool_dirs(cfg, pool)
    with _lock(pool):
        _reconcile(pool, incus)
        infos: list[SlotInfo] = []
        for slot in _all_slots(pool):
            mtime = _slot_mtime(pool, slot)
            infos.append(
                SlotInfo(
                    pool=pool.name,
                    name=slot.name,
                    path=slot,
                    container=_container_for_slot(pool, slot),
                    warmth_mtime=mtime if mtime > 0.0 else None,
                    size_bytes=_slot_size_bytes(slot),
                )
            )
        return infos


def prune(cfg: Config, incus: Incus, pool: Pool) -> int:
    """Delete every unallocated slot. Returns count deleted."""
    ensure_pool_dirs(cfg, pool)
    with _lock(pool):
        _reconcile(pool, incus)
        free = _free_slots(pool)
        for slot in free:
            shutil.rmtree(slot)
        return len(free)


def allocate_startup(cfg: Config, incus: Incus, container: str) -> None:
    """Allocate every `on-start` pool for `container`. Idempotent."""
    for p in pools_for(cfg):
        if p.spec.allocate != "on-start":
            continue
        ensure_pool_dirs(cfg, p)
        allocate(cfg, incus, p, container)


def release_all(cfg: Config, incus: Incus, container: str) -> None:
    """Release `container`'s slot in every pool. No-op where it has none."""
    for p in pools_for(cfg):
        release(cfg, incus, p, container)


def unique_bytes(pool: Pool) -> int:
    """Bytes the pool occupies, counting each inode once.

    Hardlinked slots share inodes, so summing per-slot sizes reports
    several times the real figure.
    """
    seen: set[tuple[int, int]] = set()
    total = 0
    for slot in _all_slots(pool):
        for p in slot.rglob("*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total
