"""`GET /api/repos/{prefix}/sessions/{agent}/{session_id}/subagents/{agent_id}`.

Through the REST seam over the committed Claude fixture, which holds a
session with one subagent transcript.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient

from tests.test_web_repo_sessions import FIXTURES, SESSION_B, Homes, open_client

SESSION_A = "11111111-1111-4111-8111-111111111111"
AGENT_ID = "a1b2c3d4e5f6a7b8c"
URL = f"/api/repos/alpha/sessions/claude/{SESSION_A}/subagents/{AGENT_ID}"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / "claude-home"
    shutil.copytree(FIXTURES, target)
    return target


@pytest.fixture
def homes(home: Path) -> Homes:
    return Homes({"alpha": [("claude", home)], "secret": [("claude", home)]})


@pytest.fixture
def client(homes: Homes) -> Iterator[TestClient]:
    yield from open_client(homes, frozenset({"secret"}))


def test_the_parent_session_references_its_subagent(client: TestClient) -> None:
    body = client.get(f"/api/repos/alpha/sessions/claude/{SESSION_A}").json()
    refs = [e for e in body["entries"] if e["kind"] == "subagent"]
    assert [r["agent_id"] for r in refs] == [AGENT_ID]
    assert refs[0]["description"] == "Review auth"


def test_a_subagent_transcript_is_rendered_like_a_session(client: TestClient) -> None:
    res = client.get(URL)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["session_id"] == SESSION_A
    assert body["agent"] == "claude"
    assert body["models"] == ["claude-haiku-5"]
    kinds = [e["kind"] for e in body["entries"]]
    assert kinds == ["user", "assistant"]
    assert "Review it." in body["entries"][0]["html"]
    assert "<p>Looks fine.</p>" in body["entries"][1]["html"]


def test_a_subagent_title_is_the_description(client: TestClient) -> None:
    assert client.get(URL).json()["title"] == "Review auth"


@pytest.mark.parametrize(
    "url",
    [
        f"/api/repos/secret/sessions/claude/{SESSION_A}/subagents/{AGENT_ID}",
        f"/api/repos/nope/sessions/claude/{SESSION_A}/subagents/{AGENT_ID}",
        f"/api/repos/alpha/sessions/codex/{SESSION_A}/subagents/{AGENT_ID}",
        f"/api/repos/alpha/sessions/claude/{SESSION_A}/subagents/affffffffffffffff",
        f"/api/repos/alpha/sessions/claude/{SESSION_B}/subagents/{AGENT_ID}",
        f"/api/repos/alpha/sessions/claude/{SESSION_A}/subagents/{AGENT_ID}.meta",
    ],
)
def test_unknown_or_hidden_subagents_give_the_same_404(client: TestClient, url: str) -> None:
    res = client.get(url)
    assert res.status_code == 404
    assert res.json() == {"detail": "Unknown session"}


def test_an_excluded_repo_is_never_resolved(client: TestClient, homes: Homes) -> None:
    client.get(f"/api/repos/secret/sessions/claude/{SESSION_A}/subagents/{AGENT_ID}")
    assert "secret" not in homes.calls


def test_requires_login(client: TestClient) -> None:
    client.headers.pop("Authorization")
    assert client.get(URL).status_code == 401


def test_a_traversal_attempt_is_not_found(client: TestClient) -> None:
    res = client.get(f"/api/repos/alpha/sessions/claude/{SESSION_A}/subagents/..%2F..%2Fx")
    assert res.status_code == 404
