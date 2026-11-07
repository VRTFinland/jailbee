"""`jailbee outbox sent` in the container: the caller's own publications, over the socket."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jailbee.inbox.transport import SOCKET_ENV
from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox.publication_db import PublicationStore, SentEntry
from tests import test_container_outbox_broker as base
from tests.test_container_outbox_broker import Broker, invoke

# The real-socket rig of the staging tests: a listener serving container "u".
isolated = base.isolated
short_dir = base.short_dir
broker = base.broker


def _sent(root: Path, uuid: str, manifest: str, *, at: float, state: str = "applied") -> None:
    entry = SentEntry(
        uuid=uuid,
        container_prefix="proj",
        container=f"proj-{uuid}",
        publisher="user",
        outbox="issue",
        manifest=manifest,
        digest="d",
        index=0,
        kind="comment",
        repo="o/r",
        target=7,
        url=None if state == "uncertain" else f"https://github.com/o/r/issues/7#{manifest}",
        title=None,
        content=f"first line of {manifest}\nmore",
    )
    PublicationStore(BrokerStore(store_path(root))).record_sent(entry, state, now=at)  # type: ignore[arg-type]


def test_sent_lists_only_the_calling_containers_records_newest_first(broker: Broker) -> None:
    _sent(broker.root, "u", "001-a.json", at=1.0)
    _sent(broker.root, "u", "002-b.json", at=2.0, state="uncertain")
    _sent(broker.root, "someone-else", "001-x.json", at=3.0)

    result = invoke("outbox", "sent", "--json")

    assert result.exit_code == 0, result.output
    records = json.loads(result.output)["records"]
    assert [r["manifest"] for r in records] == ["002-b.json", "001-a.json"]
    assert records[0]["state"] == "uncertain" and records[0]["url"] is None
    assert records[1]["summary"] == "first line of 001-a.json"
    assert "content" not in records[1]

    text = invoke("outbox", "sent", "--limit", "1")
    assert text.exit_code == 0, text.output
    assert "issue/comment  o/r#7  uncertain?  002-b.json#0" in text.output
    assert "001-a.json" not in text.output


def test_sent_without_records_says_so(broker: Broker) -> None:
    result = invoke("outbox", "sent")
    assert result.exit_code == 0, result.output
    assert "Nothing published" in result.output


def test_a_child_may_read_its_own_sent_records(
    broker: Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SOCKET_ENV, str(broker.serve("kid", role="child")))
    _sent(broker.root, "kid", "001-k.json", at=1.0)
    result = invoke("outbox", "sent", "--json")
    assert result.exit_code == 0, result.output
    assert [r["manifest"] for r in json.loads(result.output)["records"]] == ["001-k.json"]


def test_sent_refuses_a_bad_kind_before_calling_the_broker(broker: Broker) -> None:
    assert invoke("outbox", "sent", "--kind", "wiki").exit_code == 2


def test_sent_exits_4_when_the_broker_is_down(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SOCKET_ENV, str(short_dir / "missing" / "broker.sock"))
    monkeypatch.setenv("JAILBEE_BROKER_RETRY_SECONDS", "0")
    assert invoke("outbox", "sent").exit_code == 4


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 201}, {"limit": "5"}, {"kind": "wiki"}])
def test_the_broker_validates_sent_arguments(tmp_path: Path, args: dict[str, object]) -> None:
    from jailbee.orchestration import wire
    from jailbee.outbox import broker_ops

    store = PublicationStore(BrokerStore(store_path(tmp_path)))
    with pytest.raises(wire.WireError) as caught:
        broker_ops.handle_sent(store, "u", args)
    assert caught.value.code == wire.BAD_REQUEST
