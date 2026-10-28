"""Host orchestration profiles and the repo cap, as grant-model values."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from jailbee.config.models_orchestration import OrchestrationProfile
from jailbee.orchestration.grant import Grant, Grantable, GrantError, Operation, RepoCap
from jailbee.orchestration.runner_model import DefaultRunnerSource, Runner

if TYPE_CHECKING:
    from jailbee.config import Config


def _grant_of(profile: OrchestrationProfile) -> Grant:
    g = profile.grantable
    # OrchestrationProfile's validator already restricts `operations` to known
    # names, so the list is safe to narrow to the Operation literal tuple.
    operations = cast("tuple[Operation, ...]", tuple(profile.operations))
    return Grant(
        max_running_children=profile.max_running_children,
        operations=operations,
        grantable=Grantable(
            mounts=tuple(g.mounts),
            loose=g.loose,
            egress=tuple(g.egress),
        ),
    )


def profile_grant(cfg: Config, name: str) -> Grant:
    """The grant a named host profile confers; GrantError when no such profile."""
    profiles = cfg.orchestration_view().profiles
    profile = profiles.get(name)
    if profile is None:
        known = ", ".join(sorted(profiles)) or "none"
        raise GrantError(f"unknown orchestration profile {name!r}; known: {known}")
    return _grant_of(profile)


def default_grant(cfg: Config) -> Grant | None:
    """The default profile's grant, or None when no default is set."""
    view = cfg.orchestration_view()
    if view.default_profile is None:
        return None
    return profile_grant(cfg, view.default_profile)


def default_runner(
    cfg: Config, grant: Grant | None
) -> tuple[Runner | None, DefaultRunnerSource | None]:
    """Resolve the container's runner before falling back to the config view."""
    if grant is not None and grant.default_runner is not None:
        return grant.default_runner, "container"
    view = cfg.orchestration_view()
    return view.default_runner, view.default_runner_source


def repo_cap(cfg: Config) -> RepoCap:
    """This repo's narrowing block as a RepoCap; lists become tuples, None is kept."""
    repo = cfg.orchestration_view().repo

    def _tuple(values: list[str] | None) -> tuple[str, ...] | None:
        return None if values is None else tuple(values)

    return RepoCap(
        allowed=repo.allowed,
        max_running_children=repo.max_running_children,
        mounts=_tuple(repo.mounts),
        egress=_tuple(repo.egress),
        loose=repo.loose,
    )
