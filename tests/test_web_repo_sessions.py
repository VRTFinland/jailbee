"""`GET /api/repos/{prefix}/sessions`: a repository's agent sessions, newest first.

Through the REST seam with a temporary config home holding the fixture
transcripts; the home resolver is injected, so no host config is read.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
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


def open_client(
    homes: Homes,
    excluded: frozenset[str] = frozenset(),
    *,
    snapshots: FakeSourceFactory | None = None,
    warm: bool = False,
) -> Iterator[TestClient]:
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=snapshots or FakeSourceFactory(),
        warm_session_cache=warm,
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


def test_session_summary_returns_metadata_without_transcript_entries(client: TestClient) -> None:
    response = client.get(f"/api/repos/alpha/sessions/claude/{SESSION_A}/summary")
    assert response.status_code == 200
    assert response.json() == {
        "session_id": SESSION_A,
        "agent": "claude",
        "title": "Fix the login bug in the auth module",
        "last_activity": "2026-11-01T10:05:01Z",
        "models": ["claude-opus-5-5", "claude-sonnet-5"],
        "container": None,
        "live": False,
    }


@pytest.mark.parametrize(
    "prefix,agent,session",
    [
        ("secret", "claude", SESSION_A),
        ("nope", "claude", SESSION_A),
        ("alpha", "codex", SESSION_A),
        ("alpha", "claude", "missing"),
    ],
)
def test_session_summary_hides_unknown_and_excluded_sessions(
    client: TestClient, homes: Homes, prefix: str, agent: str, session: str
) -> None:
    response = client.get(f"/api/repos/{prefix}/sessions/{agent}/{session}/summary")
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown session"}
    assert "secret" not in homes.calls


def test_session_summary_requires_login(client: TestClient) -> None:
    client.headers.pop("Authorization")
    assert client.get(f"/api/repos/alpha/sessions/claude/{SESSION_A}/summary").status_code == 401


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
    # The fixture sessions were started in /work/repo.
    group = repo_group("alpha", container("alpha-1", repo_dir="/work/repo"), container("alpha-2"))
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


def _snapshots(*prefixes: str) -> FakeSourceFactory:
    from tests.web_fakes import container, repo_group, snapshot_of

    groups = [repo_group(p, container(f"{p}-1", repo=p)) for p in prefixes]
    return FakeSourceFactory(initial=snapshot_of(*groups))


def _warmed(client: TestClient) -> None:
    assert client.app.state.web.sessions_warmed.wait(10)  # type: ignore[attr-defined]  # Starlette's app is typed as ASGIApp


def test_the_server_reads_session_info_at_start_up_before_any_list_is_asked(
    homes: Homes, home: Path
) -> None:
    path = home / "projects" / "-work-repo-other" / f"{SESSION_B}.jsonl"
    for client in open_client(homes, snapshots=_snapshots("alpha"), warm=True):
        _warmed(client)
        _same_stamp_rewrite(path, "hello there", "HELLO THERE")
        rows = client.get("/api/repos/alpha/sessions").json()["sessions"]

    # The warm-up read the file before the rewrite; the list served what it read.
    assert rows[1]["title"] == "hello there"


def test_the_warm_up_skips_excluded_repositories(homes: Homes) -> None:
    for client in open_client(
        homes, frozenset({"secret"}), snapshots=_snapshots("secret", "alpha"), warm=True
    ):
        _warmed(client)

    assert "secret" not in homes.calls


class _StuckFirstCall(Homes):
    """The first resolution (the warm-up's) blocks until released."""

    def __init__(self, table: dict[str, Sequence[tuple[str, Path]]]) -> None:
        super().__init__(table)
        self.release = threading.Event()

    def __call__(self, prefix: str) -> Sequence[tuple[str, Path]] | None:
        first = not self.calls
        found = super().__call__(prefix)
        if first:
            self.release.wait(10)
        return found


def test_requests_are_answered_while_the_warm_up_is_still_running(home: Path) -> None:
    homes = _StuckFirstCall({"alpha": [("claude", home)]})
    for client in open_client(homes, snapshots=_snapshots("alpha"), warm=True):
        deadline = time.monotonic() + 10
        while not homes.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert homes.calls, "the warm-up never started"
        res = client.get("/api/repos/alpha/sessions")
        assert res.status_code == 200
        assert not client.app.state.web.sessions_warmed.is_set()  # type: ignore[attr-defined]  # ASGIApp
        homes.release.set()
        _warmed(client)


def _session_file(
    home: Path, project: str, session_id: str, prompt: str, cwd: str | None = None
) -> None:
    path = home / "projects" / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        **({"cwd": cwd} if cwd is not None else {}),
        "timestamp": "2026-11-20T10:00:00.000Z",
        "message": {"role": "user", "content": prompt},
    }
    path.write_text(json.dumps(record) + "\n")


def test_a_repo_lists_only_sessions_started_in_its_own_containers_repo_dir(
    tmp_path: Path,
) -> None:
    from tests.web_fakes import container, repo_group, snapshot_of

    shared = tmp_path / "shared-claude"  # a misconfigured home both repos point at
    _session_file(shared, "-home-dev-alpha", SESSION_A, "alpha at the root")
    # The encoding is lossy: a subdirectory and a sibling repo share the
    # dash form, so the session's own `cwd` decides.
    _session_file(
        shared,
        "-home-dev-alpha-src",
        "33333333-3333-4333-8333-333333333333",
        "alpha below",
        cwd="/home/dev/alpha/src",
    )
    _session_file(
        shared,
        "-home-dev-alpha-web",
        "55555555-5555-4555-8555-555555555555",
        "alpha-web's",
        cwd="/home/dev/alpha-web",
    )
    _session_file(shared, "-home-dev-beta", SESSION_B, "beta's own")
    _session_file(shared, "-home-dev-alpha2", "44444444-4444-4444-8444-444444444444", "alpha2's")
    alpha = repo_group("alpha", container("alpha-1", repo_dir="/home/dev/alpha"))
    beta = repo_group("beta", container("beta-1", repo="beta", repo_dir="/home/dev/beta"))
    alpha.agent_config_homes = (("alpha-1", "claude", shared),)
    beta.agent_config_homes = (("beta-1", "claude", shared),)
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(initial=snapshot_of(alpha, beta)),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        repo_scope=RemoteRepoScope(frozenset()),
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        token = client.post("/api/auth", json=body).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        alpha_rows = client.get("/api/repos/alpha/sessions").json()["sessions"]
        beta_rows = client.get("/api/repos/beta/sessions").json()["sessions"]

    assert sorted(r["title"] for r in alpha_rows) == ["alpha at the root", "alpha below"]
    assert [r["title"] for r in beta_rows] == ["beta's own"]
