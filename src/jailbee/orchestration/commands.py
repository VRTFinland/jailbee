"""Grant, revoke, show and list logic for `jb orchestrate`.

Pure logic over `Config` and the `Incus` wrapper; the CLI only parses flags.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from jailbee.config.models_orchestration import PROFILE_NAME_RE, OrchestrationProfile
from jailbee.lifecycle import short_name
from jailbee.orchestration import labels, service
from jailbee.orchestration.grant import (
    DEFAULT_MAX_RUNNING_CHILDREN,
    IMPLIED_OPERATIONS,
    ISSUE_ACTION_KINDS,
    OPERATIONS,
    ForeignIssueRights,
    Grant,
    Grantable,
    GrantError,
    IssueActionKind,
    Operation,
    PublishRights,
    cap_excess,
    effective_grant,
    kind_granted,
)
from jailbee.orchestration.profiles import default_grant, profile_grant, profile_of, repo_cap
from jailbee.orchestration.runner_model import Runner

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.config.models_remote import RemotePolicy
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
    publish_issues: tuple[str, ...] | None = None
    publish_foreign_issues: tuple[str, ...] | None = None
    publish_prs: bool | None = None
    publish_pr_reviews: bool | None = None
    publish_rate: int | None = None
    no_publish: bool = False


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


@dataclass(frozen=True)
class SavedProfile:
    name: str
    profile: OrchestrationProfile
    replaced: bool


def save_profile(
    cfg: Config,
    incus: Incus,
    full: str,
    name: str,
    *,
    global_path: Path,
    force: bool = False,
) -> SavedProfile:
    """Persist a container's stored grant as a global orchestration profile."""
    from jailbee.config.local_layer import local_config_path, read_local_raw
    from jailbee.config_writer import YamlChange, patch_file

    if not PROFILE_NAME_RE.fullmatch(name):
        raise GrantError(
            f"invalid profile name {name!r}: use lowercase letters, digits and dashes, "
            "starting with a letter (max 32 characters)"
        )
    stored = read_grant(incus, full)
    if stored is None:
        raise GrantError(f"no grant on {short_name(cfg, full)!r}; grant it first")
    local = read_local_raw(cfg.container_prefix)
    orchestration = local.get("orchestration")
    profiles = orchestration.get("profiles") if isinstance(orchestration, dict) else None
    if isinstance(profiles, dict) and name in profiles:
        path = local_config_path(cfg.container_prefix)
        raise GrantError(
            f"profile {name!r} is defined in {path}, which overrides global.yaml; "
            "edit it there with `jailbee config edit --local`"
        )
    replaced = name in cfg.orchestration_view().profiles
    if replaced and not force:
        raise GrantError(f"profile {name!r} already exists; pass --force to replace it")
    profile = profile_of(stored)
    patch_file(
        global_path,
        [
            YamlChange(
                ("orchestration", "profiles", name),
                profile.model_dump(mode="json", exclude_none=True),
            )
        ],
    )
    return SavedProfile(name, profile, replaced)


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


def _issue_kinds(values: tuple[str, ...]) -> tuple[IssueActionKind, ...]:
    if values == ("none",):
        return ()
    for value in values:
        if value not in ISSUE_ACTION_KINDS:
            raise GrantError(
                f"unknown issue action {value!r}; known: {', '.join(ISSUE_ACTION_KINDS)}"
            )
    return cast("tuple[IssueActionKind, ...]", tuple(sorted(set(values))))


