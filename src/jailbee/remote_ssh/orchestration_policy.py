"""The most a remote session may grant an orchestrator's children.

A remote SSH session can ask for an orchestration grant, but only up to a
bound set by `remote.ssh.orchestration`: `ssh` bounds it by the session's own
unlocks (`remote.ssh.network` for loose and egress, `remote.ssh.mounts` for
optional-mount kinds), while `orchestrator` bounds it by the rights the
orchestrator container already holds. `mount_roots` is deliberately left out:
a remote `jb mount` binds a resolved root path under a write check that a
child never gets, so a mount root would let children receive more than the
session itself can reach. Mount kinds are therefore granted by name, exactly
as the repo config defines them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.orchestration.grant import Grant, egress_covered


@dataclass(frozen=True)
class OrchestratorRights:
    mounts: tuple[str, ...]  # permanent, as-defined optional-mount kinds
    loose: bool  # permanently loose
    egress: tuple[str, ...]  # effective allowlist entries (repo + container extras)


@dataclass(frozen=True)
class GrantBound:
    level: Literal["ssh", "orchestrator"]
    mounts: frozenset[str]
    loose: bool
    egress: tuple[str, ...] | None  # None: any entry


def grant_bound(
    policy: RemoteSSHConfig,
    optional_mounts: Iterable[str],
    rights: OrchestratorRights | None,
) -> GrantBound | None:
    """The bound for grants made remotely; None when orchestration is off."""
    if policy.orchestration == "off":
        return None
    if policy.orchestration == "orchestrator":
        if rights is None:
            raise ValueError("orchestrator bound needs the orchestrator's rights")
        return GrantBound("orchestrator", frozenset(rights.mounts), rights.loose, rights.egress)
    return GrantBound(
        "ssh",
        frozenset(optional_mounts) if policy.mounts else frozenset(),
        policy.network,
        None if policy.network else (),
    )


def grant_excess(new: Grant, current: Grant | None, bound: GrantBound) -> list[str]:
    """One message per grantable element of `new` beyond `bound` and not already in `current`."""
    have = current.grantable if current is not None else None
    suffix = f"(remote.ssh.orchestration: {bound.level})"
    out: list[str] = []
    for mount in sorted(new.grantable.mounts):
        if mount in bound.mounts or (have is not None and mount in have.mounts):
            continue
        out.append(f"mount `{mount}` is beyond what this remote session may grant {suffix}")
    if new.grantable.loose and not (bound.loose or (have is not None and have.loose)):
        out.append(f"loose for children is beyond what this remote session may grant {suffix}")
    for entry in new.grantable.egress:
        if bound.egress is None or egress_covered(entry, bound.egress):
            continue
        if have is not None and egress_covered(entry, have.egress):
            continue
        out.append(f"egress `{entry}` is beyond what this remote session may grant {suffix}")
    return out
