from __future__ import annotations

from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.visibility import visible_repo_groups
from jailbee.lifecycle import ContainerInfo


def _groups() -> list[RepoGroup]:
    return [
        RepoGroup("alpha", "/alpha", None, []),
        RepoGroup(
            "beta",
            "/beta",
            None,
            [ContainerInfo("beta-one", "Running", "strict", None, None, "beta", "clone", None)],
        ),
        RepoGroup(
            "orphan",
            None,
            None,
            [ContainerInfo("orphan-one", "Running", "strict", None, None, "orphan", "clone", None)],
        ),
    ]


def test_defaults_retain_empty_populated_and_orphan_groups() -> None:
    groups = _groups()

    assert visible_repo_groups(groups, show_empty_repos=True, hidden_repos=frozenset()) == groups


def test_global_toggle_hides_only_empty_groups() -> None:
    groups = _groups()

    assert [
        g.prefix
        for g in visible_repo_groups(groups, show_empty_repos=False, hidden_repos=frozenset())
    ] == ["beta", "orphan"]


def test_hidden_prefix_hides_populated_and_orphan_groups() -> None:
    groups = _groups()

    assert [
        g.prefix
        for g in visible_repo_groups(
            groups, show_empty_repos=True, hidden_repos=frozenset({"beta", "orphan"})
        )
    ] == ["alpha"]


def test_individual_hidden_prefix_wins_when_empty_repos_enabled() -> None:
    groups = _groups()

    assert [
        g.prefix
        for g in visible_repo_groups(
            groups, show_empty_repos=True, hidden_repos=frozenset({"alpha"})
        )
    ] == ["beta", "orphan"]


def test_filter_preserves_order_and_does_not_mutate_input() -> None:
    groups = _groups()
    original = groups.copy()

    visible = visible_repo_groups(groups, show_empty_repos=False, hidden_repos=frozenset())

    assert visible == groups[1:]
    assert groups == original
