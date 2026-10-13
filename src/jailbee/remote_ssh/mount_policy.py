"""Which `optional_mounts` entries a restricted remote SSH session may attach.

The decision rests on the host path, not the mount's name: the repo config
decides what a name points at, while `remote.ssh.mount_roots` lives in
`global.yaml`, which no repo or branch config can write. A mount qualifies
only when its host path resolves to *exactly* a root. Nothing inside a root
is ever read as a path, so a session able to write into one cannot plant a
symlink that reaches further (see the spec's "Exact match only").
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.config.models_remote import RemoteMountRoot, RemoteSSHConfig


@dataclass(frozen=True)
class MountGrant:
    """How a granted mount is attached: the device source and readonly flag."""

    source: Path
    readonly: bool
    forced_readonly: bool = False


def _resolved(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _writable_mounts(cfg: Config) -> Iterator[tuple[str, Path]]:
    for mount in cfg.host_mounts:
        if not mount.readonly:
            yield f"host_mounts {mount.host}", _resolved(mount.host)
    for kind, opt in cfg.optional_mounts.items():
        if not opt.readonly:
            yield f"optional_mounts {kind} ({opt.host})", _resolved(opt.host)


def writable_cover(cfg: Config, root: Path) -> str | None:
    """The first read-write mount of ``cfg`` on ``root`` or one of its parents.

    A container holding such a mount can swap ``root`` for a symlink between
    the check and Incus opening it, so ``root`` is not safe to grant.
    """
    for label, host in _writable_mounts(cfg):
        if root.is_relative_to(host):
            return f"{label} (read-write)"
    return None


def _root_for(policy: RemoteSSHConfig, resolved: Path) -> RemoteMountRoot | None:
    return next((r for r in policy.mount_roots if r.path == resolved), None)


def remote_mount_grant(policy: RemoteSSHConfig, cfg: Config, kind: str) -> MountGrant | str:
    """The grant for attaching ``kind`` in a restricted session, or why not."""
    mount = cfg.optional_mounts.get(kind)
    if mount is None:
        return f"Unknown optional mount '{kind}'. Available: {list(cfg.optional_mounts)}"
    if policy.mounts:
        return MountGrant(Path(mount.host), mount.readonly)
    prefix = cfg.container_prefix
    if prefix in policy.excluded_repos:
        return f"Repo '{prefix}' is excluded from remote SSH (remote.ssh.excluded_repos)."
    resolved = _resolved(mount.host)
    root = _root_for(policy, resolved)
    if root is None:
        return (
            f"'{kind}' mounts {resolved}, which is not listed in remote.ssh.mount_roots; "
            "a remote SSH session may only attach a listed host directory itself. "
            "Attach it on the host, or list it in ~/.config/jailbee/global.yaml."
        )
    if root.repos and prefix not in root.repos:
        return f"remote.ssh.mount_roots allows {root.path} for {root.repos}, not for '{prefix}'."
    try:
        mode = os.stat(root.path).st_mode
    except OSError:
        return f"remote.ssh.mount_roots path {root.path} does not exist."
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        return f"remote.ssh.mount_roots path {root.path} is not a directory or a file."
    cover = writable_cover(cfg, root.path)
    if cover is not None:
        return (
            f"'{kind}' is not attachable over remote SSH: {cover} covers {root.path}, so a "
            "container could replace it with a symlink before the mount lands."
        )
    readonly = mount.readonly or root.readonly
    return MountGrant(root.path, readonly, forced_readonly=readonly and not mount.readonly)


def grantable_kinds(policy: RemoteSSHConfig, cfg: Config) -> tuple[str, ...]:
    """The kinds ``remote_mount_grant`` grants, in config order."""
    return tuple(
        kind
        for kind in cfg.optional_mounts
        if isinstance(remote_mount_grant(policy, cfg, kind), MountGrant)
    )
