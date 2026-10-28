"""Grant, revoke, show and list logic for `jb orchestrate`.

Pure logic over `Config` and the `Incus` wrapper; the CLI only parses flags.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from jailbee.lifecycle import short_name
from jailbee.orchestration import labels, service
from jailbee.orchestration.grant import (
    DEFAULT_MAX_RUNNING_CHILDREN,
    IMPLIED_OPERATIONS,
    OPERATIONS,
    Grant,
    Grantable,
    GrantError,
    Operation,
    cap_excess,
    effective_grant,
)
from jailbee.orchestration.profiles import default_grant, profile_grant, repo_cap
from jailbee.orchestration.runner_model import Runner

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.config.models_remote import RemoteSSHConfig
    from jailbee.incus import Incus

_MODE_LABEL = "user.jailbee.mode"
_IMPLIED_DENY = "spawn and message are part of every grant; use `jb orchestrate revoke`"


def _default() -> Grant:
    return Grant(max_running_children=DEFAULT_MAX_RUNNING_CHILDREN)


@dataclass(frozen=True)
class GrantFlags:
    """What `jb orchestrate grant` was asked to change; unset fields keep their value."""

    profile: str | None = None
    children: int | None = None
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    mounts: tuple[str, ...] | None = None
    clear_mounts: bool = False
    loose: bool | None = None
    egress: tuple[str, ...] | None = None
    clear_egress: bool = False
    runner: Runner | None = None
    clear_runner: bool = False


@dataclass(frozen=True)
class GrantRow:
    short: str
    full: str
    grant: Grant | None
    problem: str | None
    running_children: int
    effective: Grant | None


def repo_registered(cfg: Config) -> bool:
    """True when this repo's container prefix is registered on this host."""
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    with Session(get_engine()) as session:
        return session.get(RegisteredRepo, cfg.container_prefix) is not None


def _is_own(cfg: Config, raw: dict[str, object]) -> bool:
    """True when the container carries this repo's `<prefix>-base` profile.

    Prefixes may contain dashes (`app` / `app-web`), so a name prefix cannot
    tell repos apart; the base profile is the codebase's membership rule.
    """
    profiles = raw.get("profiles")
    return isinstance(profiles, list) and f"{cfg.container_prefix}-base" in profiles


def grant_refusal(cfg: Config, incus: Incus, full: str) -> str | None:
    """Why `full` cannot hold a grant at all, or None."""
    short = short_name(cfg, full)
    if not any(c.get("name") == full and _is_own(cfg, c) for c in incus.list_containers()):
        return f"'{full}' is not a container of this repo"
    if incus.config_get(full, _MODE_LABEL) == "mount":
        return f"'{short}' is a mount-mode container: it can neither fork nor import"
    parent = incus.config_get(full, labels.SPAWNED_BY)
    if parent:
        return f"'{short}' is a child of '{short_name(cfg, parent)}'; children never orchestrate"
    if not repo_registered(cfg):
        return "repo not registered on this host; run `jb apply` first"
    cap = repo_cap(cfg)
    if not cap.allowed:
        return "; ".join(cap_excess(_default(), cap))
    return None


def read_grant(incus: Incus, full: str) -> Grant | None:
    """The stored grant; None when unset, GrantError when malformed."""
    raw = incus.config_get(full, labels.GRANT)
    if raw is None:
        return None
    return Grant.from_label(raw)


def _check_ops(names: tuple[str, ...]) -> None:
    for name in names:
        if name not in OPERATIONS:
            raise GrantError(f"unknown operation {name!r}; known: {', '.join(OPERATIONS)}")


def _check_mounts(cfg: Config, names: tuple[str, ...]) -> None:
    known = sorted(cfg.optional_mounts)
    for name in names:
        if name not in cfg.optional_mounts:
            raise GrantError(
                f"unknown optional mount {name!r}; known: {', '.join(known) or 'none'}"
            )


def build_grant(cfg: Config, current: Grant | None, flags: GrantFlags) -> Grant:
    """Apply `flags` onto the profile, the current grant, `default_profile` or the defaults."""
    if flags.mounts is not None and flags.clear_mounts:
        raise GrantError("--mount and --clear-mounts are mutually exclusive; give only one")
    if flags.egress is not None and flags.clear_egress:
        raise GrantError("--egress and --clear-egress are mutually exclusive; give only one")
    if flags.runner is not None and flags.clear_runner:
        raise GrantError("--runner-agent and --clear-runner are mutually exclusive; give only one")
    _check_ops(flags.allow)
    _check_ops(flags.deny)
    if any(op in IMPLIED_OPERATIONS for op in flags.deny):
        raise GrantError(_IMPLIED_DENY)
    if flags.mounts is not None:
        _check_mounts(cfg, flags.mounts)

    if flags.profile:
        base = profile_grant(cfg, flags.profile)
    else:
        base = current if current is not None else (default_grant(cfg) or _default())

    ops = (set(base.operations) | set(flags.allow)) - set(flags.deny)
    mounts = base.grantable.mounts
    if flags.clear_mounts:
        mounts = ()
    elif flags.mounts is not None:
        mounts = flags.mounts
    egress = base.grantable.egress
    if flags.clear_egress:
        egress = ()
    elif flags.egress is not None:
        egress = flags.egress
    loose = base.grantable.loose if flags.loose is None else flags.loose
    children = base.max_running_children if flags.children is None else flags.children
    try:
        return Grant(
            max_running_children=children,
            operations=cast("tuple[Operation, ...]", tuple(sorted(ops))),
            grantable=Grantable(mounts=mounts, loose=loose, egress=egress),
            default_runner=None if flags.clear_runner else (flags.runner or base.default_runner),
        )
    except ValidationError as e:
        raise GrantError(f"invalid grant: {e}") from e


