"""REST data routes on a deep link: no WebSocket attached, the snapshot is fetched on demand.

Through the REST seam with the fake snapshot source and homes derived from
the snapshot (no `agent_homes` override), as the real server runs.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from jailbee.db import get_engine
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.state_service.protocol import Snapshot
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from tests.web_fakes import (
    FakeClock,
    FakeSourceFactory,
    container,
    pin_sequence,
    repo_group,
    snapshot_of,
    wait_until,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "claude"
SESSION_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / "claude-home"
    shutil.copytree(FIXTURES, target)
    return target


def snapshot_with(home: Path) -> Snapshot:
    alpha = repo_group("alpha", container("alpha-1", uuid="u-1"))
    alpha.agent_config_homes = (("alpha-1", "claude", home),)
    secret = repo_group("secret", container("secret-1", repo="secret", uuid="u-s"))
    secret.agent_config_homes = (("secret-1", "claude", home),)
    return snapshot_of(alpha, secret)


def open_client(factory: FakeSourceFactory, *, wait: float = 5.0) -> Iterator[TestClient]:
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=factory,
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        grace_seconds=0.05,
        snapshot_wait_seconds=wait,
        repo_scope=RemoteRepoScope(frozenset({"secret"})),
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        token = client.post("/api/auth", json=body).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


@pytest.mark.parametrize(
    "url",
    [
        "/api/repos/alpha/sessions",
        "/api/containers/alpha-1/sessions",
        f"/api/repos/alpha/sessions/claude/{SESSION_A}",
    ],
)
def test_a_deep_link_gets_data_and_the_source_closes_after_the_grace(home: Path, url: str) -> None:
    factory = FakeSourceFactory(initial=snapshot_with(home))
    for client in open_client(factory):
        res = client.get(url)
        assert res.status_code == 200, res.text
        source = factory.only
        assert source.started
        wait_until(lambda: source.closed)


def test_a_snapshot_arriving_late_is_waited_for(home: Path) -> None:
    factory = FakeSourceFactory()
    for client in open_client(factory):

        def deliver() -> None:
            wait_until(lambda: len(factory.made) == 1)
            factory.only.push(snapshot_with(home))

        worker = threading.Thread(target=deliver)
        worker.start()
        res = client.get("/api/repos/alpha/sessions")
        worker.join()
        assert res.status_code == 200, res.text


def test_no_snapshot_within_the_wait_answers_not_found(home: Path) -> None:
    factory = FakeSourceFactory()
    for client in open_client(factory, wait=0.05):
        assert client.get("/api/repos/alpha/sessions").status_code == 404
        wait_until(lambda: factory.only.closed)


def test_an_excluded_repo_stays_hidden_on_a_deep_link(home: Path) -> None:
    factory = FakeSourceFactory(initial=snapshot_with(home))
    for client in open_client(factory):
        assert client.get("/api/repos/secret/sessions").status_code == 404
        assert client.get("/api/containers/secret-1/sessions").status_code == 404
