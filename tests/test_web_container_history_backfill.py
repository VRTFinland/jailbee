"""A container's history includes sessions the recorder never saw (#152 ticket 28).

Through the REST seam: containers with a branch label, creation time, repo
path and Incus uuid in a fake snapshot; transcripts written to a temporary
config home with the `cwd` / `gitBranch` Claude Code records them with;
recorder rows in the test database.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlmodel import Session

from jailbee.agent_status import AgentSummary
from jailbee.db import get_engine
from jailbee.db.models import ContainerSession
from jailbee.lifecycle import ContainerInfo
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

REPO = "/home/dev/alpha"
CREATED = datetime(2026, 11, 1, tzinfo=UTC)


def sid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000000"


def write_session(
    home: Path,
    n: int,
    *,
    started: str,
    branch: str | None = "main",
    cwd: str = REPO,
    title: str | None = None,
) -> Path:
    path = home / "projects" / cwd.replace("/", "-") / f"{sid(n)}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "type": "user",
        "uuid": f"u{n}",
        "timestamp": started,
        "sessionId": sid(n),
        "cwd": cwd,
        "message": {"role": "user", "content": title or f"session {n}"},
    }
    if branch is not None:
        record["gitBranch"] = branch
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def box(
    name: str,
    *,
    branch: str | None = "main",
    created: datetime | None = CREATED,
    live: tuple[str, ...] = (),
    repo_dir: str | None = REPO,
) -> ContainerInfo:
    status = (
        (AgentSummary("claude", "busy", None, None, len(live), session_ids=live),) if live else ()
    )
    return container(
        name,
        uuid=f"uuid-{name}",
        branch=branch,
        created_at=created,
        repo_dir=repo_dir,
        agent_status=status,
    )


def open_client(
    home: Path, containers: list[ContainerInfo], *recorded: ContainerSession
) -> Iterator[TestClient]:
    engine = get_engine()
    with Session(engine) as db:
        db.add_all(recorded)
        db.commit()
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(initial=snapshot_of(repo_group("alpha", *containers))),
        engine=engine,
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        repo_scope=RemoteRepoScope(frozenset()),
        agent_homes={"alpha": [("claude", home)]}.get,
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        token = client.post("/api/auth", json=body).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        # The repo list reads the snapshot a browser's WebSocket holds.
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token}))
            ws.receive_json()  # hello
            yield client


def history(home: Path, containers: list[ContainerInfo], name: str, *recorded: ContainerSession):
    for client in open_client(home, containers, *recorded):
        res = client.get(f"/api/containers/{name}/sessions/history")
        assert res.status_code == 200
        return [r["session_id"] for r in res.json()["sessions"]]
    raise AssertionError("no client")


def repo_marks(home: Path, containers: list[ContainerInfo], *recorded: ContainerSession):
    for client in open_client(home, containers, *recorded):
        rows = client.get("/api/repos/alpha/sessions").json()["sessions"]
        return {r["session_id"]: r["container"] for r in rows}
    raise AssertionError("no client")


def seen(name: str, n: int) -> ContainerSession:
    stamp = datetime(2026, 11, 20, tzinfo=UTC)
    return ContainerSession(
        container_name=name,
        container_uuid=f"uuid-{name}",
        agent="claude",
        session_id=sid(n),
        first_seen=stamp,
        last_seen=stamp,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "claude-home"


def test_a_session_on_the_containers_branch_and_path_is_in_its_history(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")
    write_session(home, 2, started="2026-11-06T10:00:00Z", cwd=f"{REPO}/src")

    assert history(home, [box("alpha-main")], "alpha-main") == [sid(2), sid(1)]


def test_the_backfilled_session_has_its_title_and_activity(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z", title="Fix the parser")

    for client in open_client(home, [box("alpha-main")]):
        (row,) = client.get("/api/containers/alpha-main/sessions/history").json()["sessions"]
        assert row["title"] == "Fix the parser" and row["live"] is False
        assert row["last_activity"].startswith("2026-11-05T10:00")


def test_another_branch_another_path_or_no_branch_is_not_attributed(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z", branch="feat")
    write_session(home, 2, started="2026-11-05T10:00:00Z", branch=None)
    write_session(home, 3, started="2026-11-05T10:00:00Z", cwd="/home/dev/alpha-web")

    assert history(home, [box("alpha-main")], "alpha-main") == []


def test_a_recreated_container_does_not_inherit_sessions_older_than_itself(home: Path) -> None:
    write_session(home, 1, started="2026-10-20T10:00:00Z")  # the old instance's
    write_session(home, 2, started="2026-11-02T10:00:00Z")

    assert history(home, [box("alpha-main")], "alpha-main") == [sid(2)]


def test_a_container_of_unknown_age_or_without_a_branch_label_gets_no_backfill(
    home: Path,
) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")
    boxes = [box("alpha-a", created=None), box("alpha-b", branch=None)]

    assert history(home, boxes, "alpha-a") == []
    assert history(home, boxes, "alpha-b") == []


def test_a_renamed_branch_keeps_only_the_sessions_on_the_labelled_branch(home: Path) -> None:
    # The label is the branch the container was created on; a session started
    # after `git branch -m` names the new branch and stays unattributed.
    write_session(home, 1, started="2026-11-05T10:00:00Z", branch="main")
    write_session(home, 2, started="2026-11-06T10:00:00Z", branch="renamed")

    assert history(home, [box("alpha-main")], "alpha-main") == [sid(1)]


def test_two_containers_on_one_branch_and_path_leave_the_session_unattributed(
    home: Path,
) -> None:
    write_session(home, 1, started="2026-11-12T10:00:00Z")
    boxes = [box("alpha-old"), box("alpha-new", created=datetime(2026, 11, 10, tzinfo=UTC))]

    assert history(home, boxes, "alpha-old") == []
    assert history(home, boxes, "alpha-new") == []


def test_two_containers_created_at_the_same_instant_are_ambiguous(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")
    boxes = [box("alpha-b"), box("alpha-a")]

    assert history(home, boxes, "alpha-a") == []
    assert history(home, boxes, "alpha-b") == []


def test_a_later_container_on_the_same_branch_is_not_a_candidate(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")
    boxes = [box("alpha-old"), box("alpha-new", created=datetime(2026, 11, 10, tzinfo=UTC))]

    assert history(home, boxes, "alpha-old") == [sid(1)]
    assert history(home, boxes, "alpha-new") == []


def test_a_container_without_a_repo_dir_label_is_not_a_candidate(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")

    assert history(home, [box("alpha-main", repo_dir=None)], "alpha-main") == []


def test_a_container_created_at_the_sessions_start_already_existed(home: Path) -> None:
    write_session(home, 1, started=CREATED.strftime("%Y-%m-%dT%H:%M:%SZ"))

    assert history(home, [box("alpha-main")], "alpha-main") == [sid(1)]


def test_recorder_rows_win_over_the_transcripts_metadata(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")  # says main
    boxes = [box("alpha-main"), box("alpha-feat", branch="feat")]

    assert history(home, boxes, "alpha-main", seen("alpha-feat", 1)) == []
    assert history(home, boxes, "alpha-feat", seen("alpha-feat", 1)) == [sid(1)]


def test_a_session_a_gone_container_recorded_is_not_backfilled(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")

    assert history(home, [box("alpha-main")], "alpha-main", seen("alpha-gone", 1)) == []


def test_a_live_session_stays_out_of_the_history(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")

    assert history(home, [box("alpha-main", live=(sid(1),))], "alpha-main") == []


def test_recorded_and_backfilled_sessions_are_listed_newest_first(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")
    write_session(home, 2, started="2026-11-07T10:00:00Z")
    write_session(home, 3, started="2026-11-06T10:00:00Z")

    found = history(home, [box("alpha-main")], "alpha-main", seen("alpha-main", 1))

    assert found == [sid(2), sid(3), sid(1)]


def test_the_repo_list_marks_backfilled_sessions_with_their_container(home: Path) -> None:
    write_session(home, 1, started="2026-11-05T10:00:00Z")
    write_session(home, 2, started="2026-11-05T10:00:00Z", branch="feat")

    assert repo_marks(home, [box("alpha-main")]) == {sid(1): "alpha-main", sid(2): None}


def test_a_restarted_server_attributes_from_the_stored_session_info(home: Path) -> None:
    path = write_session(home, 1, started="2026-11-05T10:00:00Z", branch="mainx")
    boxes = [box("alpha-main", branch="mainx")]
    assert history(home, boxes, "alpha-main") == [sid(1)]
    stat = path.stat()
    path.write_text(path.read_text().replace('"mainx"', '"other"'))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    # Same size and mtime: the stored branch answers, the file is not reread.
    assert history(home, boxes, "alpha-main") == [sid(1)]
