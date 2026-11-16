"""Only complete host-owned image captures can enter an issue proposal."""

from __future__ import annotations

import base64
import hashlib
import json

from jailbee.orchestration import wire
from tests.test_outbox_broker_ops import PLAIN, comment, manifest, put, stored
from tests.test_outbox_broker_ops import env as env

PNG = b"\x89PNG\r\n\x1a\nsynthetic image"


def capture(env):
    started = env.call(
        wire.OUTBOX_ATTACHMENT_BEGIN,
        uuid=PLAIN,
        name="screen.png",
        content_type="image/png",
        size=len(PNG),
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    assert started.ok
    blob = started.result["blob"]
    assert env.call(
        wire.OUTBOX_ATTACHMENT_CHUNK,
        uuid=PLAIN,
        blob=blob,
        offset=0,
        data=base64.b64encode(PNG).decode(),
    ).ok
    result = env.call(wire.OUTBOX_ATTACHMENT_FINISH, uuid=PLAIN, blob=blob)
    assert result.ok
    return result.result


def test_complete_images_are_atomically_bound_to_a_stored_proposal(env):
    descriptor = capture(env)
    value = {
        **manifest(comment("Before ![Evidence](attachment:screen) after")),
        "attachments": {"screen": descriptor},
    }
    result = put(env, manifest=value)
    assert result.ok, result
    (row,) = stored(env)
    assert json.loads(row.manifest)["attachments"] == {"screen": descriptor}
    shown = env.call(wire.OUTBOX_SHOW, uuid=PLAIN, kind="issue", name=row.name)
    assert (
        json.loads(shown.result["manifest"])["attachments"]["screen"]["sha256"]
        == descriptor["sha256"]
    )


def test_forged_metadata_and_unknown_captures_never_create_partial_proposals(env):
    descriptor = capture(env)
    for invalid in (
        {**descriptor, "sha256": "0" * 64},
        {**descriptor, "blob": "z" * 32},
        {**descriptor, "size": len(PNG) + 1},
    ):
        value = {
            **manifest(comment("![Evidence](attachment:screen)")),
            "attachments": {"screen": invalid},
        }
        response = put(env, manifest=value)
        assert not response.ok
        assert stored(env) == []
