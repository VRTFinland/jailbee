"""Snapshot CLI transport rejects malformed and ambiguous destructive scopes."""

import json

import pytest

from jailbee.outbox.clear_snapshot import decode_snapshot
from jailbee.outbox.models import OutboxError


def payload():
    return {
        "version": 1,
        "plans": [
            {
                "name": "acme-feature",
                "available": True,
                "error": None,
                "delete": [
                    {
                        "proposal": "issue/001.json",
                        "revision": "a" * 64,
                        "state": "pending",
                        "archive": False,
                        "reason": None,
                    }
                ],
                "keep": [],
                "warnings": [],
            }
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(version=True),
        lambda p: p.update(version=2),
        lambda p: p.update(extra="/etc/passwd"),
        lambda p: p["plans"][0].update(name="../../etc/passwd"),
        lambda p: p["plans"][0].update(available=False),
        lambda p: p["plans"].append(p["plans"][0]),
        lambda p: p["plans"][0]["delete"].append(p["plans"][0]["delete"][0]),
        lambda p: p["plans"][0]["delete"][0].update(proposal="issue/../passwd.json"),
        lambda p: p["plans"][0]["delete"][0].update(revision="not-a-revision"),
        lambda p: p["plans"][0]["delete"][0].update(reason="protected"),
        lambda p: p["plans"][0]["delete"][0].update(action=0),
    ],
)
def test_malformed_snapshot_is_rejected_without_echoing_payload(change):
    data = payload()
    change(data)
    with pytest.raises(OutboxError, match=r"^invalid approved clear snapshot$"):
        decode_snapshot(json.dumps(data))


def test_oversized_and_deeply_nested_snapshots_are_rejected():
    for token in ("x" * 64_001, "[" * 2000 + "]" * 2000):
        with pytest.raises(OutboxError, match="invalid approved clear snapshot"):
            decode_snapshot(token)
