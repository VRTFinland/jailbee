from __future__ import annotations

from datetime import timedelta

import pytest

from jailbee.agent_status import AgentSummary, SessionMark
from jailbee.accounts.models import AgentActivity
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.visibility import container_matches_filter, filter_repo_groups
from jailbee.orchestration.view import ChildView
from tests.web_fakes import repo_group
from tests.web_fakes import START, container
from jailbee.dashboard.visibility import visible_repo_groups
from jailbee.lifecycle import ContainerInfo


@pytest.mark.parametrize(
    ("state", "active"),
    [("waiting", True), ("busy", True), ("shell", True), ("error:quota", True),
     ("error:auth", True), ("idle", False), ("unknown", False)],
)
def test_active_uses_coloured_agent_marks(state: str, active: bool) -> None:
    info = container("alpha-one", agent_status=(AgentSummary("claude", state, START, None, 1),))
    assert container_matches_filter(info, "active", now=START) is active


def test_active_includes_recent_idle_events_but_not_just_recent_state() -> None:
    info = container("alpha-one", agent_status=(AgentSummary("claude", "idle", START, None, 1),))
    assert not container_matches_filter(info, "active", now=START)
    info.agent_status = (AgentSummary("claude", "idle", START, None, 1,
        activity=AgentActivity(None, None, state="idle", last_event_at=START)),)
    assert container_matches_filter(info, "active", now=START + timedelta(minutes=29))
    assert not container_matches_filter(info, "active", now=START + timedelta(minutes=30))


def test_active_uses_per_session_marks_not_only_the_summary_state() -> None:
    info = container("alpha-one", agent_status=(AgentSummary("claude", "idle", START, None, 2,
        sessions=(SessionMark("idle", START - timedelta(hours=1)), SessionMark("busy", START))),))
    assert container_matches_filter(info, "active", now=START)


@pytest.mark.parametrize("state", ["Running", "Stopped", "Frozen"])
def test_running_uses_container_state(state: str) -> None:
    info = container("alpha-one", state=state)
    assert container_matches_filter(info, "running", now=START) is (state == "Running")
    assert container_matches_filter(info, "all", now=START)
    assert not container_matches_filter(info, "active", now=START)


def test_filter_keeps_nonmatching_ancestors_as_dimmed_context_without_mutation() -> None:
    parent = container("alpha-parent", state="Stopped")
    middle = container("alpha-middle", state="Stopped",
        child=ChildView(parent.name, "working", (), False, ()))
    child = container("alpha-child", child=ChildView(middle.name, "working", (), False, ()))
    groups = [repo_group("alpha", parent, middle, child, container("alpha-hidden", state="Stopped")),
              repo_group("empty"), repo_group("beta", container("beta-stopped", state="Stopped"))]
    result = filter_repo_groups(groups, "running", now=START)
    assert [g.prefix for g in result.groups] == ["alpha"]
    assert [c.name for c in result.groups[0].containers] == [parent.name, middle.name, child.name]
    assert result.context == frozenset({parent.name, middle.name})
    assert result.hidden == 2
    assert len(groups[0].containers) == 4
    assert filter_repo_groups(groups, "all", now=START).groups == groups


def test_filter_handles_cycles_and_missing_or_cross_repo_parents() -> None:
    first = container("alpha-a", state="Stopped", child=ChildView("alpha-b", "working", (), False, ()))
    second = container("alpha-b", child=ChildView(first.name, "working", (), False, ()))
    orphan = container("beta-child", repo="beta", child=ChildView("absent", "working", (), False, ()))
    cross = container("beta-cross", repo="beta", child=ChildView(first.name, "working", (), False, ()))
    result = filter_repo_groups([repo_group("alpha", first, second), repo_group("beta", orphan, cross)],
                                "running", now=START)
    assert result.context == frozenset({first.name})
    assert result.hidden == 0
    assert [len(g.containers) for g in result.groups] == [2, 2]


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
