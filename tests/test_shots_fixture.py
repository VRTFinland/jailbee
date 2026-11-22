"""The screenshot harness' fixture loader: YAML in, dashboard model objects out."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jailbee.dashboard import fork_tree
from website.shots.fixture import FixtureError, load_world

MINIMAL = """
size: [120, 30]
repos:
  - prefix: demo
    containers:
      - name: lead
        grant: {limit: 3}
      - name: b
        child: {orchestrator: lead, state: done}
      - name: solo
        state: Stopped
      - name: a
        pr_number: 7
        child: {orchestrator: lead}
        agent_status:
          - {agent: claude, state: waiting, waiting_for: an answer, count: 1}
        git_status: {wt: clean, ahead_diff: "+3 -1", ahead_count: "2", conflict: ok}
        activity: [{comm: pytest, percent: 140.0, count: 2}]
steps:
  - shot: first
"""


def test_minimal_fixture_builds_groups_and_rows() -> None:
    world = load_world(MINIMAL)
    assert world.size == (120, 30)
    (group,) = world.groups
    assert group.prefix == "demo"
    assert [c.name for c in group.containers] == ["demo-lead", "demo-b", "demo-solo", "demo-a"]
    lead, b, solo, a = group.containers
    assert (lead.repo, lead.state, lead.network) == ("demo", "Running", "strict")
    assert lead.grant is not None and lead.grant.limit == 3
    assert solo.state == "Stopped"
    assert b.child is not None and b.child.state == "done"
    assert a.pr_number == 7
    assert a.agent_status[0].state == "waiting" and a.agent_status[0].waiting_for == "an answer"
    assert a.git_status is not None and a.git_status.ahead_diff == "+3 -1"
    assert a.activity[0].comm == "pytest" and a.activity[0].count == 2
    assert world.steps == [("shot", "first")]


def test_orchestrator_child_links_give_tree_order() -> None:
    (group,) = load_world(MINIMAL).groups
    assert group.containers[1].child is not None
    assert group.containers[1].child.orchestrator == "demo-lead"
    ordered = [c.name for c in fork_tree.tree_order(group.containers)]
    assert ordered == ["demo-lead", "demo-b", "demo-a", "demo-solo"]


def test_datetimes_and_defaults() -> None:
    row = "{name: c, created_at: '2026-11-01T10:00:00Z'}"
    world = load_world(f"repos:\n  - prefix: x\n    containers:\n      - {row}\n")
    assert world.size == (200, 45)
    assert world.groups[0].containers[0].created_at == datetime(2026, 11, 1, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("sise: [1, 2]\nrepos: []\n", "sise"),
        ("repos:\n  - prefix: x\n    contaners: []\n", "contaners"),
        ("repos:\n  - prefix: x\n    containers:\n      - {name: c, stat: Running}\n", "stat"),
        (
            "repos:\n  - prefix: x\n    containers:\n      - {name: c, git_status: {wtt: clean}}\n",
            "wtt",
        ),
        ("repos:\n  - prefix: x\n    containers: [{name: c}]\nsteps:\n  - shoot: a\n", "shoot"),
    ],
)
def test_unknown_keys_fail_loudly_naming_the_key(text: str, key: str) -> None:
    with pytest.raises(FixtureError, match=key):
        load_world(text)


def test_child_of_unknown_orchestrator_is_an_error() -> None:
    text = (
        "repos:\n  - prefix: x\n    containers:\n      - {name: c, child: {orchestrator: nope}}\n"
    )
    with pytest.raises(FixtureError, match="nope"):
        load_world(text)


def test_outbox_and_accounts_data_reach_the_world() -> None:
    world = load_world(
        MINIMAL
        + """
outbox:
  demo-a:
    - {id: pr/one.json, kind: pr, title: Add retries, target: demo/demo}
accounts:
  - {agent: claude, group: team, account: ann@example.com#org12345, state: live}
"""
    )
    assert "demo-a" in world.outbox
    assert world.accounts[0]["state"] == "live"
