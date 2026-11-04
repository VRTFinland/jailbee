"""Git-ignored output selection and best-effort warm fork seeding."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import yaml

from jailbee.incus import IncusError
from jailbee.tui import info, warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

SNAPSHOT_PREFIX = "jailbee-warm-"
DEVICE_NAME = "jailbee-warm"
MOUNT_PATH = "/run/jailbee-warm"
POOL_ROOT = "/var/lib/incus/storage-pools"

# Reject links instead of dereferencing them: a copied link can still write to
# shared storage. Also refuse existing destinations before partial-copy cleanup.
_PATH_SAFETY_SCRIPT = """
import os
import stat
import sys
from pathlib import Path
source, target, relative = sys.argv[1:]
for root in (source, target):
    current = Path(root)
    for part in relative.split('/'):
        current = current / part
        if current.is_symlink():
            raise ValueError('warm path contains a symbolic link')
if os.path.lexists(Path(target) / relative):
    raise ValueError('warm destination already exists')
origin = Path(source) / relative
for directory, dirs, files in os.walk(origin, followlinks=False):
    for name in dirs + files:
        mode = (Path(directory) / name).lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError('warm path contains a link or special file')
mode = origin.lstat().st_mode
if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
    raise ValueError('warm path is a link or special file')
"""


@dataclass(frozen=True)
class WarmResult:
    seeded: list[str]
    skipped: str | None


def validate_pattern(pattern: str) -> str:
    """Validate a repository-relative glob without shell interpretation."""
    value = pattern.strip().rstrip("/")
    parts = value.split("/")
    if (
        not value
        or pattern.strip().startswith("/")
        or not re.fullmatch(r"[A-Za-z0-9._@+*?\-/\[\]]+", value)
        or any(p in {"", ".", ".."} for p in parts)
        or parts[0] == ".git"
    ):
        raise ValueError(f"Invalid warm path pattern: {pattern!r}; use repo-relative globs")
    return value


def resolve_warm_paths(cfg: Config, override: Sequence[str] | None) -> list[str]:
    """Resolve a per-fork replacement without mutating configuration."""
    return (
        list(cfg.fork.warm_paths) if override is None else [validate_pattern(p) for p in override]
    )


def seed_warm_paths(
    cfg: Config, incus: Incus, *, child: str, source: str, commit: str, patterns: Sequence[str]
) -> WarmResult:
    """Seed ignored outputs by reflink, never preventing creation of a fork."""
    if not patterns:
        return WarmResult([], None)
    try:
        return _seed(cfg, incus, child=child, source=source, commit=commit, patterns=patterns)
    except Exception as exc:
        return _skip(f"Warm paths skipped: {exc}")


def _skip(reason: str) -> WarmResult:
    warn(reason)
    return WarmResult([], reason)


def _seed(
    cfg: Config, incus: Incus, *, child: str, source: str, commit: str, patterns: Sequence[str]
) -> WarmResult:
    from jailbee.lifecycle import container_repo_dir, short_name
    from jailbee.sync import _container_head_oid, _container_status_dirty

    patterns = [validate_pattern(p) for p in patterns]
    if not any(
        c.get("name") == source and c.get("status") == "Running" for c in incus.list_containers()
    ):
        return _skip("Warm paths skipped: source is not running")
    if incus.current_project() != "default":
        return _skip("Warm paths skipped: only the default Incus project is supported")
    repo_dir = container_repo_dir(cfg, incus, source)
    child_repo = container_repo_dir(cfg, incus, child)
    config = yaml.safe_load(incus.config_show(source, expanded=True))
    roots = [
        d
        for d in config.get("devices", {}).values()
        if d.get("type") == "disk" and d.get("path") == "/"
    ]
    pool = roots[0].get("pool") if len(roots) == 1 else None
    driver = next(
        (p.get("driver") for p in incus.list_storage_pools() if p.get("name") == pool), None
    )
    if driver != "btrfs":
        return _skip("Warm paths need a btrfs storage pool; skipped")
    uid, gid = cfg.container_user.uid, cfg.container_user.gid
    if _container_status_dirty(incus, source, repo_dir, uid=uid):
        return _skip("Warm paths skipped: source has uncommitted changes")
    if _container_head_oid(incus, source, repo_dir, uid=uid) != commit:
        return _skip("Warm paths skipped: source moved past the fork commit")
    listing = incus.exec(
        source,
        [
            "git",
            "-C",
            repo_dir,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ],
        uid=uid,
    )
    entries = [p for p in listing.split("\0") if p]
    directories = {p.rstrip("/") for p in entries if p.endswith("/")}
    paths, unmatched = match_patterns(patterns, entries)
    if unmatched:
        warn(f"Warm paths matched no ignored entries: {', '.join(unmatched)}")
    if not paths:
        return _skip("Warm paths skipped: no ignored paths matched")
    snap = f"{SNAPSHOT_PREFIX}{child}"
    # Never delete another fork's snapshot: it may still be mounted there.
    for old in incus.snapshot_list(source):
        if old.get("name") == snap:
            try:
                incus.snapshot_delete(source, snap)
            except Exception:
                pass
    seeded: list[str] = []
    failed: list[str] = []
    incus.snapshot_create(source, snap)
    attached = False
    try:
        incus.config_device_add(
            child,
            DEVICE_NAME,
            "disk",
            {
                "source": (
                    f"{POOL_ROOT}/{pool}/containers-snapshots/{source}/{snap}/rootfs{repo_dir}"
                ),
                "path": MOUNT_PATH,
                "readonly": "true",
            },
        )
        attached = True
        # Inspect the immutable view too: the live source may change between
        # the preflight probes and the snapshot operation.
        if (
            _container_status_dirty(incus, child, MOUNT_PATH, uid=uid)
            or _container_head_oid(incus, child, MOUNT_PATH, uid=uid) != commit
        ):
            return _skip("Warm paths skipped: snapshot does not match the clean fork commit")
        for path in paths:
            target = f"{child_repo}/{path}"
            try:
                incus.exec(
                    child,
                    [
                        "git",
                        "-C",
                        child_repo,
                        "check-ignore",
                        "-q",
                        "--",
                        path + "/" if path in directories else path,
                    ],
                    uid=uid,
                )
                incus.exec(
                    child,
                    ["python3", "-c", _PATH_SAFETY_SCRIPT, MOUNT_PATH, child_repo, path],
                    uid=uid,
                    gid=gid,
                )
            except IncusError:
                warn(f"Warm path skipped: {path} is not ignored in the child or is unsafe to copy")
                continue
            try:
                incus.exec(
                    child,
                    ["mkdir", "-p", "--", str(PurePosixPath(target).parent)],
                    uid=uid,
                    gid=gid,
                )
                incus.exec(
                    child,
                    ["cp", "-a", "--reflink=always", "--", f"{MOUNT_PATH}/{path}", target],
                    uid=uid,
                    gid=gid,
                )
                seeded.append(path)
            except IncusError:
                failed.append(path)
                try:
                    incus.exec(child, ["rm", "-rf", "--", target], uid=uid, gid=gid)
                except Exception as exc:
                    warn(f"Warm path partial result could not be removed: {path}: {exc}")
    finally:
        try:
            incus.config_device_remove(child, DEVICE_NAME, missing_ok=not attached)
        except Exception as exc:
            warn(f"Warm paths device cleanup failed: {exc}")
        try:
            incus.snapshot_delete(source, snap)
        except Exception as exc:
            warn(f"Warm paths snapshot cleanup failed: {exc}")
    if failed:
        warn(f"Warm paths could not be reflinked: {', '.join(failed)}")
    if seeded:
        info(
            f"Seeded {len(seeded)} warm path(s) from '{short_name(cfg, source)}': "
            f"{', '.join(seeded)}"
        )
    return WarmResult(seeded, None)


def _matches(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return _matches(pattern[1:], path) or bool(path and _matches(pattern, path[1:]))
    return bool(
        path and fnmatch.fnmatchcase(path[0], pattern[0]) and _matches(pattern[1:], path[1:])
    )


def match_patterns(patterns: Sequence[str], ignored: Sequence[str]) -> tuple[list[str], list[str]]:
    """Select ignored entries in listing order, coalescing descendants."""
    normalized = [validate_pattern(p) for p in patterns]
    matched: list[str] = []
    found: set[str] = set()
    for entry in ignored:
        path = entry.rstrip("/")
        try:
            validate_pattern(path)
        except ValueError:
            continue
        for pattern in normalized:
            if _matches(pattern.split("/"), path.split("/")):
                found.add(pattern)
                if path not in matched:
                    matched.append(path)
    selected = set(matched)
    return (
        [
            p
            for p in matched
            if not any("/".join(p.split("/")[:i]) in selected for i in range(1, len(p.split("/"))))
        ],
        [p for p in normalized if p not in found],
    )
