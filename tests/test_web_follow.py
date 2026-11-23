"""Live transcript follow over `/api/ws`: subscribe, append, unsubscribe.

Through the WebSocket seam with a temporary config home holding a growing
transcript; the file watcher is a fake the test fires, so no timing is involved.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from jailbee.db import get_engine
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import CLOSE_UNAUTHORIZED, PROTOCOL_VERSION
from jailbee.web.ws_follow import MAX_SUBSCRIPTIONS, TOO_MANY_SUBSCRIPTIONS
from tests.web_fakes import (
    FakeClock,
    FakeSourceFactory,
    FakeWatchFactory,
    pin_sequence,
    wait_until,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "claude"
SESSION = "33333333-3333-4333-8333-333333333333"
FIXTURE_SESSION = "11111111-1111-4111-8111-111111111111"
SUBAGENT = "a1b2c3d4e5f6a7b8c"


def record(n: int, kind: str, text: str) -> str:
    message: dict[str, object] = {"role": kind, "content": text}
    if kind == "assistant":
        message["model"] = "claude-opus-5-5"
        message["content"] = [{"type": "text", "text": text}]
    return (
        json.dumps(
            {
                "uuid": f"r{n:07d}-0000-4000-8000-000000000000",
                "timestamp": f"2026-11-02T10:00:{n:02d}.000Z",
                "sessionId": SESSION,
                "type": kind,
                "message": message,
            }
        )
        + "\n"
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / "claude-home"
    shutil.copytree(FIXTURES, target)
    (target / "projects" / "-work-repo" / f"{SESSION}.jsonl").write_text(
        record(1, "user", "Fix the login")
    )
    return target


@pytest.fixture
def transcript(home: Path) -> Path:
    return home / "projects" / "-work-repo" / f"{SESSION}.jsonl"


@pytest.fixture
def watches() -> FakeWatchFactory:
    return FakeWatchFactory()


@pytest.fixture
def started(home: Path, watches: FakeWatchFactory) -> Iterator[tuple[TestClient, str]]:
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    homes = {"alpha": [("claude", home)], "secret": [("claude", home)]}
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        repo_scope=RemoteRepoScope(frozenset({"secret"})),
        agent_homes=homes.get,
        file_watch=watches,
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        token = client.post("/api/auth", json=body).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token


@contextmanager
def connect(client: TestClient, token: str) -> Iterator[WebSocketTestSession]:
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token}))
        assert ws.receive_json()["type"] == "hello"
        yield ws


def follow_key(**key: str | None) -> dict[str, str | None]:
    return {"prefix": "alpha", "agent": "claude", "session_id": SESSION, **key}


def subscribe(
    ws: WebSocketTestSession, offset: int, meta: str | None = None, **key: str | None
) -> None:
    message = {"type": "subscribe", "v": PROTOCOL_VERSION, "key": follow_key(**key)}
    extra = {"meta": meta} if meta else {}
    ws.send_text(json.dumps({**message, "offset": offset, **extra}))


def unsubscribe(ws: WebSocketTestSession, **key: str | None) -> None:
    message = {"type": "unsubscribe", "v": PROTOCOL_VERSION, "key": follow_key(**key)}
    ws.send_text(json.dumps(message))


def rest_offset(
    client: TestClient, url: str = f"/api/repos/alpha/sessions/claude/{SESSION}"
) -> int:
    res = client.get(url)
    assert res.status_code == 200, res.text
    offset: int = res.json()["offset"]
    return offset


def append(path: Path, text: str) -> None:
    with path.open("a") as f:
        f.write(text)


def expect_no_append(ws: WebSocketTestSession) -> None:
    """Prove nothing is queued: an excluded subscribe answers with an error
    that must be the very next message."""
    subscribe(ws, 0, prefix="secret")
    message = ws.receive_json()
    assert message["type"] == "subscribe_error", message


def test_the_rest_view_carries_the_offset_to_follow_from(
    started: tuple[TestClient, str], transcript: Path
) -> None:
    client, _ = started
    assert rest_offset(client) == transcript.stat().st_size


def test_an_appended_entry_arrives_rendered(
    started: tuple[TestClient, str], transcript: Path, watches: FakeWatchFactory
) -> None:
    client, token = started
    offset = rest_offset(client)
    with connect(client, token) as ws:
        subscribe(ws, offset)
        wait_until(lambda: len(watches.open) == 1)
        assert watches.open[0].path == transcript

        append(transcript, record(2, "assistant", "Done **now**."))
        watches.open[0].fire()

        message = ws.receive_json()
        assert message["type"] == "append"
        assert message["v"] == PROTOCOL_VERSION
        assert message["key"] == {
            "prefix": "alpha",
            "agent": "claude",
            "session_id": SESSION,
            "subagent_id": None,
        }
        assert [e["kind"] for e in message["entries"]] == ["assistant"]
        assert "<strong>now</strong>" in message["entries"][0]["html"]
        assert message["offset"] == transcript.stat().st_size


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (None, [("assistant", None)]),
        ("hidden", [("assistant", None)]),
        ("collapsed", [("user", ""), ("assistant", None)]),
        ("shown", [("user", "Job done"), ("assistant", None)]),
    ],
)
def test_live_appends_follow_the_agent_context_setting(
    started: tuple[TestClient, str],
    transcript: Path,
    watches: FakeWatchFactory,
    mode: str | None,
    expected: list[tuple[str, str | None]],
) -> None:
    client, token = started
    offset = rest_offset(client)
    with connect(client, token) as ws:
        subscribe(ws, offset, meta=mode)
        wait_until(lambda: len(watches.open) == 1)

        append(transcript, record(2, "user", "<task-notification>Job done</task-notification>"))
        append(transcript, record(3, "assistant", "Done."))
        watches.open[0].fire()

        message = ws.receive_json()
        got = [
            (
                e["kind"],
                None if e["kind"] != "user" else ("Job done" if "Job done" in e["html"] else ""),
            )
            for e in message["entries"]
        ]
        assert got == expected
        if mode == "collapsed":
            assert message["entries"][0]["label"] == "Task notification"
            assert message["entries"][0]["expandable"] is True
        assert message["offset"] == transcript.stat().st_size


def test_a_partial_line_is_delivered_only_once_complete(
    started: tuple[TestClient, str], transcript: Path, watches: FakeWatchFactory
) -> None:
    client, token = started
    offset = rest_offset(client)
    with connect(client, token) as ws:
        subscribe(ws, offset)
        wait_until(lambda: len(watches.open) == 1)
        line = record(2, "assistant", "Half and half")
        append(transcript, line[:20])
        watches.open[0].fire()
        append(transcript, line[20:])
        watches.open[0].fire()

        message = ws.receive_json()
        assert message["type"] == "append"
        assert len(message["entries"]) == 1
        assert "Half and half" in message["entries"][0]["html"]
        expect_no_append(ws)


def test_entries_written_before_the_subscribe_are_caught_up(
    started: tuple[TestClient, str], transcript: Path
) -> None:
    client, token = started
    offset = rest_offset(client)
    append(transcript, record(2, "assistant", "Written meanwhile"))
    with connect(client, token) as ws:
        subscribe(ws, offset)
        message = ws.receive_json()
        assert message["type"] == "append"
        assert "Written meanwhile" in message["entries"][0]["html"]


def test_after_unsubscribe_nothing_is_sent_and_the_watch_is_gone(
    started: tuple[TestClient, str], transcript: Path, watches: FakeWatchFactory
) -> None:
    client, token = started
    offset = rest_offset(client)
    with connect(client, token) as ws:
        subscribe(ws, offset)
        wait_until(lambda: len(watches.open) == 1)
        watch = watches.open[0]

        unsubscribe(ws)
        wait_until(lambda: watch.closed)
        append(transcript, record(2, "assistant", "Nobody listens"))
        watch.on_change()  # a late event from the closed watch's thread
        expect_no_append(ws)
        assert watches.open == []


def test_a_disconnect_removes_the_watch(
    started: tuple[TestClient, str], watches: FakeWatchFactory
) -> None:
    client, token = started
    offset = rest_offset(client)
    with connect(client, token) as ws:
        subscribe(ws, offset)
        wait_until(lambda: len(watches.open) == 1)
    wait_until(lambda: watches.open == [])


def test_two_browsers_share_one_watch_until_the_last_leaves(
    started: tuple[TestClient, str], transcript: Path, watches: FakeWatchFactory
) -> None:
    client, token = started
    offset = rest_offset(client)
    with connect(client, token) as ws_b:
        with connect(client, token) as ws_a:
            subscribe(ws_a, offset)
            subscribe(ws_b, offset)
            expect_no_append(ws_a)
            expect_no_append(ws_b)
            assert len(watches.open) == 1

            append(transcript, record(2, "assistant", "For both"))
            watches.open[0].fire()
            assert "For both" in ws_a.receive_json()["entries"][0]["html"]
            assert "For both" in ws_b.receive_json()["entries"][0]["html"]

        expect_no_append(ws_b)
        assert len(watches.open) == 1
    wait_until(lambda: watches.open == [])


@pytest.mark.parametrize(
    "key",
    [
        {"prefix": "secret"},
        {"prefix": "nowhere"},
        {"agent": "codex"},
        {"session_id": "44444444-4444-4444-8444-444444444444"},
        {"session_id": "../../etc/passwd"},
        {"subagent_id": "../x"},
    ],
)
def test_an_unknown_or_hidden_session_is_refused_and_not_watched(
    started: tuple[TestClient, str], watches: FakeWatchFactory, key: dict[str, str]
) -> None:
    client, token = started
    with connect(client, token) as ws:
        subscribe(ws, 0, **key)
        message = ws.receive_json()
        assert message["type"] == "subscribe_error"
        assert message["message"] == "Unknown session"
        assert "entries" not in message
        assert watches.made == []


def test_a_subagent_transcript_can_be_followed(
    started: tuple[TestClient, str], home: Path, watches: FakeWatchFactory
) -> None:
    client, token = started
    url = f"/api/repos/alpha/sessions/claude/{FIXTURE_SESSION}/subagents/{SUBAGENT}"
    offset = rest_offset(client, url)
    with connect(client, token) as ws:
        subscribe(ws, offset, session_id=FIXTURE_SESSION, subagent_id=SUBAGENT)
        wait_until(lambda: len(watches.open) == 1)
        path = watches.open[0].path
        assert path.name == f"agent-{SUBAGENT}.jsonl"

        append(path, record(9, "assistant", "Subagent speaks"))
        watches.open[0].fire()
        message = ws.receive_json()
        assert message["key"]["subagent_id"] == SUBAGENT
        assert "Subagent speaks" in message["entries"][0]["html"]


def test_subscribe_as_first_message_is_still_a_bad_handshake(
    started: tuple[TestClient, str],
) -> None:
    client, _ = started
    with client.websocket_connect("/api/ws") as ws:
        subscribe(ws, 0)
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_a_revoked_session_gets_no_more_appends_and_is_closed(
    started: tuple[TestClient, str], transcript: Path, watches: FakeWatchFactory
) -> None:
    client, token = started
    offset = rest_offset(client)
    with connect(client, token) as ws:
        subscribe(ws, offset)
        wait_until(lambda: len(watches.open) == 1)
        assert client.post("/api/logout").status_code == 200

        append(transcript, record(2, "assistant", "Secret reply"))
        watches.open[0].fire()

        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == CLOSE_UNAUTHORIZED


def test_subscriptions_per_socket_are_capped(
    started: tuple[TestClient, str], transcript: Path, watches: FakeWatchFactory
) -> None:
    client, token = started
    ids = [f"{SESSION[:-12]}{n:012d}" for n in range(MAX_SUBSCRIPTIONS + 1)]
    for session_id in ids:
        (transcript.parent / f"{session_id}.jsonl").write_text(record(1, "user", "hi"))
    with connect(client, token) as ws:
        for session_id in ids[:MAX_SUBSCRIPTIONS]:
            subscribe(ws, 0, session_id=session_id)
            assert ws.receive_json()["type"] == "append"

        subscribe(ws, 0, session_id=ids[-1])

        refusal = ws.receive_json()
        assert refusal["type"] == "subscribe_error"
        assert refusal["message"] == TOO_MANY_SUBSCRIPTIONS
        assert len(watches.open) == MAX_SUBSCRIPTIONS
        # Re-subscribing an existing key is not a new subscription.
        subscribe(ws, 0, session_id=ids[0])
        assert ws.receive_json()["type"] == "append"


def test_a_failing_read_is_logged_not_lost(
    tmp_path: Path, watches: FakeWatchFactory, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio

    from jailbee.web.protocol import FollowKey
    from jailbee.web.transcript_follow import TranscriptFollows

    class Broken:
        def read_from(self, path: Path, offset: int) -> object:
            raise OSError("disk on fire")

    async def run() -> None:
        follows = TranscriptFollows(watches, lambda entries, meta: [])

        async def deliver(entries: object, offset: int) -> None:
            raise AssertionError("nothing to deliver")

        key = FollowKey(prefix="alpha", agent="claude", session_id=SESSION)
        broken: Any = Broken()  # a source that only fails
        follower = follows.follow(key, broken, tmp_path / "t.jsonl", 0, deliver)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if caplog.records:
                break
        follows.unfollow(follower)

    with caplog.at_level("ERROR", logger="jailbee.web.transcript_follow"):
        asyncio.run(run())

    assert any(r.exc_info and "disk on fire" in str(r.exc_info[1]) for r in caplog.records)


@pytest.mark.timeout(10)
def test_a_batch_of_only_hidden_records_still_advances_the_offset(
    started: tuple[TestClient, str], transcript: Path, watches: FakeWatchFactory
) -> None:
    """Hidden mode renders nothing for it, but the browser must learn the new
    offset, or a reconnect re-reads the batch."""
    client, token = started
    offset = rest_offset(client)
    with connect(client, token) as ws:
        subscribe(ws, offset, meta="hidden")
        wait_until(lambda: len(watches.open) == 1)

        append(transcript, record(2, "user", "<task-notification>Job done</task-notification>"))
        watches.open[0].fire()

        message = ws.receive_json()
        assert message["type"] == "append"
        assert message["entries"] == []
        assert message["offset"] == transcript.stat().st_size
