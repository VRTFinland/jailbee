"""Session attribution through the REST seam: the container view and the repo list's marks.

The snapshot comes from a fake source (containers with Incus uuids and live
agent sessions), recorded sessions from `ContainerSession` rows in the test
database, transcripts from the fixture config home.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlmodel import Session

from jailbee.agent_status import AgentSummary
from jailbee.db import get_engine
from jailbee.db.models import ContainerSession
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import PROTOCOL_VERSION
from tests.web_fakes import (
    FakeClock,
    FakeSourceFactory,
    container,
    pin_sequence,
    repo_group,
    snapshot_of,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "claude"
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
STAMP = datetime(2026, 11, 2, 9, tzinfo=UTC)


def seen(name: str, uuid: str, session_id: str, agent: str = "claude") -> ContainerSession:
    return ContainerSession(
        container_name=name,
        container_uuid=uuid,
        agent=agent,
        session_id=session_id,
        first_seen=STAMP,
        last_seen=STAMP,
    )


def live(*session_ids: str) -> tuple[AgentSummary, ...]:
    return (AgentSummary("claude", "busy", None, None, len(session_ids), session_ids=session_ids),)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / "claude-home"
    shutil.copytree(FIXTURES, target)
    return target


def open_client(home: Path, *recorded: ContainerSession) -> Iterator[TestClient]:
    engine = get_engine()
    with Session(engine) as db:
        db.add_all(recorded)
        db.commit()
    snap = snapshot_of(
        repo_group(
            "alpha",
            container("alpha-1", uuid="u-1", agent_status=live(SESSION_A)),
            container("alpha-2", uuid="u-2"),
        ),
        repo_group("secret", container("secret-1", repo="secret", uuid="u-s")),
    )
    homes_table: dict[str, Sequence[tuple[str, Path]]] = {
        "alpha": [("claude", home)],
        "secret": [("claude", home)],
    }
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(initial=snap),
        engine=engine,
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        repo_scope=RemoteRepoScope(frozenset({"secret"})),
        agent_homes=homes_table.get,
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        token = client.post("/api/auth", json=body).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        # The snapshot exists while a browser holds the WebSocket open (the front page does).
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token}))
            ws.receive_json()  # hello
            yield client


@pytest.fixture
def client(home: Path) -> Iterator[TestClient]:
    # alpha-1 is running SESSION_A right now and ran SESSION_B before; a
    # container of the same name but another uuid once ran a third session.
    yield from open_client(
        home,
        seen("alpha-1", "u-1", SESSION_A),
        seen("alpha-1", "u-1", SESSION_B),
        seen("alpha-1", "u-old", "ffffffff-ffff-4fff-8fff-ffffffffffff"),
    )


def test_a_containers_live_sessions_come_from_the_snapshot_alone(client: TestClient) -> None:
    res = client.get("/api/containers/alpha-1/sessions")

    assert res.status_code == 200
    body = res.json()
    assert (body["container"], body["repo"]) == ("alpha-1", "alpha")
    assert [(r["session_id"], r["live"]) for r in body["sessions"]] == [(SESSION_A, True)]
    first = body["sessions"][0]
    assert first["agent"] == "claude" and first["title"]
    assert first["last_activity"].startswith("2026-11-01T")


def test_a_containers_history_is_its_recorded_sessions_that_are_not_live(
    client: TestClient,
) -> None:
    res = client.get("/api/containers/alpha-1/sessions/history")

    assert res.status_code == 200
    body = res.json()
    assert (body["container"], body["repo"]) == ("alpha-1", "alpha")
    assert [(r["session_id"], r["live"]) for r in body["sessions"]] == [(SESSION_B, False)]
    assert body["sessions"][0]["title"] == "hello there"
    assert body["sessions"][0]["models"] == ["jb.codex.standard"]


def test_history_is_bounded_and_pages_are_newest_first(home: Path) -> None:
    records = []
    for n in range(25):
        sid = f"33333333-3333-4333-8333-{n:012d}"
        record = {
            "type": "user",
            "uuid": f"prompt-{n}",
            "timestamp": f"2026-11-{n + 1:02d}T09:00:00Z",
            "sessionId": sid,
            "cwd": "/work/repo",
            "message": {"role": "user", "content": f"Release task {n}"},
        }
        (home / "projects" / "-work-repo" / f"{sid}.jsonl").write_text(json.dumps(record) + "\n")
        records.append(seen("alpha-1", "u-1", sid))
    for client in open_client(home, *records):
        first = client.get("/api/containers/alpha-1/sessions/history").json()
        assert len(first["sessions"]) == 20
        assert first["total"] == 25
        assert (first["limit"], first["offset"]) == (20, 0)
        assert [r["title"] for r in first["sessions"]] == [
            f"Release task {n}" for n in range(24, 4, -1)
        ]
        last = client.get("/api/containers/alpha-1/sessions/history?limit=20&offset=20").json()
        assert [r["title"] for r in last["sessions"]] == [
            f"Release task {n}" for n in range(4, -1, -1)
        ]
        beyond = client.get("/api/containers/alpha-1/sessions/history?offset=100").json()
        assert beyond["sessions"] == [] and beyond["total"] == 25


@pytest.mark.parametrize("query", ["HELLO", "there", SESSION_B, "  hello  "])
def test_history_searches_titles_and_session_ids_without_hiding_live_sessions(
    client: TestClient, query: str
) -> None:
    body = client.get(
        "/api/containers/alpha-1/sessions/history", params={"q": query, "limit": 1}
    ).json()
    assert [r["session_id"] for r in body["sessions"]] == [SESSION_B]
    assert body["total"] == 1
    assert client.get("/api/containers/alpha-1/sessions").json()["sessions"][0]["session_id"] == SESSION_A


def test_history_search_has_an_empty_page_for_no_matches(client: TestClient) -> None:
    body = client.get("/api/containers/alpha-1/sessions/history?q=unfindable").json()
    assert body["sessions"] == [] and body["total"] == 0
    empty = client.get("/api/containers/alpha-2/sessions/history").json()
    assert empty["sessions"] == [] and empty["total"] == 0


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=abc"])
def test_history_rejects_invalid_page_parameters(client: TestClient, query: str) -> None:
    assert client.get(f"/api/containers/alpha-1/sessions/history?{query}").status_code == 422


def test_history_filters_before_paging_and_can_find_missing_transcripts(home: Path) -> None:
    for client in open_client(
        home, seen("alpha-1", "u-1", SESSION_B), seen("alpha-1", "u-1", "gone-from-disk")
    ):
        body = client.get("/api/containers/alpha-1/sessions/history?q=gone&limit=1").json()
        assert [r["session_id"] for r in body["sessions"]] == ["gone-from-disk"]
        assert body["total"] == 1
        assert client.get(
            "/api/containers/alpha-1/sessions/history?q=gone&limit=1&offset=1"
        ).json()["sessions"] == []


def test_a_recreated_container_does_not_inherit_the_old_ones_sessions(client: TestClient) -> None:
    ids = [
        r["session_id"]
        for part in ("sessions", "sessions/history")
        for r in client.get(f"/api/containers/alpha-1/{part}").json()["sessions"]
    ]

    assert "ffffffff-ffff-4fff-8fff-ffffffffffff" not in ids


def test_a_recorded_session_without_a_transcript_is_still_listed(home: Path) -> None:
    for client in open_client(home, seen("alpha-1", "u-1", "gone-from-disk")):
        rows = client.get("/api/containers/alpha-1/sessions/history").json()["sessions"]
        gone = next(r for r in rows if r["session_id"] == "gone-from-disk")
        assert gone["title"] == "" and gone["last_activity"] is None and gone["models"] == []


@pytest.mark.parametrize("part", ["sessions", "sessions/history"])
def test_a_container_without_sessions_has_an_empty_list(client: TestClient, part: str) -> None:
    res = client.get(f"/api/containers/alpha-2/{part}")
    assert res.status_code == 200
    assert res.json()["sessions"] == []


@pytest.mark.parametrize("part", ["sessions", "sessions/history"])
def test_excluded_and_unknown_containers_give_the_same_404(client: TestClient, part: str) -> None:
    excluded = client.get(f"/api/containers/secret-1/{part}")
    unknown = client.get(f"/api/containers/nope/{part}")
    assert excluded.status_code == unknown.status_code == 404
    assert excluded.json() == unknown.json()


@pytest.mark.parametrize("part", ["sessions", "sessions/history"])
def test_the_container_view_requires_login(client: TestClient, part: str) -> None:
    client.headers.pop("Authorization")
    assert client.get(f"/api/containers/alpha-1/{part}").status_code == 401


def test_the_repo_list_marks_each_session_with_its_container_or_none(home: Path) -> None:
    for client in open_client(home, seen("alpha-1", "u-1", SESSION_A)):
        rows = client.get("/api/repos/alpha/sessions").json()["sessions"]
        assert {r["session_id"]: r["container"] for r in rows} == {
            SESSION_A: "alpha-1",
            SESSION_B: None,
        }


@pytest.mark.parametrize(("newer", "older"), [("alpha-1", "alpha-2"), ("alpha-2", "alpha-1")])
def test_a_session_two_containers_saw_belongs_to_the_one_that_saw_it_last(
    home: Path, newer: str, older: str
) -> None:
    uuids = {"alpha-1": "u-1", "alpha-2": "u-2"}
    recent = seen(newer, uuids[newer], SESSION_B)
    recent.last_seen = datetime(2026, 11, 10, tzinfo=UTC)
    stale = seen(older, uuids[older], SESSION_B)
    stale.last_seen = datetime(2026, 11, 5, tzinfo=UTC)

    for client in open_client(home, recent, stale):
        rows = client.get("/api/repos/alpha/sessions").json()["sessions"]
        assert {r["session_id"]: r["container"] for r in rows}[SESSION_B] == newer
