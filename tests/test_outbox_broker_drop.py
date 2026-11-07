"""The outbox.drop socket op."""

from __future__ import annotations

from jailbee.orchestration import wire
from tests.test_orchestration_query import Env
from tests.test_outbox_broker_ops import PLAIN, env, put, stored  # noqa: F401


def test_a_container_drops_its_own_staged_proposal(env: Env) -> None:  # noqa: F811
    assert put(env).ok
    response = env.call(wire.OUTBOX_DROP, uuid=PLAIN, kind="issue", name="001-looks-good.json")
    assert response.ok, response
    assert response.result == {"dropped": "001-looks-good.json"}
    assert stored(env) == []


def test_dropping_a_missing_proposal_is_not_found(env: Env) -> None:  # noqa: F811
    response = env.call(wire.OUTBOX_DROP, uuid=PLAIN, kind="issue", name="009-none.json")
    assert not response.ok
    assert response.error_code == wire.NOT_FOUND


def test_a_stale_revision_is_refused(env: Env) -> None:  # noqa: F811
    assert put(env).ok
    response = env.call(
        wire.OUTBOX_DROP, uuid=PLAIN, kind="issue", name="001-looks-good.json", revision=5
    )
    assert not response.ok
    assert len(stored(env)) == 1


def test_a_child_cannot_drop(env: Env) -> None:  # noqa: F811
    response = env.call(wire.OUTBOX_DROP, uuid="proj-kid", kind="issue", name="001-a.json")
    assert response.error_code == wire.REFUSED
