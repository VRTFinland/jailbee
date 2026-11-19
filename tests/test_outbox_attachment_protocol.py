"""Image captures cross the authenticated outbox protocol, not container paths."""

from __future__ import annotations

import base64
import hashlib

from jailbee.orchestration import wire
from tests.test_outbox_broker_ops import PLAIN
from tests.test_outbox_broker_ops import env as env

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic image bytes"


def begin(env, uuid=PLAIN):
    return env.call(
        "outbox.attachment.begin",
        uuid=uuid,
        name="screen.png",
        content_type="image/png",
        size=len(PNG),
        sha256=hashlib.sha256(PNG).hexdigest(),
    )


def test_capture_is_owned_by_the_authenticated_container_and_stays_immutable(env):
    started = begin(env)
    assert started.ok, started
    blob = started.result["blob"]
    stolen = env.call(
        "outbox.attachment.chunk",
        uuid="proj-orch",
        blob=blob,
        offset=0,
        data=base64.b64encode(PNG).decode(),
    )
    assert not stolen.ok
    chunk = env.call(
        "outbox.attachment.chunk",
        uuid=PLAIN,
        blob=blob,
        offset=0,
        data=base64.b64encode(PNG).decode(),
    )
    assert chunk.ok, chunk
    ready = env.call("outbox.attachment.finish", uuid=PLAIN, blob=blob)
    assert ready.ok, ready
    assert ready.result == {
        "blob": blob,
        "name": "screen.png",
        "content_type": "image/png",
        "size": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    changed = env.call(
        "outbox.attachment.chunk",
        uuid=PLAIN,
        blob=blob,
        offset=len(PNG),
        data="YQ==",
    )
    assert not changed.ok
    listed = env.call(wire.OUTBOX_LIST, uuid=PLAIN, kind="issue")
    assert listed.result == {"proposals": []}


def test_adversarial_capture_rejects_traversal_bad_offsets_and_base64(env):
    unsafe = env.call(
        "outbox.attachment.begin",
        uuid=PLAIN,
        name="../screen.png",
        content_type="image/png",
        size=len(PNG),
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    assert not unsafe.ok
    blob = begin(env).result["blob"]
    for offset, data in ((1, "YQ=="), (0, "!!!"), (True, "YQ==")):
        rejected = env.call(
            "outbox.attachment.chunk",
            uuid=PLAIN,
            blob=blob,
            offset=offset,
            data=data,
        )
        assert not rejected.ok
    unfinished = env.call("outbox.attachment.finish", uuid=PLAIN, blob=blob)
    assert not unfinished.ok
