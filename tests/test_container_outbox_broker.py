"""Container issue staging and outbox reads go over the broker socket."""

from __future__ import annotations

import json
import queue
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.inbox.transport import SOCKET_ENV
from jailbee.orchestration.listener import Listener
from jailbee.orchestration.query import ContainerSnapshot, Queries
from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import store_db

EMPTY_CHILDREN = json.dumps({"children": [], "observed_at": None, "last_from_child": {}})


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="jbo"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Broker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.queries = Queries(BrokerStore(store_path(root)))
        self.listener = Listener(queue.Queue(), queries=self.queries)
        self.listener.start()

    def serve(self, uuid: str, role: str = "plain") -> Path:
        self.queries.publish_container(uuid, ContainerSnapshot(role, True, EMPTY_CHILDREN, None))
        self.listener.open_container(uuid, self.root / uuid)
        return self.root / uuid / "broker.sock"


@pytest.fixture
def broker(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Broker]:
    instance = Broker(short_dir)
    monkeypatch.setenv(SOCKET_ENV, str(instance.serve("u")))
    yield instance
    instance.listener.stop()


def invoke(*args: str) -> object:
    return CliRunner().invoke(container_app, list(args))


def test_staged_issue_reaches_the_host_store_and_reads_back(broker: Broker, tmp_path: Path) -> None:
    body = "x" * 5000
    result = invoke("issue", "create", "--title", "Bug", "--body", body, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"name": "001-bug.json"}
    (row,) = store_db.read_proposals(broker.root, "u", "issue")
    assert row.name == "001-bug.json"
    assert row.bodies == {"001-bug-1.md": body}
    assert not (tmp_path / ".jailbee/issue-outbox").exists()  # nothing on the file store

    comment = invoke("issue", "comment", "7", "--body", "Thanks")
    assert comment.exit_code == 0, comment.output
    assert "staged issue/002-comment.json" in comment.output

    listed = invoke("outbox", "ls", "--json")
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["issue"] == [
        {"name": "001-bug.json", "summary": "create"},
        {"name": "002-comment.json", "summary": "comment"},
    ]
    shown = invoke("outbox", "show", "issue/001-bug.json")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["actions"][0]["body_file"] == "001-bug-1.md"


def test_show_of_a_missing_issue_proposal_exits_2(broker: Broker) -> None:
    result = invoke("outbox", "show", "issue/001-nope.json")
    assert result.exit_code == 2
    assert "001-nope.json" in result.output


def test_the_broker_refuses_a_child_the_cli_did_not_catch(
    broker: Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SOCKET_ENV, str(broker.serve("kid", role="child")))
    result = invoke("issue", "comment", "7", "--body", "Thanks")
    assert result.exit_code == 1
    assert "child container cannot stage" in result.output
    assert store_db.read_proposals(broker.root, "kid") == []


def test_broker_validation_errors_exit_2(broker: Broker) -> None:
    for _ in range(store_db.MAX_PENDING):
        assert invoke("issue", "comment", "7", "--body", "Thanks").exit_code == 0
    result = invoke("issue", "comment", "7", "--body", "Thanks")
    assert result.exit_code == 2
    assert "cap of 20" in result.output


def test_staging_exits_4_after_the_retry_window_when_the_broker_is_down(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(SOCKET_ENV, str(short_dir / "missing" / "broker.sock"))
    monkeypatch.setenv("JAILBEE_BROKER_RETRY_SECONDS", "0.3")
    started = time.monotonic()
    result = invoke("issue", "comment", "7", "--body", "Thanks")
    assert time.monotonic() - started >= 0.3
    assert result.exit_code == 4
    assert "broker" in result.output and "nothing was staged" in result.output
    assert not (tmp_path / ".jailbee/issue-outbox").exists()


def test_outbox_ls_exits_4_when_the_broker_is_down(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SOCKET_ENV, str(short_dir / "missing" / "broker.sock"))
    monkeypatch.setenv("JAILBEE_BROKER_RETRY_SECONDS", "0")
    assert invoke("outbox", "ls").exit_code == 4
