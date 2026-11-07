"""Issue draft batches over the real broker socket: append, submit, discard."""
# ruff: noqa: F811  (fixtures imported from the sibling test module)

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.inbox.transport import SOCKET_ENV
from jailbee.issue_manifest import parse_manifest
from jailbee.outbox import store_db
from tests.test_container_outbox_broker import (  # noqa: F401  (fixtures)
    Broker,
    broker,
    isolated,
    short_dir,
)


def invoke(*args: str):
    return CliRunner().invoke(container_app, list(args))


def drafts(broker: Broker) -> list[store_db.Draft]:
    return store_db.OutboxStore(broker.queries._store).list_drafts("u")


def test_a_batch_with_a_ref_submits_as_one_proposal_over_the_socket(broker: Broker) -> None:
    created = invoke(
        "issue", "create", "--title", "Bug", "--body", "x" * 5000, "--ref", "bug", "--batch", "b"
    )
    assert created.exit_code == 0, created.output
    assert invoke("issue", "labels", "@bug", "--add", "triage", "--batch", "b").exit_code == 0
    closing = invoke("issue", "state", "@bug", "closed", "--reason", "completed", "--batch", "b")
    assert closing.exit_code == 0, closing.output
    listed = json.loads(invoke("outbox", "ls", "--json").output)
    assert listed["drafts"] == [{"kind": "issue", "name": "b", "actions": 3}]
    result = invoke("outbox", "submit", "b")
    assert result.exit_code == 0, result.output
    assert "staged issue/001-b.json" in result.output
    (row,) = store_db.read_proposals(broker.root, "u", "issue")
    assert len(parse_manifest(row.name, row.manifest, row.bodies).actions) == 3
    assert json.loads(row.manifest)["actions"][1]["expected"] == {"labels": []}
    assert json.loads(invoke("outbox", "ls", "--json").output)["drafts"] == []


def test_discard_removes_a_draft_over_the_socket(broker: Broker) -> None:
    invoke("issue", "comment", "7", "--body", "x", "--batch", "b")
    assert invoke("outbox", "discard", "b", "--kind", "issue").exit_code == 0
    assert drafts(broker) == []


def test_a_draft_append_exits_4_when_the_broker_is_down(
    broker: Broker, monkeypatch: pytest.MonkeyPatch, short_dir: Path
) -> None:
    monkeypatch.setenv(SOCKET_ENV, str(short_dir / "missing" / "broker.sock"))
    monkeypatch.setenv("JAILBEE_BROKER_RETRY_SECONDS", "0")
    result = invoke("issue", "comment", "7", "--body", "x", "--batch", "b")
    assert result.exit_code == 4
    assert drafts(broker) == []