def _write(incus: Incus, full: str, grant: Grant) -> None:
    incus.config_set(full, labels.GRANT, grant.to_label())
    service.ensure_running()


def grant(
    cfg: Config,
    incus: Incus,
    full: str,
    flags: GrantFlags,
    *,
    remote: RemoteSSHConfig | None = None,
    on_previous: Callable[[Grant | None], None] | None = None,
) -> Grant:
    """Validate and store a grant on `full`; nothing is written on refusal.

    With `remote` (a restricted SSH session's policy) the resulting grant is
    also bounded by `remote.ssh.orchestration`; elements the stored grant
    already holds stay, so narrowing and unrelated edits always pass.
    """
    refusal = grant_refusal(cfg, incus, full)
    if refusal is not None:
        raise GrantError(refusal)
    try:
        current = read_grant(incus, full)
    except GrantError:
        current = None  # a malformed label is overwritten
    new = build_grant(cfg, current, flags)
    if remote is not None:
        _check_remote_bound(cfg, incus, full, new, current, remote)
    excess = cap_excess(new, repo_cap(cfg))
    if excess:
        raise GrantError("; ".join(excess))
    _write(incus, full, new)
    if on_previous is not None:
        on_previous(current)
    return new


def _check_remote_bound(
    cfg: Config,
    incus: Incus,
    full: str,
    new: Grant,
    current: Grant | None,
    remote: RemoteSSHConfig,
) -> None:
    from jailbee.remote_ssh import orchestration_policy as policy

    if remote.orchestration == "off":
        raise GrantError(
            "orchestrate grant is a host command here: remote.ssh.orchestration is off"
        )
    rights = (
        policy.read_orchestrator_rights(cfg, incus, full)
        if remote.orchestration == "orchestrator"
        else None
    )
    bound = policy.grant_bound(remote, cfg.optional_mounts, rights)
    if bound is None:  # unreachable: "off" is refused above
        raise GrantError("remote.ssh.orchestration is off")
    excess = policy.grant_excess(new, current, bound)
    if excess:
        raise GrantError("; ".join(excess))


def revoke(incus: Incus, full: str) -> bool:
    """Remove the grant; False when there was none."""
    if incus.config_get(full, labels.GRANT) is None:
        return False
    incus.config_unset_checked(full, labels.GRANT)
    return True


def _running_children(containers: list[dict[str, object]], full: str) -> int:
    count = 0
    for c in containers:
        config = c.get("config")
        if (
            isinstance(config, dict)
            and config.get(labels.SPAWNED_BY) == full
            and c.get("status") == "Running"
        ):
            count += 1
    return count


def _row(cfg: Config, full: str, raw: str | None, containers: list[dict[str, object]]) -> GrantRow:
    grant_: Grant | None = None
    problem: str | None = None
    effective: Grant | None = None
    if raw is not None:
        try:
            grant_ = Grant.from_label(raw)
        except GrantError as e:
            problem = str(e)
        else:
            effective = effective_grant(grant_, repo_cap(cfg))
    return GrantRow(
        short=short_name(cfg, full),
        full=full,
        grant=grant_,
        problem=problem,
        running_children=_running_children(containers, full),
        effective=effective,
    )


def show(cfg: Config, incus: Incus, full: str) -> GrantRow:
    raw = incus.config_get(full, labels.GRANT)
    return _row(cfg, full, raw, incus.list_containers())


def list_grants(cfg: Config, incus: Incus) -> list[GrantRow]:
    """Rows for this repo's containers holding a grant label, by short name."""
    containers = incus.list_containers()
    rows: list[GrantRow] = []
    for c in containers:
        name = c.get("name")
        config = c.get("config")
        if not isinstance(name, str) or not isinstance(config, dict) or not _is_own(cfg, c):
            continue
        raw = config.get(labels.GRANT)
        if raw is None:
            continue
        rows.append(_row(cfg, name, str(raw), containers))
    return sorted(rows, key=lambda r: r.short)


def apply_default_profile(cfg: Config, incus: Incus, full: str) -> Grant | None:
    """Give a fresh container the default profile's grant, when it may hold one."""
    new = default_grant(cfg)
    if new is None:
        return None
    if grant_refusal(cfg, incus, full) is not None:
        return None
    if cap_excess(new, repo_cap(cfg)):
        return None
    _write(incus, full, new)
    return new
