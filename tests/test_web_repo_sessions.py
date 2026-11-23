"""`GET /api/repos/{prefix}/sessions`: a repository's agent sessions, newest first.

Through the REST seam with a temporary config home holding the fixture
transcripts; the home resolver is injected, so no host config is read.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from jailbee.db import get_engine
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "claude"
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"


class Homes:
    """A resolver over a fixed table; counts how often it is asked."""

    def __init__(self, table: dict[str, Sequence[tuple[str, Path]]]) -> None:
        self.table = table
        self.calls: list[str] = []

    def __call__(self, prefix: str) -> Sequence[tuple[str, Path]] | None:
        self.calls.append(prefix)
        return self.table.get(prefix)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / "claude-home"
    shutil.copytree(FIXTURES, target)
    return target


def open_client(homes: Homes, excluded: frozenset[str] = frozenset()) -> Iterator[TestClient]:
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        repo_scope=RemoteRepoScope(excluded),
        agent_homes=homes,
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        token = client.post("/api/auth", json=body).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


@pytest.fixture
def homes(home: Path) -> Homes:
    return Homes({"alpha": [("claude", home)], "secret": [("claude", home)], "empty": []})


@pytest.fixture
def client(homes: Homes) -> Iterator[TestClient]:
    yield from open_client(homes, frozenset({"secret"}))


def test_lists_sessions_newest_first_with_title_activity_and_models(client: TestClient) -> None:
    res = client.get("/api/repos/alpha/sessions")
    assert res.status_code == 200
    rows = res.json()["sessions"]
    assert [r["session_id"] for r in rows] == [SESSION_A, SESSION_B]  # Nov 1 before Oct 30
    first = rows[0]
    assert first["agent"] == "claude"
    assert first["last_activity"].startswith("2026-11-01T")
    assert first["title"]
    assert isinstance(first["models"], list) and first["models"]
    assert rows[1]["title"] == "hello there"


def test_session_list_shows_the_typed_prompt_after_slash_commands(
    client: TestClient, home: Path
) -> None:
    path = home / "projects" / "-work-repo" / f"{SESSION_A}.jsonl"
    texts = [
        "<command-name>/model</command-name> <command-message>model</command-message>",
        "<local-command-caveat>Local only</local-command-caveat>",
        "<local-command-stdout>Model changed</local-command-stdout>",
        "Fix the session titles",
    ]
    path.write_text(
        "".join(json.dumps({"type": "user", "message": {"content": text}}) + "\n" for text in texts)
    )

    res = client.get("/api/repos/alpha/sessions")

    assert res.status_code == 200
    row = next(r for r in res.json()["sessions"] if r["session_id"] == SESSION_A)
    assert row["title"] == "Fix the session titles"


def test_a_repo_without_agents_has_an_empty_list(client: TestClient) -> None:
    res = client.get("/api/repos/empty/sessions")
    assert res.status_code == 200
    assert res.json() == {"sessions": []}


def test_excluded_and_unknown_repos_give_the_same_404(client: TestClient, homes: Homes) -> None:
    excluded = client.get("/api/repos/secret/sessions")
    unknown = client.get("/api/repos/nope/sessions")
    assert excluded.status_code == unknown.status_code == 404
    assert excluded.json() == unknown.json()
    assert "secret" not in homes.calls  # never even resolved


def test_requires_login(client: TestClient) -> None:
    client.headers.pop("Authorization")
    assert client.get("/api/repos/alpha/sessions").status_code == 401


def test_an_unreadable_home_lists_nothing(tmp_path: Path) -> None:
    homes = Homes({"alpha": [("claude", tmp_path / "missing")]})
    for client in open_client(homes):
        assert client.get("/api/repos/alpha/sessions").json() == {"sessions": []}


def test_a_changed_transcript_is_reread(client: TestClient, home: Path) -> None:
    path = home / "projects" / "-work-repo-other" / f"{SESSION_B}.jsonl"
    before = client.get("/api/repos/alpha/sessions").json()["sessions"]
    assert before[1]["last_activity"].startswith("2026-10-30T")
    line = (
        '{"type": "user", "timestamp": "2026-11-05T09:00:00.000Z",'
        ' "message": {"role": "user", "content": "later"}}\n'
    )
    with path.open("a") as f:
        f.write(line)
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))
    after = client.get("/api/repos/alpha/sessions").json()["sessions"]
    assert after[0]["session_id"] == SESSION_B
    assert after[0]["last_activity"].startswith("2026-11-05T")


def test_without_an_injected_resolver_homes_come_from_the_snapshot(home: Path) -> None:
    import json

    from jailbee.web.protocol import PROTOCOL_VERSION
    from tests.web_fakes import container, repo_group, snapshot_of

    factory = FakeSourceFactory()
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=factory,
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        repo_scope=RemoteRepoScope(frozenset()),
    )
    group = repo_group("alpha", container("alpha-1"), container("alpha-2"))
    group.agent_config_homes = (("alpha-1", "claude", home), ("alpha-2", "claude", home))
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        token = client.post("/api/auth", json=body).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/repos/alpha/sessions", headers=headers).status_code == 404
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token}))
            ws.receive_json()
            factory.only.push(snapshot_of(group))
            ws.receive_text()
            res = client.get("/api/repos/alpha/sessions", headers=headers)
    assert [r["session_id"] for r in res.json()["sessions"]] == [SESSION_A, SESSION_B]


def _same_stamp_rewrite(path: Path, old: str, new: str) -> None:
    """Change the transcript's content but keep its size and mtime."""
    assert len(old) == len(new)
    stat = path.stat()
    path.write_text(path.read_text().replace(old, new))
    assert path.stat().st_size == stat.st_size
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def test_a_restarted_server_reuses_the_session_info_it_read_before(
    homes: Homes, home: Path
) -> None:
    path = home / "projects" / "-work-repo-other" / f"{SESSION_B}.jsonl"
    for client in open_client(homes):
        rows = client.get("/api/repos/alpha/sessions").json()["sessions"]
        assert rows[1]["title"] == "hello there"
    _same_stamp_rewrite(path, "hello there", "HELLO THERE")

    for client in open_client(homes):
        rows = client.get("/api/repos/alpha/sessions").json()["sessions"]

    # Unchanged size and mtime: the stored row answers, the file is not reread.
    assert rows[1]["title"] == "hello there"


def test_a_restarted_server_rereads_a_transcript_that_changed_since(
    homes: Homes, home: Path
) -> None:
    path = home / "projects" / "-work-repo-other" / f"{SESSION_B}.jsonl"
    for client in open_client(homes):
        client.get("/api/repos/alpha/sessions")
    _same_stamp_rewrite(path, "hello there", "HELLO THERE")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    for client in open_client(homes):
        rows = client.get("/api/repos/alpha/sessions").json()["sessions"]

    assert rows[1]["title"] == "HELLO THERE"
