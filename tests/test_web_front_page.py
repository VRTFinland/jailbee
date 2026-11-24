"""The front page over `/api/ws`: repo groups, dashboard-core cells, agent state, repo scope.

The expected cells come from the dashboard core (`dashboard.columns` /
`dashboard.format`) and from literals, never from the web view's own code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from rich.text import Text

from jailbee.accounts.models import AgentActivity
from jailbee.agent_status import AgentSummary, SessionMark
from jailbee.dashboard import format as dfmt
from jailbee.dashboard.columns import default_columns, visible_fields
from jailbee.db import get_engine
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import PROTOCOL_VERSION
from tests.web_fakes import (
    START,
    FakeClock,
    FakeSourceFactory,
    container,
    pin_sequence,
    repo_group,
    snapshot_of,
)


def agent(state: str) -> AgentSummary:
    return AgentSummary(
        agent="claude", state=state, since=START - timedelta(minutes=5), waiting_for=None, count=1
    )


@pytest.fixture
def factory() -> FakeSourceFactory:
    return FakeSourceFactory()


def open_client(
    factory: FakeSourceFactory, excluded: frozenset[str] = frozenset()
) -> Iterator[tuple[TestClient, str]]:
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=factory,
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        repo_scope=RemoteRepoScope(excluded),
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        yield client, client.post("/api/auth", json=body).json()["token"]


@pytest.fixture
def started(factory: FakeSourceFactory) -> Iterator[tuple[TestClient, str]]:
    yield from open_client(factory)


def first_page(client: TestClient, token: str, factory: FakeSourceFactory, snap: Any) -> str:
    """The raw text of the first front-page message after pushing ``snap``."""
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token}))
        ws.receive_json()  # hello
        factory.only.push(snap)
        return ws.receive_text()


def test_snapshot_includes_shared_filter_membership_and_context(
    factory: FakeSourceFactory,
) -> None:
    from jailbee.orchestration.view import ChildView

    parent = container("alpha-parent", state="Stopped")
    child = container(
        "alpha-child",
        agent_status=(agent("busy"),),
        child=ChildView(parent.name, "working", (), False, ()),
    )
    snap = snapshot_of(
        repo_group("alpha", parent, child, container("alpha-idle")),
        repo_group(
            "hidden", container("hidden-busy", repo="hidden", agent_status=(agent("busy"),))
        ),
    )
    for client, token in open_client(factory, excluded=frozenset({"hidden"})):
        raw = first_page(client, token, factory, snap)
    assert "hidden-busy" not in raw
    view = json.loads(raw)["view"]
    assert view["filters"]["running"]["context"] == [parent.name]
    assert set(view["filters"]["running"]["visible"]) == {
        parent.name,
        child.name,
        "alpha-idle",
    }
    assert set(view["filters"]["active"]["visible"]) == {parent.name, child.name}
    assert view["filters"]["active"]["context"] == [parent.name]
    assert view["filters"]["active"]["indents"] == {parent.name: "", child.name: "└ "}


def test_recent_idle_emphasis_and_age_survive_the_snapshot(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    fresh = AgentSummary(
        "claude",
        "idle",
        START - timedelta(hours=2),
        None,
        20,
        activity=AgentActivity(
            None, None, state="idle", last_event_at=START - timedelta(minutes=5)
        ),
    )
    # A second session can be fresh even when the summary's first session is old.
    mixed = AgentSummary(
        "claude",
        "idle",
        START - timedelta(hours=2),
        None,
        2,
        sessions=(
            SessionMark("idle", START - timedelta(hours=2)),
            SessionMark("idle", START - timedelta(minutes=7)),
        ),
    )
    snap = snapshot_of(
        repo_group(
            "alpha",
            container("alpha-fresh", agent_status=(fresh,)),
            container("alpha-mixed", agent_status=(mixed,)),
            container("alpha-old", agent_status=(agent("idle"),)),
        )
    )
    view = json.loads(first_page(client, token, factory, snap))["view"]
    rows = {row["name"]: row for row in view["repos"][0]["containers"]}
    assert rows["alpha-fresh"]["agent_recent_idle"] == "5m"
    assert rows["alpha-mixed"]["agent_recent_idle"] == "7m"
    assert rows["alpha-old"]["agent_recent_idle"] is None
    assert set(view["filters"]["active"]["visible"]) == {"alpha-fresh", "alpha-mixed"}


def test_containers_are_grouped_under_their_repo(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    snap = snapshot_of(
        repo_group("alpha", container("alpha-1"), container("alpha-2")),
        repo_group("beta", container("beta-1", repo="beta")),
    )
    view = json.loads(first_page(client, token, factory, snap))["view"]
    assert [(r["prefix"], [c["name"] for c in r["containers"]]) for r in view["repos"]] == [
        ("alpha", ["alpha-1", "alpha-2"]),
        ("beta", ["beta-1"]),
    ]


def test_orchestration_relation_survives_the_web_snapshot(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    from jailbee.orchestration.view import ChildView, GrantView

    client, token = started
    parent = container("alpha-orch", grant=GrantView(4, (), (), False, ()))
    child = container(
        "alpha-task",
        child=ChildView("alpha-orch", "working", (), False, (), "codex/opus"),
    )
    snap = snapshot_of(repo_group("alpha", child, parent))
    page = json.loads(first_page(client, token, factory, snap))
    assert page["v"] == PROTOCOL_VERSION
    rows = page["view"]["repos"][0]["containers"]
    assert [row["name"] for row in rows] == ["alpha-orch", "alpha-task"]
    assert rows[0]["orchestrator"] is True
    assert rows[0]["child_count"] == 1
    assert rows[0]["child"] is None
    assert rows[1]["depth"] == 1
    assert rows[1]["child"] == {
        "orchestrator": "alpha-orch",
        "orchestrator_short_name": "orch",
        "state": "working",
        "runner": "codex/opus",
    }


def test_interleaved_children_stay_with_their_own_orchestrator(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    from jailbee.orchestration.view import ChildView, GrantView

    client, token = started
    snap = snapshot_of(
        repo_group(
            "alpha",
            container("alpha-a", grant=GrantView(4, (), (), False, ())),
            container("alpha-b", grant=GrantView(4, (), (), False, ())),
            container("alpha-a1", child=ChildView("alpha-a", "working", (), False, ())),
            container("alpha-b1", child=ChildView("alpha-b", "working", (), False, ())),
            container("alpha-a2", child=ChildView("alpha-a", "working", (), False, ())),
            container("alpha-b2", child=ChildView("alpha-b", "working", (), False, ())),
        )
    )
    view = json.loads(first_page(client, token, factory, snap))["view"]
    rows = view["repos"][0]["containers"]
    assert [(row["name"], row["depth"], row["child_count"]) for row in rows] == [
        ("alpha-a", 0, 2),
        ("alpha-a1", 1, 0),
        ("alpha-a2", 1, 0),
        ("alpha-b", 0, 2),
        ("alpha-b1", 1, 0),
        ("alpha-b2", 1, 0),
    ]


def test_absent_orchestrator_keeps_the_child_relation(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    from jailbee.orchestration.view import ChildView

    client, token = started
    child = container(
        "alpha-task", child=ChildView("alpha-missing-parent", "imported", (), False, ())
    )
    snap = snapshot_of(repo_group("alpha", child))
    row = json.loads(first_page(client, token, factory, snap))["view"]["repos"][0]["containers"][0]
    assert row["depth"] == 0
    assert row["child"] == {
        "orchestrator": "alpha-missing-parent",
        "orchestrator_short_name": "missing-parent",
        "state": "imported",
        "runner": None,
    }


def test_child_removal_updates_the_orchestrators_count(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    from jailbee.orchestration.view import ChildView, GrantView

    client, token = started
    parent = container("alpha-orch", grant=GrantView(4, (), (), False, ()))
    child = container("alpha-task", child=ChildView("alpha-orch", "working", (), False, ()))
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token}))
        ws.receive_json()
        factory.only.push(snapshot_of(repo_group("alpha", parent, child), seq=1))
        assert ws.receive_json()["view"]["repos"][0]["containers"][0]["child_count"] == 1
        factory.only.push(snapshot_of(repo_group("alpha", parent), seq=2))
        rows = ws.receive_json()["view"]["repos"][0]["containers"]
        assert [row["name"] for row in rows] == ["alpha-orch"]
        assert rows[0]["child_count"] == 0


def test_cells_equal_the_dashboard_core_for_the_same_snapshot(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    info = container("alpha-1", agent_status=(agent("waiting"),))
    page = json.loads(first_page(client, token, factory, snapshot_of(repo_group("alpha", info))))
    view = page["view"]
    row = view["repos"][0]["containers"][0]

    specs = visible_fields(START, [info], default_columns())
    shown = {c["name"]: c["header"] for c in view["columns"]}
    assert "name" in shown
    assert "agent_compact" in shown, "the agent state is a default column"
    cells = dict(zip((c["name"] for c in view["columns"]), row["cells"], strict=True))
    for spec in specs:
        if spec.name not in shown:
            continue  # the core drops all-empty columns; the web page follows
        assert shown[spec.name] == dfmt.dashboard_header(spec)
        assert cells[spec.name] == Text.from_markup(dfmt.dashboard_cell(spec, info, START)).plain
    # Known-good literals, independent of the core's code.
    assert cells["name"] == "1", "the repo prefix is the heading, not part of the cell"
    assert row["name"] == "alpha-1", "the full name stays the row identity"
    assert cells["state"] == "▶"
    assert cells["agent_compact"].startswith("◆")


def test_row_carries_an_api_error_state_and_cell(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    info = container("alpha-1", agent_status=(agent("error:<b>quota"),))
    view = json.loads(first_page(client, token, factory, snapshot_of(repo_group("alpha", info))))[
        "view"
    ]
    row = view["repos"][0]["containers"][0]
    cells = dict(zip((c["name"] for c in view["columns"]), row["cells"], strict=True))
    assert row["agent"] == "error:<b>quota"  # the browser renders it as text
    assert cells["agent_compact"].startswith("✖ <b>quota")


@pytest.mark.parametrize("state", ["waiting", "busy", "idle"])
def test_row_carries_the_agent_state(
    started: tuple[TestClient, str], factory: FakeSourceFactory, state: str
) -> None:
    client, token = started
    info = container("alpha-1", agent_status=(agent(state),))
    view = json.loads(first_page(client, token, factory, snapshot_of(repo_group("alpha", info))))[
        "view"
    ]
    assert view["repos"][0]["containers"][0]["agent"] == state


def test_row_without_an_agent_has_no_agent_state(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    snap = snapshot_of(repo_group("alpha", container("alpha-1")))
    view = json.loads(first_page(client, token, factory, snap))["view"]
    assert view["repos"][0]["containers"][0]["agent"] is None


def test_row_carries_state_and_no_phase_at_rest(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    snap = snapshot_of(repo_group("alpha", container("alpha-1")))
    row = json.loads(first_page(client, token, factory, snap))["view"]["repos"][0]["containers"][0]
    assert row["state"] == "Running"
    assert row["phase"] is None


def test_stopped_row_carries_its_state(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    snap = snapshot_of(repo_group("alpha", container("alpha-1", state="Stopped")))
    row = json.loads(first_page(client, token, factory, snap))["view"]["repos"][0]["containers"][0]
    assert row["state"] == "Stopped"


@pytest.mark.parametrize(
    ("fields", "phase"),
    [
        ({"job_kind": "boot", "job_phase": "starting", "job_pid": 1, "job_live": True}, "booting"),
        ({"job_kind": "autostart", "job_phase": "starting", "job_pid": 1}, "autostart"),
        ({"job_kind": "create", "job_phase": "creating", "job_pid": 1}, "job"),
        ({"autostart_failed": True}, "failed"),
    ],
)
def test_row_carries_the_shared_row_phase(
    started: tuple[TestClient, str],
    factory: FakeSourceFactory,
    fields: dict[str, Any],
    phase: str,
) -> None:
    client, token = started
    info = container("alpha-1", **fields)
    snap = snapshot_of(repo_group("alpha", info))
    row = json.loads(first_page(client, token, factory, snap))["view"]["repos"][0]["containers"][0]
    assert row["phase"] == phase


def test_excluded_repo_appears_nowhere_in_the_message(factory: FakeSourceFactory) -> None:
    snap = snapshot_of(
        repo_group("alpha", container("alpha-1")),
        repo_group("secret", container("secret-box", repo="secret")),
    )
    for client, token in open_client(factory, frozenset({"secret"})):
        raw = first_page(client, token, factory, snap)
    assert "alpha-1" in raw
    assert "secret" not in raw


def test_excluded_orchestrator_is_redacted_from_a_visible_child(
    factory: FakeSourceFactory,
) -> None:
    from jailbee.orchestration.view import ChildView

    snap = snapshot_of(
        repo_group(
            "alpha",
            container("alpha-task", child=ChildView("secret-orch", "working", (), False, ())),
        ),
        repo_group("secret", container("secret-orch", repo="secret")),
    )
    for client, token in open_client(factory, frozenset({"secret"})):
        raw = first_page(client, token, factory, snap)
    assert "secret" not in raw
    relation = json.loads(raw)["view"]["repos"][0]["containers"][0]["child"]
    assert relation["orchestrator"] is None
    assert relation["orchestrator_short_name"] == "hidden orchestrator"


def test_present_orchestrator_repo_wins_over_an_excluded_longer_prefix(
    factory: FakeSourceFactory,
) -> None:
    from jailbee.orchestration.view import ChildView

    snap = snapshot_of(
        repo_group(
            "web",
            container("web-admin-fix", repo="web"),
            container(
                "web-task", repo="web", child=ChildView("web-admin-fix", "working", (), False, ())
            ),
        ),
        repo_group("web-admin"),
    )
    for client, token in open_client(factory, frozenset({"web-admin"})):
        view = json.loads(first_page(client, token, factory, snap))["view"]
    child = next(row for row in view["repos"][0]["containers"] if row["name"] == "web-task")
    assert child["child"]["orchestrator"] == "web-admin-fix"
    assert child["child"]["orchestrator_short_name"] == "admin-fix"


def test_orchestrator_counts_children_across_visible_repositories(
    factory: FakeSourceFactory,
) -> None:
    from jailbee.orchestration.view import ChildView

    snap = snapshot_of(
        repo_group("alpha", container("alpha-orch")),
        repo_group(
            "beta",
            container(
                "beta-task", repo="beta", child=ChildView("alpha-orch", "working", (), False, ())
            ),
        ),
        repo_group(
            "secret",
            container(
                "secret-task",
                repo="secret",
                child=ChildView("alpha-orch", "working", (), False, ()),
            ),
        ),
    )
    for client, token in open_client(factory, frozenset({"secret"})):
        view = json.loads(first_page(client, token, factory, snap))["view"]
    assert view["repos"][0]["containers"][0]["child_count"] == 1
    assert "secret" not in json.dumps(view)


def test_cross_repo_orchestrator_uses_its_own_repo_prefix(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    from jailbee.orchestration.view import ChildView

    client, token = started
    snap = snapshot_of(
        repo_group(
            "alpha",
            container("alpha-task", child=ChildView("other-repo-orch", "working", (), False, ())),
        ),
        repo_group("other-repo"),
    )
    page = json.loads(first_page(client, token, factory, snap))
    child = page["view"]["repos"][0]["containers"][0]["child"]
    assert child["orchestrator_short_name"] == "orch"


def test_the_page_updates_on_each_snapshot(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token}))
        ws.receive_json()
        factory.only.push(snapshot_of(repo_group("alpha", container("a1")), seq=1))
        first = ws.receive_json()["view"]
        factory.only.push(snapshot_of(repo_group("alpha", container("a2")), seq=2))
        second = ws.receive_json()["view"]
    assert [c["name"] for c in first["repos"][0]["containers"]] == ["a1"]
    assert [c["name"] for c in second["repos"][0]["containers"]] == ["a2"]