def _publish_rights(base: PublishRights, flags: GrantFlags) -> PublishRights:
    changes = {
        "issues": flags.publish_issues,
        "foreign_issues": flags.publish_foreign_issues,
        "pull_requests": flags.publish_prs,
        "pr_reviews": flags.publish_pr_reviews,
        "max_actions_per_hour": flags.publish_rate,
    }
    if flags.no_publish:
        if any(v is not None for v in changes.values()):
            raise GrantError("--no-publish is exclusive with all other publish flags")
        return PublishRights()
    data = base.model_dump()
    if flags.publish_issues is not None:
        data["issues"] = _issue_kinds(flags.publish_issues)
    if flags.publish_foreign_issues == ("none",):
        data["foreign_issues"] = ForeignIssueRights().model_dump()
    elif flags.publish_foreign_issues is not None:
        repos: list[str] = []
        actions: tuple[IssueActionKind, ...] | None = None
        for entry in flags.publish_foreign_issues:
            repo, sep, kinds = entry.partition(":")
            if not sep:
                raise GrantError("foreign issue rights must be REPO:KIND[,KIND]")
            parsed = _issue_kinds(tuple(kinds.split(",")))
            if actions is not None and actions != parsed:
                raise GrantError("foreign repositories must have the same action list")
            repos.append(repo)
            actions = parsed
        data["foreign_issues"] = ForeignIssueRights(
            repos=tuple(repos), actions=actions or ()
        ).model_dump()
    for name in ("pull_requests", "pr_reviews", "max_actions_per_hour"):
        if changes[name] is not None:
            data[name] = changes[name]
    return PublishRights.model_validate(data)


def publish_summary(rights: PublishRights) -> str:
    if rights.empty():
        return "none"
    parts: list[str] = []
    if rights.issues:
        parts.append(f"issues: {', '.join(rights.issues)}")
    f = rights.foreign_issues
    if f.repos and f.actions:
        parts.append(f"foreign issues: {', '.join(f.repos)} ({', '.join(f.actions)})")
    if rights.pull_requests:
        parts.append("PRs")
    if rights.pr_reviews:
        parts.append("PR reviews")
    parts.append(f"{rights.max_actions_per_hour}/hour")
    return "; ".join(parts)


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
            publish=_publish_rights(base.publish, flags),
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
    remote: RemotePolicy | None = None,
    on_previous: Callable[[Grant | None], None] | None = None,
) -> Grant:
    """Validate and store a grant on `full`; nothing is written on refusal.

    With `remote` (a restricted remote session's policy) the resulting grant is
    also bounded by `remote.<transport>.orchestration`; elements the stored grant
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
    remote: RemotePolicy,
) -> None:
    from jailbee.remote_ssh import orchestration_policy as policy
    from jailbee.remote_ssh.session import policy_transport

    transport = policy_transport()
    if remote.orchestration == "off":
        raise GrantError(
            f"orchestrate grant is a host command here: remote.{transport}.orchestration is off"
        )
    rights = (
        policy.read_orchestrator_rights(cfg, incus, full)
        if remote.orchestration == "orchestrator"
        else None
    )
    bound = policy.grant_bound(remote, cfg.optional_mounts, rights)
    if bound is None:  # unreachable: "off" is refused above
        raise GrantError(f"remote.{transport}.orchestration is off")
    from jailbee.remote_ssh.router import RemoteUnlocks, RouteError, policy_allows

    required: list[tuple[str, list[str]]] = []
    p = new.publish
    had = current.publish if current is not None else PublishRights()
    rate_widened = p.max_actions_per_hour > had.max_actions_per_hour
    issue_added = any(not kind_granted(k, had.issues) for k in p.issues)
    foreign_added = any(
        not had.covers_issue(k, r, foreign=True)
        for r in p.foreign_issues.repos
        for k in p.foreign_issues.actions
    )
    if (
        issue_added
        or foreign_added
        or (rate_widened and (p.issues or (p.foreign_issues.repos and p.foreign_issues.actions)))
    ):
        required.append(("issues", ["issue", "apply", full]))
    if p.pull_requests and (not had.pull_requests or rate_widened):
        required.append(
            ("pull_requests", ["pr", "push", full, "--source-manifest", "publish.json"])
        )
    if p.pr_reviews and (not had.pr_reviews or rate_widened):
        required.append(("pr_reviews", ["review", "apply", full]))
    for right, argv in required:
        try:
            policy_allows(
                argv,
                remote.commands,
                restrict_host=remote.restrict_host,
                unlocks=RemoteUnlocks.of(remote, transport=transport),
            )
        except RouteError as e:
            raise GrantError(f"publish right {right!r} needs manual publish permission: {e}") from e
    excess = policy.grant_excess(new, current, bound, transport=transport)
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
