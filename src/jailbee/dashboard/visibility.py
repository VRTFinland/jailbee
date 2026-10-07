"""Pure repository-group visibility filtering shared by dashboard front-ends."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup


def visible_repo_groups(
    groups: list[RepoGroup],
    *,
    show_empty_repos: bool,
    hidden_repos: frozenset[str],
) -> list[RepoGroup]:
    """Return visible groups without modifying the gathered snapshot."""
    return [
        group
        for group in groups
        if group.prefix not in hidden_repos and (show_empty_repos or group.containers)
    ]
